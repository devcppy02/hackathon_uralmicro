from datetime import timedelta
from unittest.mock import AsyncMock

import pytest

from app.completion import CompletionChecker
from app.llm import LLMError
from app.models import (
    CallInput,
    CompletionDecision,
    CompletionEvidence,
    CompletionExtraction,
    Extraction,
    TaskCorrection,
    TaskStatus,
)
from app.storage import ConflictError
from app.workflow import Workflow
from tests.conftest import NOW


def followup(call, *, phone=None, earlier=False):
    end = call.ended_at + timedelta(days=-1 if earlier else 1)
    return CallInput(
        **{
            **call.model_dump(),
            "id": "followup",
            "client_phone": phone or call.client_phone,
            "started_at": end - timedelta(minutes=5),
            "ended_at": end,
            "transcript": [{"speaker": "client", "text": "Да, договор получил, спасибо."}],
        }
    )


def save_empty(storage, call):
    storage.save_calls([call], Extraction(calls=[{"call_id": call.id, "promises": []}]))


def setup_evidence(storage, call, extraction):
    task = storage.save_calls([call], extraction).tasks[0]
    later = followup(call)
    save_empty(storage, later)
    workflow = Workflow(storage)
    evidence = CompletionEvidence(
        task_id=task.id,
        call_id=later.id,
        quote="договор получил",
        replica_index=0,
        explanation="Клиент подтвердил получение договора",
    )
    contexts = workflow.completion_contexts([later])
    workflow.save_suggestions([evidence], contexts)
    return workflow, task, later, evidence


def test_correction_and_history(client, call_json):
    task = client.post("/calls", json=[call_json]).json()["tasks"][0]
    url = f"/tasks/{task['id']}"
    changed = client.patch(
        url + "/details",
        json={
            "title": "Отправить именно договор",
            "due_at": (NOW + timedelta(days=2)).isoformat(),
            "reason": "Уточнили договорённость",
            "actor": "Анна",
        },
    )
    assert changed.status_code == 200 and changed.json()["revision"] == 1
    assert changed.json()["deadline_source"] == "manual"
    assert changed.json()["quote"] == task["quote"]
    history = client.get(url + "/history").json()
    assert [event["action"] for event in history] == ["created", "corrected"]
    assert history[-1]["actor"] == "Анна" and history[-1]["before"]["title"] == task["title"]
    assert client.get(url).json()["title"] == "Отправить именно договор"
    assert client.patch(url + "/details", json={"title": "X"}).status_code == 422
    assert client.patch(url + "/details", json={"reason": "Нет изменений"}).status_code == 422
    assert (
        client.patch(
            url + "/details", json={"type_code": "missing", "reason": "Ошибка типа"}
        ).status_code
        == 409
    )
    assert client.get("/tasks/missing").status_code == 404
    assert client.get("/tasks/missing/history").status_code == 404
    assert (
        client.patch(
            "/tasks/missing/details", json={"title": "X", "reason": "Изменение"}
        ).status_code
        == 404
    )


def test_summary_and_completed_correction(client, call_json):
    task = client.post("/calls", json=[call_json]).json()["tasks"][0]
    url = f"/tasks/{task['id']}"
    client.patch(url, json={"status": "in_progress", "actor": "Анна"})
    client.patch(url, json={"status": "completed", "actor": "Анна"})
    assert (
        client.patch(url + "/details", json={"title": "X", "reason": "Изменение"}).status_code
        == 409
    )
    data = client.get("/summary", params={"operator_id": 7}).json()
    assert data["totals"]["completed_on_time"] == 1
    assert data["operators"][0]["operator_id"] == 7
    assert client.get("/summary", params={"operator_id": 999}).json()["totals"]["total"] == 0
    assert len(client.get(url + "/history").json()) == 3


def test_overdue_correction_and_late_completion(storage, call, extraction, monkeypatch):
    task = storage.save_calls([call], extraction).tasks[0]
    later = NOW + timedelta(hours=2)
    for module in ["storage", "workflow"]:
        monkeypatch.setattr(f"app.{module}.utcnow", lambda: later)
    workflow = Workflow(storage)
    assert storage.get_task(task.id).status == "overdue"
    changed = workflow.correct(
        task.id,
        TaskCorrection(due_at=later + timedelta(hours=1), reason="Клиент согласовал новый срок"),
    )
    assert changed.status == "in_progress"
    workflow.correct(task.id, TaskCorrection(due_at=NOW, reason="Уточнение срока"))
    storage.update_task(task.id, TaskStatus.COMPLETED)
    assert workflow.summary()["totals"]["completed_late"] == 1


