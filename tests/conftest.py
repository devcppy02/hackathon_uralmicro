from datetime import datetime

import pytest
from fastapi.testclient import TestClient

from app.api import create_app
from app.config import Settings
from app.models import CallInput, Extraction
from app.storage import Storage

NOW = datetime.fromisoformat("2026-10-29T10:05:00+05:00")


@pytest.fixture(autouse=True)
def fixed_clock(monkeypatch):
    monkeypatch.setattr("app.storage.utcnow", lambda: NOW)


@pytest.fixture
def call_json():
    return {
        "id": "call-1",
        "operator_id": 7,
        "client_phone": "+70000000000",
        "started_at": "2026-10-29T10:00:00+05:00",
        "ended_at": "2026-10-29T10:05:00+05:00",
        "transcript": [
            {"speaker": "client", "text": "Пришлите договор. Я сам перезвоню."},
            {"speaker": "operator", "text": "Я отправлю договор через час. Завтра перезвоню."},
        ],
    }


@pytest.fixture
def call(call_json):
    return CallInput(**call_json)


@pytest.fixture
def promise_json():
    return {
        "type_code": "send_documents",
        "title": "Отправить договор",
        "quote": "Я отправлю договор через час.",
        "replica_index": 1,
        "deadline_text": "через час",
        "deadline_at": None,
    }


@pytest.fixture
def extraction(promise_json):
    return Extraction(calls=[{"call_id": "call-1", "promises": [promise_json]}])


@pytest.fixture
def storage(tmp_path):
    store = Storage(tmp_path / "data" / "test.sqlite3")
    store.initialize()
    return store


class FakeExtractor:
    def __init__(self, result):
        self.result = result
        self.requests = []

    async def extract(self, calls, types):
        self.requests.append((calls, types))
        return self.result


@pytest.fixture
def fake(extraction):
    return FakeExtractor(extraction)


@pytest.fixture
def client(tmp_path, fake):
    settings = Settings(_env_file=None, database_path=tmp_path / "api.sqlite3")
    with TestClient(create_app(settings, extractor=fake)) as client:
        yield client
