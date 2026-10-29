import hashlib
import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

from app.deadlines import resolve_deadline
from app.migrations import migrate
from app.models import (
    CallInput,
    CallsResult,
    Extraction,
    PromiseType,
    PromiseTypeUpdate,
    Task,
    TaskPage,
    TaskStatus,
    phone_key,
    timestamp,
    utcnow,
)
from app.workflow import record_event

DEFAULT_TYPES = [
    PromiseType(
        code="callback", name="Перезвонить", description="Оператор обещает сам перезвонить клиенту."
    ),
    PromiseType(
        code="send_documents",
        name="Выслать документы",
        description="Оператор обещает отправить клиенту документы, договор, справку или выписку.",
    ),
    PromiseType(
        code="clarify_and_return",
        name="Уточнить и вернуться с ответом",
        description=(
            "Оператор обещает выяснить информацию и сообщить результат клиенту. "
            "Это одно составное обещание."
        ),
    ),
]


class ConflictError(ValueError):
    pass


def fingerprint(call: CallInput) -> str:
    payload = json.dumps(call.model_dump(mode="json"), sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(payload.encode()).hexdigest()


def expire_tasks(db: sqlite3.Connection, now: datetime) -> None:
    if not db.in_transaction:
        db.execute("BEGIN IMMEDIATE")
    rows = db.execute(
        "SELECT * FROM tasks WHERE status IN ('created','in_progress') AND due_at < ?",
        (timestamp(now),),
    ).fetchall()
    for before in rows:
        db.execute(
            "UPDATE tasks SET status='overdue',updated_at=? WHERE id=?",
            (timestamp(now), before["id"]),
        )
        after = db.execute("SELECT * FROM tasks WHERE id=?", (before["id"],)).fetchone()
        record_event(db, before, after, "overdue", "system", "Истёк срок задачи")


class Storage:
    def __init__(self, path: Path):
        self.path = path

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        try:
            with db:
                yield db
        finally:
            db.close()

    def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS promise_types (
                    code TEXT PRIMARY KEY, name TEXT NOT NULL, description TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS calls (
                    id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS tasks (
                    id TEXT PRIMARY KEY, call_id TEXT NOT NULL REFERENCES calls(id),
                    operator_id INTEGER NOT NULL, client_phone TEXT NOT NULL,
                    type_code TEXT NOT NULL REFERENCES promise_types(code),
                    title TEXT NOT NULL, quote TEXT NOT NULL, replica_index INTEGER NOT NULL,
                    deadline_text TEXT, deadline_source TEXT NOT NULL, due_at TEXT NOT NULL,
                    status TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                    completed_at TEXT
                );
                CREATE INDEX IF NOT EXISTS tasks_filters ON tasks(operator_id, status, due_at);
            """)
            # Начальные типы добавляем один раз, чтобы удалённые не вернулись после перезапуска.
            if db.execute("PRAGMA user_version").fetchone()[0] == 0:
                db.executemany(
                    "INSERT OR IGNORE INTO promise_types VALUES (:code,:name,:description)",
                    [item.model_dump() for item in DEFAULT_TYPES],
                )
                db.execute("PRAGMA user_version=1")
            migrate(db)

    def list_types(self) -> list[PromiseType]:
        with self.connect() as db:
            return [
                PromiseType(**dict(row))
                for row in db.execute("SELECT * FROM promise_types ORDER BY code")
            ]

    def get_type(self, code: str) -> PromiseType | None:
        with self.connect() as db:
            row = db.execute("SELECT * FROM promise_types WHERE code=?", (code,)).fetchone()
            return PromiseType(**dict(row)) if row else None

    def create_type(self, item: PromiseType) -> PromiseType:
        with self.connect() as db:
            try:
                db.execute(
                    "INSERT INTO promise_types VALUES (:code,:name,:description)", item.model_dump()
                )
            except sqlite3.IntegrityError as error:
                raise ConflictError("Promise type already exists") from error
        return item

    def update_type(self, code: str, item: PromiseTypeUpdate) -> PromiseType | None:
        with self.connect() as db:
            changed = db.execute(
                "UPDATE promise_types SET name=?, description=? WHERE code=?",
                (item.name, item.description, code),
            ).rowcount
        return PromiseType(code=code, **item.model_dump()) if changed else None

    def delete_type(self, code: str) -> bool:
        with self.connect() as db:
            try:
                return db.execute("DELETE FROM promise_types WHERE code=?", (code,)).rowcount > 0
            except sqlite3.IntegrityError as error:
                raise ConflictError("Promise type is used by existing tasks") from error

    def new_calls(self, calls: list[CallInput]) -> list[CallInput]:
        result = []
        seen = {}
        with self.connect() as db:
            for call in calls:
                digest = fingerprint(call)
                row = db.execute("SELECT fingerprint FROM calls WHERE id=?", (call.id,)).fetchone()
                previous = seen.get(call.id) or (row[0] if row else None)
                if previous is not None and previous != digest:
                    raise ConflictError(f"Call {call.id} already has different content")
                if previous is None:
                    result.append(call)
                seen[call.id] = digest
        return result

    def save_calls(
        self,
        calls: list[CallInput],
        extraction: Extraction,
        new_types: list[PromiseType] | None = None,
    ) -> CallsResult:
        promises = {item.call_id: item.promises for item in extraction.calls}
        proposed = {item.code: item for item in new_types or []}
        types_created = []
        new_ids = set()
        processed = 0
        now = utcnow()
        with self.connect() as db:
            # Перепроверяем ID под блокировкой записи, чтобы параллельные повторы не создали дубли.
            db.execute("BEGIN IMMEDIATE")
            for call in calls:
                digest = fingerprint(call)
                row = db.execute("SELECT fingerprint FROM calls WHERE id=?", (call.id,)).fetchone()
                if row:
                    if row[0] != digest:
                        raise ConflictError(f"Call {call.id} already has different content")
                    continue
                db.execute(
                    "INSERT INTO calls(id,fingerprint,payload,ended_at,client_phone) "
                    "VALUES (?,?,?,?,?)",
                    (
                        call.id,
                        digest,
                        call.model_dump_json(),
                        timestamp(call.ended_at),
                        phone_key(call.client_phone),
                    ),
                )
                processed += 1
                new_ids.add(call.id)
                for promise in promises[call.id]:
                    new_type = proposed.pop(promise.type_code, None)
                    if new_type is not None:
                        existing = db.execute(
                            "SELECT * FROM promise_types WHERE code=?", (new_type.code,)
                        ).fetchone()
                        if existing and dict(existing) != new_type.model_dump():
                            raise ConflictError("Promise types changed; retry the webhook")
                        if not existing:
                            db.execute(
                                "INSERT INTO promise_types VALUES (:code,:name,:description)",
                                new_type.model_dump(),
                            )
                            types_created.append(new_type)
                    key = f"{call.id}:{promise.type_code}:{promise.replica_index}:"
                    key += " ".join(promise.quote.casefold().split())
                    due_at, source = resolve_deadline(promise, call.ended_at)
                    task = Task(
                        id=str(uuid5(NAMESPACE_URL, key)),
                        call_id=call.id,
                        operator_id=call.operator_id,
                        client_phone=call.client_phone,
                        type_code=promise.type_code,
                        title=promise.title,
                        quote=promise.quote,
                        replica_index=promise.replica_index,
                        deadline_text=promise.deadline_text,
                        deadline_source=source,
                        due_at=due_at,
                        status=TaskStatus.CREATED,
                        created_at=now,
                        updated_at=now,
                        completed_at=None,
                    )
                    values = task.model_dump(mode="json")
                    for name in ("due_at", "created_at", "updated_at"):
                        values[name] = timestamp(getattr(task, name))
                    try:
                        inserted = db.execute(
                            "INSERT INTO tasks ("
                            + ",".join(values)
                            + ") VALUES ("
                            + ",".join(f":{name}" for name in values)
                            + ") ON CONFLICT(id) DO NOTHING",
                            values,
                        ).rowcount
                        if inserted:
                            record_event(db, None, values, "created", "qwen")
                    except sqlite3.IntegrityError as error:
                        raise ConflictError("Promise types changed; retry the webhook") from error
            expire_tasks(db, now)
            ids = [call.id for call in calls]
            rows = db.execute(
                "SELECT * FROM tasks WHERE call_id IN (" + ",".join("?" for _ in ids) + ") "
                "ORDER BY due_at, id",
                ids,
            ).fetchall()
            created = [Task(**dict(row)) for row in rows]
        # Не считаем новыми задачи из звонков, которые уже сохранил другой запрос.
        count = sum(task.call_id in new_ids for task in created)
        return CallsResult(
            processed=processed,
            duplicates=len(calls) - processed,
            tasks_created=count,
            tasks=created,
            types_created=types_created,
        )

    def list_tasks(
        self,
        *,
        status: TaskStatus | None = None,
        operator_id: int | None = None,
        due_from: datetime | None = None,
        due_to: datetime | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> TaskPage:
        clauses, params = [], []
        for column, op, value in [
            ("status", "=", status),
            ("operator_id", "=", operator_id),
            ("due_at", ">=", timestamp(due_from) if due_from else None),
            ("due_at", "<=", timestamp(due_to) if due_to else None),
        ]:
            if value is not None:
                clauses.append(f"{column} {op} ?")
                params.append(value)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        with self.connect() as db:
            expire_tasks(db, utcnow())
            total = db.execute("SELECT count(*) FROM tasks" + where, params).fetchone()[0]
            rows = db.execute(
                "SELECT * FROM tasks" + where + " ORDER BY due_at, id LIMIT ? OFFSET ?",
                [*params, limit, offset],
            ).fetchall()
        return TaskPage(
            items=[Task(**dict(row)) for row in rows], total=total, limit=limit, offset=offset
        )

    def get_task(self, task_id: str) -> Task | None:
        with self.connect() as db:
            expire_tasks(db, utcnow())
            row = db.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
            return Task(**dict(row)) if row else None

    def update_task(
        self, task_id: str, status: TaskStatus, actor: str = "operator", reason: str = ""
    ) -> Task | None:
        now = utcnow()
        with self.connect() as db:
            expire_tasks(db, now)
            row = db.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
            if row is None:
                return None
            allowed = {
                "created": {"in_progress"},
                "in_progress": {"completed"},
                "overdue": {"completed"},
                "completed": set(),
            }
            if status != row["status"] and status not in allowed[row["status"]]:
                raise ConflictError(f"Cannot change status from {row['status']} to {status}")
            if status != row["status"]:
                before = row
                db.execute(
                    "UPDATE tasks SET status=?, updated_at=?, completed_at=? WHERE id=?",
                    (
                        status,
                        timestamp(now),
                        timestamp(now) if status == "completed" else None,
                        task_id,
                    ),
                )
                after = db.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
                record_event(db, before, after, "status_changed", actor, reason)
                if status == "completed":
                    db.execute(
                        "UPDATE completion_suggestions SET status='obsolete' "
                        "WHERE task_id=? AND status='pending'",
                        (task_id,),
                    )
            row = db.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        return Task(**dict(row))
