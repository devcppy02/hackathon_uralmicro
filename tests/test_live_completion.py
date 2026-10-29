import os
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from app.api import create_app
from app.config import Settings

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(
        os.getenv("RUN_LIVE_LLM") != "1",
        reason="Set RUN_LIVE_LLM=1 to use the real Qwen",
    ),
]


def test_real_qwen_followup_confirmation(tmp_path):
    now = datetime.now(UTC)
    original = {
        "id": "promise-doc",
        "operator_id": 7,
        "client_phone": "+79990000000",
        "started_at": (now - timedelta(hours=2, minutes=5)).isoformat(),
        "ended_at": (now - timedelta(hours=2)).isoformat(),
        "transcript": [{"speaker": "operator", "text": "Я отправлю вам договор через три часа."}],
    }
    settings = Settings(_env_file=None, database_path=tmp_path / "real-followup.sqlite3")
    with TestClient(create_app(settings)) as client:
        first = client.post("/calls", json=[original])
        assert first.status_code == 200, first.text
        task_id = first.json()["tasks"][0]["id"]
        for identifier, text, expected in [
            ("wrong-document", "Справку получил, а договор пока не пришёл.", 0),
            ("confirmed-doc", "Да, договор получил и уже подписал, спасибо.", 1),
        ]:
            call = {
                **original,
                "id": identifier,
                "started_at": (now - timedelta(minutes=5)).isoformat(),
                "ended_at": now.isoformat(),
                "transcript": [{"speaker": "client", "text": text}],
            }
            response = client.post("/calls", json=[call])
            assert response.status_code == 200, response.text
            assert not response.json()["warnings"], response.text
            assert response.json()["completions_suggested"] == expected, response.text
        assert client.get("/tasks/" + task_id).json()["status"] != "completed"
        proposal = client.get("/completion-suggestions").json()[0]
        result = client.post(
            f"/completion-suggestions/{proposal['id']}/review",
            json={"accept": True, "actor": "Тест"},
        )
        assert result.status_code == 200 and result.json()["task"]["status"] == "completed"
