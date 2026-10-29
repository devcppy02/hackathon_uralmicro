from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from app.api import create_app
from app.completion import CompletionChecker
from app.config import Settings
from app.llm import LLMError
from app.models import CompletionExtraction, Extraction, TaskCorrection
from app.storage import Storage
from app.workflow import Workflow
from tests.test_bot import TEST_TOKEN
from tests.test_workflow import followup


def test_web_assets_and_no_token_leak(client):
    page = client.get("/")
    assert page.status_code == 200 and "text/html" in page.headers["content-type"]
    assert "Задачи" in page.text
    for asset in ["style.css", "app.js"]:
        assert client.get("/static/" + asset).status_code == 200
    assert client.get("/static/%2e%2e/config.py").status_code == 404
    assert "telegram_bot_token" not in client.get("/telegram/status").text


def test_suggestion_review_through_api(client, fake, call, call_json):
    task = client.post("/calls", json=[call_json]).json()["tasks"][0]
    later = followup(call)
    fake.result = Extraction(calls=[{"call_id": later.id, "promises": []}])
    qwen = AsyncMock()
    qwen.request.return_value = CompletionExtraction(
        confirmations=[
            {
                "task_id": task["id"],
                "call_id": later.id,
                "quote": "договор получил",
                "replica_index": 0,
                "explanation": "Получение подтверждено клиентом",
            }
        ]
    )
    client.app.state.checker = CompletionChecker(qwen, Workflow(client.app.state.storage))
    response = client.post("/calls", json=[later.model_dump(mode="json")])
    assert response.json()["completions_suggested"] == 1
    proposal = client.get("/completion-suggestions?operator_id=7").json()[0]
    reviewed = client.post(
        f"/completion-suggestions/{proposal['id']}/review", json={"accept": True, "actor": "Анна"}
    )
    assert reviewed.status_code == 200 and reviewed.json()["task"]["status"] == "completed"
    assert (
        client.post(
            f"/completion-suggestions/{proposal['id']}/review", json={"accept": True}
        ).status_code
        == 409
    )
    assert client.get("/tasks/" + task["id"]).json()["status"] == "completed"


def test_failed_confirmation_retains_tasks_and_retry_is_safe(client, fake, call_json):
    client.app.state.checker = AsyncMock()
    client.app.state.checker.check.side_effect = LLMError("LM Studio offline")
    first = client.post("/calls", json=[call_json]).json()
    assert first["tasks_created"] == 1 and first["warnings"]
    repeated = client.post("/calls", json=[call_json]).json()
    assert repeated["duplicates"] == 1 and repeated["tasks_created"] == 0
    assert len(fake.requests) == 1


def test_telegram_startup_link_and_shutdown(tmp_path):
    settings = Settings(
        _env_file=None, database_path=tmp_path / "bot-api.sqlite3", telegram_bot_token=TEST_TOKEN
    )
    app = create_app(settings)
    with patch("app.bot.TelegramService.run", new=AsyncMock()), TestClient(app) as client:
        app.state.telegram.username = "test_ural_bot"
        link = client.post("/telegram/link", json={"operator_id": 7})
        assert link.status_code == 200
        assert link.json()["url"].startswith("https://t.me/test_ural_bot?start=")
        assert client.get("/telegram/status").json()["configured"] is True
        assert TEST_TOKEN not in client.get("/telegram/status").text


def test_noop_correction_does_not_invalidate_history(storage, call, extraction):
    task = storage.save_calls([call], extraction).tasks[0]
    corrected = Workflow(storage).correct(
        task.id, TaskCorrection(title=task.title, due_at=task.due_at, reason="Без изменений")
    )
    assert corrected.revision == task.revision
    assert len(Workflow(storage).history(task.id)) == 1


def test_upgrade_from_v1_preserves_existing_tasks(storage, call, extraction):
    task = storage.save_calls([call], extraction).tasks[0]
    storage.delete_type("callback")
    with storage.connect() as db:
        for table in [
            "completion_checks",
            "completion_suggestions",
            "notifications",
            "task_history",
            "telegram_bindings",
            "telegram_invites",
        ]:
            db.execute("DROP TABLE " + table)
        for column in ["payload", "ended_at", "client_phone"]:
            db.execute("ALTER TABLE calls DROP COLUMN " + column)
        db.execute("ALTER TABLE tasks DROP COLUMN revision")
        db.execute("PRAGMA user_version=1")
    restarted = Storage(storage.path)
    restarted.initialize()
    assert restarted.get_task(task.id) == task
    assert restarted.new_calls([call]) == []
    assert restarted.get_type("callback") is None
