import os

import httpx
import pytest
from fastapi.testclient import TestClient

from app.api import create_app
from app.config import Settings
from app.llm import QwenExtractor
from app.models import AutoTypeExtraction, PromiseType
from app.storage import DEFAULT_TYPES

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(
        os.getenv("RUN_LIVE_LLM") != "1",
        reason="Set RUN_LIVE_LLM=1 to use the real LM Studio",
    ),
]


def make_call(number, text):
    return {
        "id": f"auto-{number}",
        "operator_id": 7,
        "client_phone": "+70000000000",
        "started_at": "2026-10-29T10:00:00+05:00",
        "ended_at": "2026-10-29T10:05:00+05:00",
        "transcript": [{"speaker": "operator", "text": text}],
    }


async def test_real_qwen_novelty_review_reuses_document_category(extraction):
    result = AutoTypeExtraction(
        **extraction.model_dump(),
        new_types=[
            PromiseType(
                code="send_contract",
                name="Отправить договор",
                description="Оператор обещает отправить клиенту договор.",
            )
        ],
    )
    result.calls[0].promises[0].type_code = "send_contract"
    async with httpx.AsyncClient(trust_env=False) as client:
        extractor = QwenExtractor(client, Settings(_env_file=None, auto_create_promise_types=True))
        await extractor.resolve_new_types(result, DEFAULT_TYPES)
    assert result.new_types == []
    assert result.calls[0].promises[0].type_code == "send_documents"


def test_real_qwen_creates_reusable_category_without_splitting_existing_types(tmp_path):
    cases = [
        ("Я передам ваше обращение руководителю завтра.", None),
        ("Я передам ваше обращение в профильный отдел сегодня.", None),
        ("Я отправлю вам справку через час.", "send_documents"),
        ("Я оформлю возврат средств на ваш счёт завтра.", "refund"),
        ("Я перезвоню вам завтра в 11:00.", "callback"),
        ("Обращение уже передано руководителю. Новых действий не обещаю.", "negative"),
        ("Я уточню статус у коллег и сообщу вам ответ завтра.", "clarify_and_return"),
    ]
    calls = [make_call(i, text) for i, (text, _) in enumerate(cases)]
    settings = Settings(
        _env_file=None, database_path=tmp_path / "auto.sqlite3", auto_create_promise_types=True
    )
    with TestClient(create_app(settings, start_bot=False)) as client:
        assert (
            client.post(
                "/promise-types",
                json={
                    "code": "refund",
                    "name": "Возврат денег",
                    "description": "Оператор обещает вернуть деньги клиенту на карту или счёт.",
                },
            ).status_code
            == 201
        )
        response = client.post("/calls", json=calls)
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["processed"] == len(cases) and body["tasks_created"] == 6, body
        assert len(body["types_created"]) == 1, body
        created_type = body["types_created"][0]
        for i, (_, expected) in enumerate(cases):
            tasks = [t for t in body["tasks"] if t["call_id"] == f"auto-{i}"]
            if expected == "negative":
                assert tasks == []
            else:
                assert len(tasks) == 1, tasks
                assert tasks[0]["type_code"] == (expected or created_type["code"]), tasks

        more_calls = [
            make_call(7, "Я передам ваше обращение в юридический отдел завтра."),
            make_call(8, "Я вышлю вам выписку через два часа."),
        ]
        response = client.post("/calls", json=more_calls)
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["types_created"] == [] and body["tasks_created"] == 2, body
        assert {t["type_code"] for t in body["tasks"]} == {created_type["code"], "send_documents"}
        repeated = client.post("/calls", json=calls).json()
        assert repeated["duplicates"] == len(cases) and repeated["types_created"] == []
        assert len(client.get("/promise-types").json()) == 5

    settings = Settings(_env_file=None, database_path=settings.database_path)
    with TestClient(create_app(settings, start_bot=False)) as client:
        response = client.post(
            "/calls", json=[make_call(9, "Я передам ваше обращение начальнику отдела завтра.")]
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["types_created"] == [] and body["tasks_created"] == 1, body
        assert body["tasks"][0]["type_code"] == created_type["code"]
