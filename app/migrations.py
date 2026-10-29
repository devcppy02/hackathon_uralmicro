"""Additive, repeatable migration from the original API database."""

import sqlite3


def migrate(db: sqlite3.Connection) -> None:
    for table, columns in {
        "calls": {"payload": "TEXT", "ended_at": "TEXT", "client_phone": "TEXT"},
        "tasks": {"revision": "INTEGER NOT NULL DEFAULT 0"},
    }.items():
        existing = {row[1] for row in db.execute(f"PRAGMA table_info({table})")}
        for name, definition in columns.items():
            if name not in existing:
                db.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")
    db.executescript("""
        CREATE TABLE IF NOT EXISTS task_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id TEXT NOT NULL REFERENCES tasks(id), action TEXT NOT NULL,
            actor TEXT NOT NULL, reason TEXT NOT NULL, before_json TEXT,
            after_json TEXT NOT NULL, created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS history_task ON task_history(task_id, id);
        CREATE TABLE IF NOT EXISTS completion_suggestions (
            id TEXT PRIMARY KEY, task_id TEXT NOT NULL REFERENCES tasks(id),
            call_id TEXT NOT NULL REFERENCES calls(id), quote TEXT NOT NULL,
            replica_index INTEGER NOT NULL, speaker TEXT NOT NULL,
            explanation TEXT NOT NULL, task_revision INTEGER NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending', created_at TEXT NOT NULL,
            reviewed_at TEXT, reviewer TEXT, review_reason TEXT,
            UNIQUE(task_id, call_id, task_revision)
        );
        CREATE TABLE IF NOT EXISTS completion_checks (
            task_id TEXT NOT NULL REFERENCES tasks(id), call_id TEXT NOT NULL REFERENCES calls(id),
            task_revision INTEGER NOT NULL, PRIMARY KEY(task_id,call_id,task_revision)
        );
        CREATE TABLE IF NOT EXISTS telegram_invites (
            code_hash TEXT PRIMARY KEY, operator_id INTEGER NOT NULL,
            expires_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS telegram_bindings (
            operator_id INTEGER PRIMARY KEY, chat_id INTEGER NOT NULL UNIQUE,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS notifications (
            id INTEGER PRIMARY KEY AUTOINCREMENT, event_key TEXT NOT NULL UNIQUE,
            task_id TEXT NOT NULL REFERENCES tasks(id), operator_id INTEGER NOT NULL,
            kind TEXT NOT NULL, due_snapshot TEXT NOT NULL, suggestion_id TEXT,
            status TEXT NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0,
            next_attempt_at TEXT NOT NULL, created_at TEXT NOT NULL, sent_at TEXT,
            last_error TEXT
        );
        CREATE INDEX IF NOT EXISTS notification_pending ON notifications(status, next_attempt_at);
    """)
    db.execute("PRAGMA user_version=2")
