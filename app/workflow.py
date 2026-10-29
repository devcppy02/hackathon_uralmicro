"""Task corrections, audit trail and human review of model evidence."""

import json
from typing import TYPE_CHECKING
from uuid import uuid4

from app.models import (
    CallInput,
    CompletionDecision,
    CompletionEvidence,
    Task,
    TaskCorrection,
    phone_key,
    timestamp,
    utcnow,
)

if TYPE_CHECKING:
    from app.storage import Storage


def record_event(db, before, after, action, actor, reason=""):
    db.execute(
        "INSERT INTO task_history(task_id,action,actor,reason,before_json,after_json,created_at) "
        "VALUES (?,?,?,?,?,?,?)",
        (
            after["id"],
            action,
            actor,
            reason,
            json.dumps(dict(before), ensure_ascii=False) if before else None,
            json.dumps(dict(after), ensure_ascii=False),
            timestamp(utcnow()),
        ),
    )


class Workflow:
    def __init__(self, storage: "Storage"):
        self.storage = storage

    def history(self, task_id):
        with self.storage.connect() as db:
            return [
                {
                    **dict(row),
                    "before": json.loads(row["before_json"]) if row["before_json"] else None,
                    "after": json.loads(row["after_json"]),
                }
                for row in db.execute(
                    "SELECT * FROM task_history WHERE task_id=? ORDER BY id", (task_id,)
                )
            ]

    def correct(self, task_id: str, item: TaskCorrection) -> Task | None:
        from app.storage import ConflictError, expire_tasks

        with self.storage.connect() as db:
            expire_tasks(db, utcnow())
            before = db.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
            if before is None:
                return None
            if before["status"] == "completed":
                raise ConflictError("Completed tasks cannot be corrected")
            changes = {
                key: value
                for key, value in item.model_dump().items()
                if key in {"title", "type_code", "due_at"} and value is not None
            }
            if (
                "type_code" in changes
                and not db.execute(
                    "SELECT 1 FROM promise_types WHERE code=?", (changes["type_code"],)
                ).fetchone()
            ):
                raise ConflictError("Promise type not found")
            if "due_at" in changes:
                changes["due_at"] = timestamp(changes["due_at"])
                changes["deadline_source"] = "manual"
                changes["deadline_text"] = None
                if before["status"] == "overdue" and item.due_at >= utcnow():
                    changes["status"] = "in_progress"
            changes["revision"] = before["revision"] + 1
            changes["updated_at"] = timestamp(utcnow())
            db.execute(
                "UPDATE tasks SET " + ",".join(f"{key}=?" for key in changes) + " WHERE id=?",
                [*changes.values(), task_id],
            )
            db.execute(
                "UPDATE completion_suggestions SET status='obsolete' "
                "WHERE task_id=? AND status='pending'",
                (task_id,),
            )
            after = db.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
            record_event(db, before, after, "corrected", item.actor, item.reason)
            expire_tasks(db, utcnow())
            return Task(**dict(db.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()))

    def summary(self, operator_id=None):
        from app.storage import expire_tasks

        with self.storage.connect() as db:
            expire_tasks(db, utcnow())
            where = " WHERE operator_id=?" if operator_id else ""
            rows = db.execute(
                """SELECT operator_id, count(*) total,
                sum(status='created') created, sum(status='in_progress') in_progress,
                sum(status='completed') completed, sum(status='overdue') overdue,
                sum(status='completed' AND completed_at<=due_at) completed_on_time,
                sum(status='completed' AND completed_at>due_at) completed_late
                FROM tasks"""
                + where
                + " GROUP BY operator_id ORDER BY operator_id",
                (operator_id,) if operator_id else (),
            ).fetchall()
            operators = [dict(row) for row in rows]
            keys = [
                "total",
                "created",
                "in_progress",
                "completed",
                "overdue",
                "completed_on_time",
                "completed_late",
            ]
            totals = {key: sum(row[key] for row in operators) for key in keys}
            return {"totals": totals, "operators": operators}

    def completion_contexts(self, calls: list[CallInput]):
        """Consider stored follow-ups too: an earlier call may arrive late."""
        phones = sorted({phone_key(call.client_phone) for call in calls})
        phones = [phone for phone in phones if phone.isdigit() and len(phone) >= 7]
        if not phones:
            return []
        result = {}
        with self.storage.connect() as db:
            rows = db.execute(
                """SELECT t.*, ev.payload evidence_payload, ev.id evidence_id
                FROM tasks t JOIN calls src ON src.id=t.call_id
                JOIN calls ev ON ev.client_phone=src.client_phone AND ev.ended_at>src.ended_at
                WHERE t.status!='completed' AND ev.payload IS NOT NULL
                AND src.client_phone IN ("""
                + ",".join("?" for _ in phones)
                + """ )
                AND NOT EXISTS (SELECT 1 FROM completion_checks s
                    WHERE s.task_id=t.id AND s.call_id=ev.id AND s.task_revision=t.revision)
                ORDER BY ev.ended_at, t.id""",
                phones,
            ).fetchall()
            for row in rows:
                call_id = row["evidence_id"]
                if call_id not in result:
                    result[call_id] = {
                        "call": CallInput.model_validate_json(row["evidence_payload"]),
                        "tasks": [],
                    }
                values = {key: row[key] for key in Task.model_fields}
                result[call_id]["tasks"].append(Task(**values))
        return list(result.values())

    def save_suggestions(self, evidence: list[CompletionEvidence], contexts) -> int:
        allowed = {
            (task.id, ctx["call"].id): (task, ctx["call"])
            for ctx in contexts
            for task in ctx["tasks"]
        }
        count = 0
        with self.storage.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            for item in evidence:
                task, call = allowed[(item.task_id, item.call_id)]
                current = db.execute("SELECT * FROM tasks WHERE id=?", (task.id,)).fetchone()
                if current["status"] == "completed" or current["revision"] != task.revision:
                    continue
                count += db.execute(
                    """INSERT OR IGNORE INTO completion_suggestions
                    (id,task_id,call_id,quote,replica_index,speaker,explanation,task_revision,created_at)
                    VALUES (?,?,?,?,?,?,?,?,?)""",
                    (
                        str(uuid4()),
                        task.id,
                        call.id,
                        item.quote,
                        item.replica_index,
                        call.transcript[item.replica_index].speaker,
                        item.explanation,
                        task.revision,
                        timestamp(utcnow()),
                    ),
                ).rowcount
            for (task_id, call_id), (task, _call) in allowed.items():
                db.execute(
                    "INSERT OR IGNORE INTO completion_checks VALUES (?,?,?)",
                    (task_id, call_id, task.revision),
                )
        return count

    def suggestions(self, operator_id=None, status="pending"):
        with self.storage.connect() as db:
            clauses, values = ["s.status=?"], [status]
            if operator_id:
                clauses.append("t.operator_id=?")
                values.append(operator_id)
            return [
                dict(row)
                for row in db.execute(
                    "SELECT s.*,t.title task_title,t.operator_id,t.client_phone FROM "
                    "completion_suggestions s JOIN tasks t ON t.id=s.task_id WHERE "
                    + " AND ".join(clauses)
                    + " ORDER BY s.created_at DESC",
                    values,
                )
            ]

    def review(self, suggestion_id: str, decision: CompletionDecision):
        from app.storage import ConflictError

        with self.storage.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            suggestion = db.execute(
                "SELECT * FROM completion_suggestions WHERE id=?", (suggestion_id,)
            ).fetchone()
            if suggestion is None:
                return None
            if suggestion["status"] != "pending":
                raise ConflictError("Suggestion has already been reviewed or is obsolete")
            task = db.execute("SELECT * FROM tasks WHERE id=?", (suggestion["task_id"],)).fetchone()
            if task["status"] == "completed" or task["revision"] != suggestion["task_revision"]:
                raise ConflictError("Task changed; suggestion is obsolete")
            now = timestamp(utcnow())
            status = "accepted" if decision.accept else "rejected"
            db.execute(
                "UPDATE completion_suggestions SET status=?,reviewed_at=?,reviewer=?,"
                "review_reason=? WHERE id=?",
                (status, now, decision.actor, decision.reason, suggestion_id),
            )
            if decision.accept:
                db.execute(
                    "UPDATE tasks SET status='completed',completed_at=?,updated_at=? WHERE id=?",
                    (now, now, task["id"]),
                )
                db.execute(
                    "UPDATE completion_suggestions SET status='obsolete' WHERE task_id=? "
                    "AND status='pending'",
                    (task["id"],),
                )
            after = db.execute("SELECT * FROM tasks WHERE id=?", (task["id"],)).fetchone()
            record_event(
                db,
                task,
                after,
                "completion_" + status,
                decision.actor,
                decision.reason or suggestion["quote"],
            )
            return {"status": status, "task": Task(**dict(after)).model_dump(mode="json")}
