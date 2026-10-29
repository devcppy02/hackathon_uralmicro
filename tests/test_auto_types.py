import asyncio
import json
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest
from fastapi.testclient import TestClient

from app.api import create_app
from app.config import Settings
from app.llm import LLMError, QwenExtractor
from app.models import (
    AutoTypeExtraction,
    CallExtraction,
    CallInput,
    Extraction,
    PromiseType,
    TypeMatch,
)
from app.storage import DEFAULT_TYPES, ConflictError, Storage
from tests.conftest import FakeExtractor
from tests.test_llm import llm_response

ESCALATION = PromiseType(
    code="forward_request",
    name="Передать обращение ответственным",
    description="Оператор обещает передать обращение руководству или профильному отделу.",
)


@pytest.fixture
def auto_extraction(extraction):
    result = AutoTypeExtraction(**extraction.model_dump(), new_types=[ESCALATION])
    result.calls[0].promises[0].type_code = ESCALATION.code
    return result


async def extract_auto(call, result, matches=(), types=DEFAULT_TYPES):
    responses = iter([result, *matches])
    requests = []

    def handler(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200, json=llm_response(next(responses)))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        extractor = QwenExtractor(client, Settings(_env_file=None, auto_create_promise_types=True))
        return await extractor.extract([call], types), requests


async def test_new_category_requires_separate_novelty_check(call, auto_extraction):
    result, requests = await extract_auto(
        call, auto_extraction, [TypeMatch(existing_type_code=None)]
    )
    assert result.new_types == [ESCALATION]
    assert result.calls[0].promises[0].type_code == ESCALATION.code
    assert len(requests) == 2
    schema = requests[0]["response_format"]["json_schema"]["schema"]
    assert schema["properties"]["calls"]["minItems"] == 1
    assert schema["properties"]["calls"]["maxItems"] == 1
    assert schema["$defs"]["CallExtraction"]["properties"]["call_id"]["enum"] == [call.id]
    check = json.loads(requests[1]["messages"][1]["content"])
    assert check["candidate_type"] == ESCALATION.model_dump()
    assert {t["code"] for t in check["promise_types"]} == {t.code for t in DEFAULT_TYPES}
    assert check["promises"][0]["quote"] == auto_extraction.calls[0].promises[0].quote


async def test_review_reuses_existing_type_instead_of_document_subtype(call, auto_extraction):
    auto_extraction.new_types = [
        PromiseType(
            code="send_contract", name="Отправить договор", description="Выслать договор клиенту"
        )
    ]
    auto_extraction.calls[0].promises[0].type_code = "send_contract"
    result, requests = await extract_auto(
        call, auto_extraction, [TypeMatch(existing_type_code="send_documents")]
    )
    assert len(requests) == 2
    assert result.new_types == []
    assert result.calls[0].promises[0].type_code == "send_documents"


async def test_existing_categories_need_no_review(call, extraction):
    result, requests = await extract_auto(
        call, AutoTypeExtraction(**extraction.model_dump(), new_types=[])
    )
    assert len(requests) == 1
    assert result.new_types == []
    assert result.calls == extraction.calls


async def test_auto_schema_requires_every_call_in_batch(call, extraction):
    second = call.model_copy(update={"id": "call-2"})
    result = AutoTypeExtraction(
        calls=[*extraction.calls, CallExtraction(call_id=second.id, promises=[])], new_types=[]
    )

    def handler(request):
        schema = json.loads(request.content)["response_format"]["json_schema"]["schema"]
        assert schema["properties"]["calls"]["minItems"] == 2
        assert schema["properties"]["calls"]["maxItems"] == 2
        assert schema["$defs"]["CallExtraction"]["properties"]["call_id"]["enum"] == [
            call.id,
            second.id,
        ]
        return httpx.Response(200, json=llm_response(result))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        extractor = QwenExtractor(client, Settings(_env_file=None, auto_create_promise_types=True))
        assert await extractor.extract([call, second], DEFAULT_TYPES) == result


async def test_same_name_reuses_existing_type_without_extra_request(call, auto_extraction):
    auto_extraction.new_types[0] = ESCALATION.model_copy(
        update={"name": DEFAULT_TYPES[1].name.upper()}
    )
    result, requests = await extract_auto(call, auto_extraction)
    assert len(requests) == 1
    assert result.new_types == []
    assert result.calls[0].promises[0].type_code == "send_documents"


