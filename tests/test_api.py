from datetime import timedelta

import pytest

from app.llm import LLMError
from app.models import CallExtraction, ExtractedPromise, Extraction, timestamp
from tests.conftest import NOW


def test_promise_type_crud(client):
    assert len(client.get("/promise-types").json()) == 3
    item = {
        "code": "refund",
        "name": "Возврат денег",
        "description": "Оператор обещает вернуть деньги клиенту",
    }
    response = client.post("/promise-types", json=item)
    assert response.status_code == 201 and response.json() == item
    assert client.post("/promise-types", json=item).status_code == 409
    assert client.get("/promise-types/refund").json() == item
    updated = {"name": "Возврат", "description": "Оператор обещает оформить возврат средств"}
    assert client.put("/promise-types/refund", json=updated).json() == {**updated, "code": "refund"}
    assert client.delete("/promise-types/refund").status_code == 204
    assert client.get("/promise-types/refund").status_code == 404
    assert client.put("/promise-types/refund", json=updated).status_code == 404
    assert client.delete("/promise-types/refund").status_code == 404


def test_webhook_and_status_scenario(client, fake, call_json):
    response = client.post("/calls", json=[call_json])
    assert response.status_code == 200
    body = response.json()
    assert (body["processed"], body["duplicates"], body["tasks_created"]) == (1, 0, 1)
    task = body["tasks"][0]
    assert task["status"] == "created"
    assert task["type_code"] == "send_documents"
    assert client.get("/tasks", params={"operator_id": 7, "status": "created"}).json()["total"] == 1
    repeated = client.post("/calls", json=[call_json]).json()
    assert repeated["duplicates"] == 1 and repeated["tasks_created"] == 0
    assert len(fake.requests) == 1
    task_url = f"/tasks/{task['id']}"
    assert client.patch(task_url, json={"status": "completed"}).status_code == 409
    assert client.patch(task_url, json={"status": "in_progress"}).json()["status"] == "in_progress"
    done = client.patch(task_url, json={"status": "completed"})
    assert done.json()["completed_at"] is not None
    assert client.patch(task_url, json={"status": "in_progress"}).status_code == 409
    assert client.get("/tasks", params={"status": "completed"}).json()["total"] == 1
    assert client.delete("/promise-types/send_documents").status_code == 409
    assert client.patch("/tasks/missing", json={"status": "in_progress"}).status_code == 404


def test_separate_promises_and_multiple_calls(client, fake, call_json, promise_json):
    callback = ExtractedPromise(
        **{
            **promise_json,
            "type_code": "callback",
            "title": "Перезвонить",
            "quote": "Завтра перезвоню.",
            "deadline_text": "Завтра",
            "deadline_at": None,
        }
    )
    fake.result.calls[0].promises.append(callback)
    second_call = {**call_json, "id": "call-2", "operator_id": 8}
    fake.result.calls.append(CallExtraction(call_id="call-2", promises=[callback]))
    result = client.post("/calls", json=[call_json, second_call]).json()
    assert result["tasks_created"] == 3
    assert len({task["id"] for task in result["tasks"]}) == 3
    assert client.get("/tasks", params={"operator_id": 7}).json()["total"] == 2
    page = client.get("/tasks", params={"limit": 1, "offset": 2}).json()
    assert page["total"] == 3 and len(page["items"]) == 1


def test_webhook_conflict(client, call_json):
    client.post("/calls", json=[call_json])
    assert client.post("/calls", json=[{**call_json, "operator_id": 8}]).status_code == 409


def test_upstream_failure_is_retryable_and_atomic(client, fake, call_json):
    original = fake.extract

    async def fail(*args):
        raise LLMError("Cannot connect to LM Studio")

    fake.extract = fail
    assert client.post("/calls", json=[call_json]).status_code == 502
    assert client.get("/tasks").json()["total"] == 0
    fake.extract = original
    assert client.post("/calls", json=[call_json]).json()["processed"] == 1


def test_no_promises_and_dynamic_types(client, fake, call_json):
    new_type = {
        "code": "refund",
        "name": "Возврат",
        "description": "Оператор обещает вернуть деньги клиенту",
    }
    client.post("/promise-types", json=new_type)
    fake.result = Extraction(calls=[{"call_id": "call-1", "promises": []}])
    result = client.post("/calls", json=[call_json]).json()
    assert result["processed"] == 1 and result["tasks_created"] == 0
    assert "refund" in {t.code for t in fake.requests[0][1]}


def test_due_range_and_overdue(client, call_json, monkeypatch):
    task = client.post("/calls", json=[call_json]).json()["tasks"][0]
    exact = timestamp(NOW + timedelta(hours=1))
    assert client.get("/tasks", params={"due_from": exact, "due_to": exact}).json()["total"] == 1
    assert client.get("/tasks", params={"due_to": timestamp(NOW)}).json()["total"] == 0
    monkeypatch.setattr("app.storage.utcnow", lambda: NOW + timedelta(hours=2))
    assert client.get("/tasks", params={"status": "overdue"}).json()["total"] == 1
    assert client.patch(f"/tasks/{task['id']}", json={"status": "completed"}).status_code == 200


@pytest.mark.parametrize("payload", [[], [{}], "bad", [{"id": "bad"}]])
def test_invalid_webhook_payload(client, payload):
    assert client.post("/calls", json=payload).status_code == 422


@pytest.mark.parametrize(
    "params",
    [
        {"status": "bad"},
        {"operator_id": 0},
        {"limit": 0},
        {"limit": 501},
        {"offset": -1},
        {"due_from": "2026-10-29T10:00:00"},
        {"due_from": "2026-10-30T00:00:00Z", "due_to": "2026-10-29T00:00:00Z"},
    ],
)
def test_invalid_filters(client, params):
    assert client.get("/tasks", params=params).status_code == 422


@pytest.mark.parametrize("status", ["created", "overdue", "unknown"])
def test_status_cannot_be_set_manually(client, status):
    assert client.patch("/tasks/any", json={"status": status}).status_code == 422


def test_openapi_contains_only_required_business_routes(client):
    schema = client.get("/openapi.json").json()
    assert set(schema["paths"]) == {
        "/calls",
        "/tasks",
        "/tasks/{task_id}",
        "/promise-types",
        "/promise-types/{code}",
    }