@pytest.mark.parametrize("accept", [True, False])
def test_evidence_requires_human_and_deduplicates(storage, call, extraction, accept):
    workflow, task, later, evidence = setup_evidence(storage, call, extraction)
    assert storage.get_task(task.id).status == "created"
    assert workflow.completion_contexts([later]) == []
    suggestion = workflow.suggestions(operator_id=7)[0]
    assert suggestion["speaker"] == "client"
    assert workflow.suggestions(operator_id=999) == []
    decision = workflow.review(suggestion["id"], CompletionDecision(accept=accept, actor="Анна"))
    assert decision["status"] == ("accepted" if accept else "rejected")
    assert storage.get_task(task.id).status == ("completed" if accept else "created")
    with pytest.raises(ConflictError):
        workflow.review(suggestion["id"], CompletionDecision(accept=accept))
    assert workflow.review("missing", CompletionDecision(accept=True)) is None


def test_correction_invalidates_evidence(storage, call, extraction):
    workflow, task, _, _ = setup_evidence(storage, call, extraction)
    suggestion = workflow.suggestions()[0]
    workflow.correct(task.id, TaskCorrection(title="Другой договор", reason="Ошибка ИИ"))
    assert workflow.suggestions() == []
    with pytest.raises(ConflictError):
        workflow.review(suggestion["id"], CompletionDecision(accept=True))
    assert len(workflow.suggestions(status="obsolete")) == 1


def test_completing_task_invalidates_proposals(storage, call, extraction):
    workflow, task, _, _ = setup_evidence(storage, call, extraction)
    storage.update_task(task.id, TaskStatus.IN_PROGRESS)
    storage.update_task(task.id, TaskStatus.COMPLETED)
    assert not workflow.suggestions()


@pytest.mark.parametrize(
    "phone, earlier", [("+79999999999", False), (None, True), ("скрыт", False)]
)
def test_unrelated_calls_cannot_confirm(storage, call, extraction, phone, earlier):
    storage.save_calls([call], extraction)
    later = followup(call, phone=phone, earlier=earlier)
    save_empty(storage, later)
    assert Workflow(storage).completion_contexts([later]) == []


def test_late_arrival_and_phone_normalization(storage, call, extraction):
    later = followup(call, phone="80000000000")
    save_empty(storage, later)
    storage.save_calls([call], extraction)
    assert Workflow(storage).completion_contexts([call])[0]["call"].id == later.id


async def test_checker_and_empty_results_checked_once(storage, call, extraction):
    task = storage.save_calls([call], extraction).tasks[0]
    later = followup(call)
    save_empty(storage, later)
    qwen = AsyncMock()
    qwen.request.return_value = CompletionExtraction(confirmations=[])
    checker = CompletionChecker(qwen, Workflow(storage))
    assert await checker.check([later]) == 0
    assert await checker.check([later]) == 0
    qwen.request.assert_awaited_once()
    Workflow(storage).correct(
        task.id, TaskCorrection(title="Исправленный договор", reason="Ошибка ИИ")
    )
    qwen.request.return_value = CompletionExtraction(
        confirmations=[
            {
                "task_id": task.id,
                "call_id": later.id,
                "quote": "договор получил",
                "replica_index": 0,
                "explanation": "Клиент подтвердил получение",
            }
        ]
    )
    assert await checker.check([later]) == 1


@pytest.mark.parametrize(
    "field,value", [("task_id", "bad"), ("replica_index", 99), ("quote", "Выдумано")]
)
async def test_checker_rejects_ungrounded_evidence(storage, call, extraction, field, value):
    task = storage.save_calls([call], extraction).tasks[0]
    later = followup(call)
    save_empty(storage, later)
    item = {
        "task_id": task.id,
        "call_id": later.id,
        "quote": "договор получил",
        "replica_index": 0,
        "explanation": "Клиент подтвердил получение",
        field: value,
    }
    qwen = AsyncMock()
    qwen.request.return_value = CompletionExtraction(confirmations=[item])
    with pytest.raises(LLMError):
        await CompletionChecker(qwen, Workflow(storage)).check([later])
    assert not Workflow(storage).suggestions()


def test_summary_invalid_filters_and_missing_review(client):
    assert client.get("/summary?operator_id=0").status_code == 422
    assert client.get("/completion-suggestions?status=bad").status_code == 422
    assert (
        client.post("/completion-suggestions/missing/review", json={"accept": True}).status_code
        == 404
    )