async def test_later_proposals_reuse_category_approved_in_same_batch(call, auto_extraction):
    second = ESCALATION.model_copy(update={"code": "to_manager", "name": "Передать руководству"})
    auto_extraction.new_types.append(second)
    auto_extraction.calls[0].promises.append(
        auto_extraction.calls[0].promises[0].model_copy(update={"type_code": second.code})
    )
    result, requests = await extract_auto(
        call,
        auto_extraction,
        [TypeMatch(existing_type_code=None), TypeMatch(existing_type_code=ESCALATION.code)],
    )
    assert len(requests) == 3
    reviewed_types = json.loads(requests[2]["messages"][1]["content"])["promise_types"]
    assert ESCALATION.model_dump() in reviewed_types
    assert result.new_types == [ESCALATION]
    assert {p.type_code for p in result.calls[0].promises} == {ESCALATION.code}


async def test_auto_typing_can_start_with_empty_catalog(call, auto_extraction):
    result, requests = await extract_auto(call, auto_extraction, types=[])
    assert result.new_types == [ESCALATION]
    assert len(requests) == 1


async def test_client_only_does_not_call_model_in_auto_mode(call):
    call.transcript = call.transcript[:1]
    result, requests = await extract_auto(call, None)
    assert requests == []
    assert result.calls[0].promises == []


@pytest.mark.parametrize(
    "mutation, message",
    [
        (lambda r: r.new_types.append(r.new_types[0]), "duplicate or existing"),
        (lambda r: r.new_types.append(DEFAULT_TYPES[0]), "duplicate or existing"),
        (lambda r: r.new_types.clear(), "declare and use"),
        (lambda r: r.calls[0].promises.clear(), "declare and use"),
        (lambda r: setattr(r.calls[0].promises[0], "type_code", "unknown"), "declare and use"),
        (lambda r: setattr(r.calls[0].promises[0], "quote", "Выдумано"), "operator replica"),
    ],
)
async def test_invalid_proposals_fail_before_review(call, auto_extraction, mutation, message):
    mutation(auto_extraction)
    with pytest.raises(LLMError, match=message):
        await extract_auto(call, auto_extraction)


async def test_type_review_cannot_invent_a_different_code(call, auto_extraction):
    with pytest.raises(LLMError, match="unknown promise type"):
        await extract_auto(call, auto_extraction, [TypeMatch(existing_type_code="not_in_catalog")])


def test_auto_types_saved_once_and_available_after_mode_disabled(
    tmp_path, call_json, auto_extraction
):
    path = tmp_path / "api.sqlite3"
    settings = Settings(_env_file=None, database_path=path, auto_create_promise_types=True)
    fake = FakeExtractor(auto_extraction)
    with TestClient(create_app(settings, extractor=fake, start_bot=False)) as client:
        result = client.post("/calls", json=[call_json]).json()
        assert result["types_created"] == [ESCALATION.model_dump()]
        assert result["tasks_created"] == 1
        assert result["tasks"][0]["type_code"] == ESCALATION.code
        repeated = client.post("/calls", json=[call_json]).json()
        assert repeated["types_created"] == [] and repeated["tasks_created"] == 0
        assert len(fake.requests) == 1
        assert client.get(f"/promise-types/{ESCALATION.code}").json() == ESCALATION.model_dump()

    fake.result = Extraction(
        calls=[auto_extraction.calls[0].model_copy(update={"call_id": "call-2"})]
    )
    settings = Settings(_env_file=None, database_path=path)
    with TestClient(create_app(settings, extractor=fake, start_bot=False)) as client:
        result = client.post("/calls", json=[{**call_json, "id": "call-2"}]).json()
        assert result["types_created"] == [] and result["tasks_created"] == 1
        assert ESCALATION in fake.requests[-1][1]
        update = {"name": "Передать запрос", "description": ESCALATION.description}
        assert client.put(f"/promise-types/{ESCALATION.code}", json=update).status_code == 200
        assert client.delete(f"/promise-types/{ESCALATION.code}").status_code == 409


def test_disabled_mode_never_saves_proposed_types(client, fake, auto_extraction, call_json):
    fake.result = auto_extraction
    assert client.post("/calls", json=[call_json]).status_code == 502
    assert len(client.get("/promise-types").json()) == 3
    assert client.get("/tasks").json()["total"] == 0


