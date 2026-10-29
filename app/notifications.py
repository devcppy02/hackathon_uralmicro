"""Persistent Telegram subscriptions and notification outbox in the same SQLite file."""

import hashlib
import secrets
from datetime import timedelta

from app.models import Task, timestamp, utcnow
from app.storage import expire_tasks


class Notifications:
    def __init__(self, storage):
        self.storage = storage

    def create_link(self, operator_id: int):
        code = secrets.token_urlsafe(18)
        expires = timestamp(utcnow() + timedelta(minutes=10))
        with self.storage.connect() as db:
            db.execute(
                "DELETE FROM telegram_invites WHERE expires_at<? OR operator_id=?",
                (timestamp(utcnow()), operator_id),
            )
            db.execute(
                "INSERT INTO telegram_invites VALUES (?,?,?)",
                (hashlib.sha256(code.encode()).hexdigest(), operator_id, expires),
            )
        return {"code": code, "expires_at": expires}

    def bind(self, code: str, chat_id: int):
        with self.storage.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            digest = hashlib.sha256(code.encode()).hexdigest()
            invite = db.execute(
                "SELECT * FROM telegram_invites WHERE code_hash=? AND expires_at>=?",
                (digest, timestamp(utcnow())),
            ).fetchone()
            if invite is None:
                return None
            db.execute(
                "DELETE FROM telegram_bindings WHERE chat_id=? OR operator_id=?",
                (chat_id, invite["operator_id"]),
            )
            db.execute(
                "INSERT INTO telegram_bindings VALUES (?,?,?)",
                (invite["operator_id"], chat_id, timestamp(utcnow())),
            )
            db.execute("DELETE FROM telegram_invites WHERE code_hash=?", (digest,))
            return invite["operator_id"]

    def operator(self, chat_id):
        with self.storage.connect() as db:
            row = db.execute(
                "SELECT operator_id FROM telegram_bindings WHERE chat_id=?", (chat_id,)
            ).fetchone()
            return row[0] if row else None

    def unbind(self, chat_id):
        with self.storage.connect() as db:
            db.execute("DELETE FROM telegram_bindings WHERE chat_id=?", (chat_id,))

    def bindings(self):
        with self.storage.connect() as db:
            return [
                dict(row)
                for row in db.execute("SELECT * FROM telegram_bindings ORDER BY operator_id")
            ]

    def prepare(self, reminder_minutes=30):
        now = utcnow()
        with self.storage.connect() as db:
            expire_tasks(db, now)
            db.execute("""UPDATE notifications SET status='skipped' WHERE status='pending' AND
                EXISTS (SELECT 1 FROM tasks t WHERE t.id=notifications.task_id AND
                (t.status='completed' OR (kind!='new' AND due_snapshot!=t.due_at)
                 OR (kind='reminder' AND t.status='overdue')))""")
            tasks = db.execute("SELECT * FROM tasks WHERE status!='completed'").fetchall()
            for task in tasks:
                kinds = ["new"]
                if task["status"] == "overdue":
                    kinds.append("overdue")
                elif task["due_at"] <= timestamp(now + timedelta(minutes=reminder_minutes)):
                    kinds.append("reminder")
                for kind in kinds:
                    key = task["id"] + ":" + kind
                    if kind != "new":
                        key += ":" + task["due_at"]
                    db.execute(
                        """INSERT OR IGNORE INTO notifications
                        (event_key,task_id,operator_id,kind,due_snapshot,next_attempt_at,created_at)
                        VALUES (?,?,?,?,?,?,?)""",
                        (
                            key,
                            task["id"],
                            task["operator_id"],
                            kind,
                            task["due_at"],
                            timestamp(now),
                            timestamp(now),
                        ),
                    )
            for row in db.execute("""SELECT s.id,s.task_id,t.operator_id,t.due_at
                    FROM completion_suggestions s JOIN tasks t ON t.id=s.task_id
                    WHERE s.status='pending' AND t.status!='completed'""").fetchall():
                db.execute(
                    """INSERT OR IGNORE INTO notifications
                    (event_key,task_id,operator_id,kind,due_snapshot,suggestion_id,
                     next_attempt_at,created_at) VALUES (?,?,?,?,?,?,?,?)""",
                    (
                        "completion:" + row["id"],
                        row["task_id"],
                        row["operator_id"],
                        "completion",
                        row["due_at"],
                        row["id"],
                        timestamp(now),
                        timestamp(now),
                    ),
                )

    def pending(self, limit=20):
        with self.storage.connect() as db:
            rows = db.execute(
                """SELECT n.*,b.chat_id FROM notifications n
                JOIN telegram_bindings b ON b.operator_id=n.operator_id
                WHERE n.status='pending' AND n.next_attempt_at<=? ORDER BY n.id LIMIT ?""",
                (timestamp(utcnow()), limit),
            ).fetchall()
            result = []
            for row in rows:
                task = db.execute("SELECT * FROM tasks WHERE id=?", (row["task_id"],)).fetchone()
                stale = task["status"] == "completed" or (
                    row["kind"] != "new" and task["due_at"] != row["due_snapshot"]
                )
                stale = stale or (row["kind"] == "reminder" and task["status"] == "overdue")
                suggestion = None
                if row["suggestion_id"]:
                    suggestion = db.execute(
                        "SELECT * FROM completion_suggestions WHERE id=?", (row["suggestion_id"],)
                    ).fetchone()
                    stale = stale or suggestion["status"] != "pending"
                if stale:
                    db.execute("UPDATE notifications SET status='skipped' WHERE id=?", (row["id"],))
                    continue
                result.append(
                    {
                        **dict(row),
                        "task": Task(**dict(task)),
                        "suggestion": dict(suggestion) if suggestion else None,
                    }
                )
            return result

    def sent(self, notification_id):
        with self.storage.connect() as db:
            db.execute(
                "UPDATE notifications SET status='sent',sent_at=?,attempts=attempts+1,"
                "last_error=NULL WHERE id=?",
                (timestamp(utcnow()), notification_id),
            )

    def failed(self, notification_id, error_type, retry_after=30, permanent=False):
        with self.storage.connect() as db:
            db.execute(
                "UPDATE notifications SET status=?,attempts=attempts+1,last_error=?,"
                "next_attempt_at=? WHERE id=?",
                (
                    "skipped" if permanent else "pending",
                    error_type,
                    timestamp(utcnow() + timedelta(seconds=retry_after)),
                    notification_id,
                ),
            )

    def journal(self, operator_id=None, limit=100, offset=0):
        with self.storage.connect() as db:
            where = " WHERE n.operator_id=?" if operator_id else ""
            params = [operator_id] if operator_id else []
            total = db.execute("SELECT count(*) FROM notifications n" + where, params).fetchone()[0]
            rows = db.execute(
                "SELECT n.*,t.title task_title FROM notifications n "
                "JOIN tasks t ON t.id=n.task_id" + where + " ORDER BY n.id DESC LIMIT ? OFFSET ?",
                [*params, limit, offset],
            ).fetchall()
            return {
                "items": [dict(row) for row in rows],
                "total": total,
                "limit": limit,
                "offset": offset,
            }
