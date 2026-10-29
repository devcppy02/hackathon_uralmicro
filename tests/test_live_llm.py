"""Opt-in acceptance test: the real Qwen must understand ten synthetic calls."""

import os
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from app.api import create_app
from app.config import Settings

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(
        os.getenv("RUN_LIVE_LLM") != "1",
        reason="Set RUN_LIVE_LLM=1 to use the real LM Studio",
    ),
]


def test_real_qwen_full_batch(tmp_path):
    cases = [
        ([("operator", "Я перезвоню вам через два часа.")], ["callback"]),
        (
            [
                ("client", "Я сам вам перезвоню."),
                ("operator", "Документы уже отправлены. Перезванивать не буду."),
            ],
            [],
        ),
        ([("operator", "Я оформлю возврат денег на вашу карту завтра до 18:00.")], ["refund"]),
        (
            [("operator", "Вышлю справку завтра к 12:00, а еще перезвоню сегодня в 17:00.")],
            ["callback", "send_documents"],
        ),
        (
            [("operator", "Я уточню сумму у бухгалтера и завтра в 11:00 вернусь к вам с ответом.")],
            ["clarify_and_return"],
        ),
        (
            [
                ("operator", "Могу отправить документы, если понадобятся."),
                ("client", "Не нужно, у меня всё есть."),
            ],
            [],
        ),
        (
            [("client", "Вы отправите справку завтра до 14:00?"), ("operator", "Да, отправлю.")],
            ["send_documents"],
        ),
        ([("operator", "Отправлю вам договор.")], ["send_documents"]),
        (
            [
                ("operator", "Здравствуйте! Чем могу помочь?"),
                ("client", "Спасибо, вопрос уже решился."),
            ],
            [],
        ),
        ([("operator", "Возврат уже оформлен, деньги поступили.")], []),
    ]
    calls = [
        {
            "id": f"live-{i}",
            "operator_id": 7,
            "client_phone": "+70000000000",
            "started_at": "2026-10-29T10:00:00+05:00",
            "ended_at": "2026-10-29T10:05:00+05:00",
            "transcript": [{"speaker": speaker, "text": text} for speaker, text in replicas],
        }
        for i, (replicas, _) in enumerate(cases)
    ]
    settings = Settings(_env_file=None, database_path=tmp_path / "live.sqlite3")
    with TestClient(create_app(settings)) as client:
        assert (
            client.post(
                "/promise-types",
                json={
                    "code": "refund",
                    "name": "Возврат денег",
                    "description": "Оператор обещает вернуть деньги клиенту на карту или счет.",
                },
            ).status_code
            == 201
        )
        response = client.post("/calls", json=calls)
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["processed"] == 10
        assert body["tasks_created"] == 7, body
        for i, (_, expected_types) in enumerate(cases):
            found = [task for task in body["tasks"] if task["call_id"] == f"live-{i}"]
            assert sorted(task["type_code"] for task in found) == sorted(expected_types), found
        relative = next(task for task in body["tasks"] if task["call_id"] == "live-0")
        assert datetime.fromisoformat(relative["due_at"]) == datetime.fromisoformat(
            calls[0]["ended_at"]
        ) + timedelta(hours=2)
        no_deadline = next(task for task in body["tasks"] if task["call_id"] == "live-7")
        assert no_deadline["deadline_source"] == "default_24h"
        assert datetime.fromisoformat(no_deadline["due_at"]) == datetime.fromisoformat(
            calls[7]["ended_at"]
        ) + timedelta(hours=24)
        repeated = client.post("/calls", json=calls).json()
        assert repeated["duplicates"] == 10 and repeated["tasks_created"] == 0
        assert client.get("/tasks", params={"operator_id": 7}).json()["total"] == 7
        task_url = f"/tasks/{relative['id']}"
        assert client.patch(task_url, json={"status": "in_progress"}).status_code == 200
        assert client.patch(task_url, json={"status": "completed"}).status_code == 200


def test_real_qwen_calendar_deadlines(tmp_path):
    cases = [
        ("Перезвоню через два дня к 15:00.", "callback", "2026-10-31T15:00:00+05:00"),
        ("Вышлю договор послезавтра в 15:00.", "send_documents", "2026-10-31T15:00:00+05:00"),
        (
            "Я уточню статус у коллег и сообщу ответ, но не позднее завтра в 12:00.",
            "clarify_and_return",
            "2026-10-30T12:00:00+05:00",
        ),
        ("Перезвоню через двадцать минут.", "callback", "2026-10-30T00:10:00+05:00"),
        ("Отправлю договор до конца дня.", "send_documents", "2026-10-29T23:59:59+05:00"),
        ("Вышлю справку до конца рабочего дня.", "send_documents", "2026-10-29T18:00:00+05:00"),
    ]
    calls = [
        {
            "id": f"deadline-{i}",
            "operator_id": 7,
            "client_phone": "+70000000000",
            "started_at": "2026-10-29T10:00:00+05:00",
            "ended_at": "2026-10-29T23:50:00+05:00" if i == 3 else "2026-10-29T10:05:00+05:00",
            "transcript": [{"speaker": "operator", "text": text}],
        }
        for i, (text, _, _) in enumerate(cases)
    ]
    settings = Settings(_env_file=None, database_path=tmp_path / "deadlines.sqlite3")
    with TestClient(create_app(settings)) as client:
        response = client.post("/calls", json=calls)
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["tasks_created"] == len(cases), body
        for i, (_, expected_type, expected_due) in enumerate(cases):
            found = [task for task in body["tasks"] if task["call_id"] == f"deadline-{i}"]
            assert len(found) == 1, found
            assert found[0]["type_code"] == expected_type
            assert datetime.fromisoformat(found[0]["due_at"]) == datetime.fromisoformat(
                expected_due
            )