def test_new_types_rollback_together_with_tasks(storage, call, auto_extraction):
    invalid = auto_extraction.calls[0].promises[0].model_copy(update={"type_code": "unknown"})
    auto_extraction.calls[0].promises.append(invalid)
    with pytest.raises(ConflictError, match="types changed"):
        storage.save_calls([call], auto_extraction, [ESCALATION])
    assert storage.get_type(ESCALATION.code) is None
    assert storage.list_tasks().total == 0
    assert storage.new_calls([call]) == [call]


def test_type_code_collision_does_not_overwrite_existing_type(storage, call, auto_extraction):
    storage.create_type(
        ESCALATION.model_copy(update={"description": "Другое значение этой категории"})
    )
    with pytest.raises(ConflictError, match="types changed"):
        storage.save_calls([call], auto_extraction, [ESCALATION])
    assert storage.get_type(ESCALATION.code).description == "Другое значение этой категории"
    assert storage.new_calls([call]) == [call]


def test_concurrent_storage_writes_deduplicate_type_and_tasks(storage, call, auto_extraction):
    with ThreadPoolExecutor(max_workers=3) as pool:
        results = list(
            pool.map(lambda _: storage.save_calls([call], auto_extraction, [ESCALATION]), range(3))
        )
    assert sum(len(r.types_created) for r in results) == 1
    assert sum(r.tasks_created for r in results) == 1


def test_existing_identical_type_is_reused_at_commit(storage, call, auto_extraction):
    storage.create_type(ESCALATION)
    result = storage.save_calls([call], auto_extraction, [ESCALATION])
    assert result.types_created == [] and result.tasks_created == 1


def test_unused_type_is_not_saved_for_duplicate_call(storage, call, extraction):
    storage.save_calls([call], extraction)
    result = storage.save_calls([call], Extraction(calls=[]), [ESCALATION])
    assert result.types_created == []
    assert storage.get_type(ESCALATION.code) is None


def test_concurrent_webhooks_see_latest_catalog(tmp_path, call_json, auto_extraction):
    class Extractor:
        async def extract(self, calls, types):
            # Даём запросам пересечься: без блокировки оба увидели бы старый справочник.
            await asyncio.sleep(0.05)
            known = ESCALATION in types
            return AutoTypeExtraction(
                calls=[
                    CallExtraction(call_id=calls[0].id, promises=auto_extraction.calls[0].promises)
                ],
                new_types=[] if known else [ESCALATION],
            )

    settings = Settings(
        _env_file=None, database_path=tmp_path / "parallel.sqlite3", auto_create_promise_types=True
    )
    with TestClient(create_app(settings, extractor=Extractor(), start_bot=False)) as client:
        with ThreadPoolExecutor(max_workers=2) as pool:
            responses = list(
                pool.map(
                    lambda i: client.post("/calls", json=[{**call_json, "id": f"parallel-{i}"}]),
                    range(2),
                )
            )
        assert all(r.status_code == 200 for r in responses)
        assert sum(len(r.json()["types_created"]) for r in responses) == 1
        assert sum(r.json()["tasks_created"] for r in responses) == 2
        assert len(client.get("/promise-types").json()) == 4


def test_api_rolls_back_on_novelty_review_failure(
    tmp_path, call_json, auto_extraction, monkeypatch
):
    unavailable = True

    async def request(self, schema, prompt, data):
        if schema is AutoTypeExtraction:
            return auto_extraction
        if unavailable:
            raise LLMError("LM Studio timed out; retry the webhook")
        return TypeMatch(existing_type_code=None)

    monkeypatch.setattr(QwenExtractor, "request", request)
    settings = Settings(
        _env_file=None, database_path=tmp_path / "retry.sqlite3", auto_create_promise_types=True
    )
    with TestClient(create_app(settings, start_bot=False)) as client:
        assert client.post("/calls", json=[call_json]).status_code == 502
        assert len(client.get("/promise-types").json()) == 3
        assert client.get("/tasks").json()["total"] == 0
        call = CallInput(**call_json)
        assert Storage(settings.database_path).new_calls([call]) == [call]
        unavailable = False
        result = client.post("/calls", json=[call_json]).json()
        assert result["processed"] == 1 and result["types_created"] == [ESCALATION.model_dump()]
