from datetime import timedelta

from app.models import TaskStatus
from app.notifications import Notifications
from tests.conftest import NOW
from tests.test_workflow import setup_evidence


def test_binding_codes_are_one_time_and_expire(storage, monkeypatch):
    notifications = Notifications(storage)
    link = notifications.create_link(7)
    with storage.connect() as db:
        assert db.execute("SELECT code_hash FROM telegram_invites").fetchone()[0] != link["code"]
    assert notifications.bind("wrong", 100) is None
    assert notifications.bind(link["code"], 100) == 7
    assert notifications.bind(link["code"], 200) is None
    assert notifications.operator(100) == 7
    notifications.unbind(100)
    assert notifications.operator(100) is None
    expired = notifications.create_link(7)
    monkeypatch.setattr("app.notifications.utcnow", lambda: NOW + timedelta(minutes=11))
    assert notifications.bind(expired["code"], 100) is None


def test_notification_lifecycle_and_dedup(storage, call, extraction, monkeypatch):
    task = storage.save_calls([call], extraction).tasks[0]
    notifications = Notifications(storage)
    notifications.prepare()
    notifications.prepare()
    assert notifications.journal()["total"] == 1
    assert not notifications.pending()
    notifications.bind(notifications.create_link(7)["code"], 100)
    first = notifications.pending()[0]
    notifications.failed(first["id"], "NetworkError", retry_after=30)
    assert not notifications.pending()
    monkeypatch.setattr("app.notifications.utcnow", lambda: NOW + timedelta(minutes=40))
    notifications.prepare()
    assert {row["kind"] for row in notifications.pending()} == {"new", "reminder"}
    for item in notifications.pending():
        notifications.sent(item["id"])
    notifications.prepare()
    assert not notifications.pending()
    monkeypatch.setattr("app.notifications.utcnow", lambda: NOW + timedelta(hours=2))
    notifications.prepare()
    assert notifications.pending()[0]["kind"] == "overdue"
    storage.update_task(task.id, TaskStatus.COMPLETED)
    assert not notifications.pending()
    assert notifications.journal(operator_id=999)["total"] == 0
    assert notifications.journal(operator_id=7, limit=1)["total"] == 3


def test_expired_reminder_is_not_sent(storage, call, extraction, monkeypatch):
    storage.save_calls([call], extraction)
    notifications = Notifications(storage)
    notifications.bind(notifications.create_link(7)["code"], 100)
    monkeypatch.setattr("app.notifications.utcnow", lambda: NOW + timedelta(minutes=40))
    notifications.prepare()
    monkeypatch.setattr("app.notifications.utcnow", lambda: NOW + timedelta(hours=2))
    notifications.prepare()
    assert "reminder" not in {row["kind"] for row in notifications.pending()}


def test_completion_notification_becomes_obsolete(storage, call, extraction):
    workflow, task, _, _ = setup_evidence(storage, call, extraction)
    notifications = Notifications(storage)
    notifications.bind(notifications.create_link(7)["code"], 100)
    notifications.prepare()
    assert "completion" in {row["kind"] for row in notifications.pending()}
    from app.models import CompletionDecision

    workflow.review(workflow.suggestions()[0]["id"], CompletionDecision(accept=False))
    assert "completion" not in {row["kind"] for row in notifications.pending()}


def test_notification_api_without_token(client, call_json):
    client.post("/calls", json=[call_json])
    assert client.get("/notifications").json()["total"] == 1
    assert client.get("/telegram/status").json()["configured"] is False
    assert client.post("/telegram/link", json={"operator_id": 7}).status_code == 503
    assert client.post("/telegram/link", json={"operator_id": 0}).status_code == 422
