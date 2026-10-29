import json

import httpx
import pytest

from app.config import Settings
from app.llm import LLMError, QwenExtractor
from app.models import Extraction
from app.storage import DEFAULT_TYPES


def llm_response(extraction, *, reasoning=False, finish_reason="stop"):
    field = "reasoning_content" if reasoning else "content"
    return {
        "choices": [
            {
                "finish_reason": finish_reason,
                "message": {
                    "content": "",
                    field: extraction.model_dump_json(),
                },
            }
        ]
    }


async def extract_with(handler, call, types=DEFAULT_TYPES):
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        return await QwenExtractor(client, Settings(_env_file=None)).extract([call], types)


@pytest.mark.parametrize("reasoning", [False, True])
async def test_valid_response_and_request_contract(call, extraction, reasoning):
    def handler(request):
        assert str(request.url) == "http://172.20.10.6:1234/v1/chat/completions"
        assert request.headers["authorization"] == "Bearer lm-studio"
        body = json.loads(request.content)
        assert body["model"] == "qwen/qwen3.8-27b" and body["temperature"] == 0
        assert body["response_format"]["json_schema"]["schema"] == Extraction.model_json_schema()
        data = json.loads(body["messages"][1]["content"])
        assert data["calls"][0]["ended_at"] == "2026-10-29T10:05:00+05:00"
        assert data["calls"][0]["transcript"][1]["index"] == 1
        assert {t["code"] for t in data["promise_types"]} == {t.code for t in DEFAULT_TYPES}
        return httpx.Response(200, json=llm_response(extraction, reasoning=reasoning))

    assert await extract_with(handler, call) == extraction


@pytest.mark.parametrize("reason", ["length", "tool_calls", None])
async def test_unfinished_response_is_rejected(call, extraction, reason):
    with pytest.raises(LLMError, match="unfinished"):
        await extract_with(
            lambda _: httpx.Response(200, json=llm_response(extraction, finish_reason=reason)), call
        )


@pytest.mark.parametrize(
    "error, expected",
    [
        (httpx.ReadTimeout("slow"), "timed out"),
        (httpx.ConnectError("offline"), "connect"),
    ],
)
async def test_network_errors(call, error, expected):
    def handler(request):
        raise error

    with pytest.raises(LLMError, match=expected):
        await extract_with(handler, call)


@pytest.mark.parametrize("status", [400, 429, 500])
async def test_http_errors(call, status):
    with pytest.raises(LLMError, match=f"HTTP {status}"):
        await extract_with(lambda _: httpx.Response(status), call)


@pytest.mark.parametrize(
    "response",
    [
        {},
        {"choices": []},
        {"choices": [{"finish_reason": "stop", "message": {}}]},
        {"choices": [{"finish_reason": "stop", "message": {"content": "not JSON"}}]},
        {"choices": [{"finish_reason": "stop", "message": {"content": "{}"}}]},
    ],
)
async def test_invalid_response(call, response):
    with pytest.raises(LLMError, match="invalid response"):
        await extract_with(lambda _: httpx.Response(200, json=response), call)


async def test_non_json_http_body(call):
    with pytest.raises(LLMError, match="invalid response"):
        await extract_with(lambda _: httpx.Response(200, text="<html>failure</html>"), call)


@pytest.mark.parametrize(
    "mutation, expected",
    [
        (lambda r: r.calls.clear(), "exactly one"),
        (lambda r: r.calls.append(r.calls[0]), "exactly one"),
        (lambda r: setattr(r.calls[0], "call_id", "wrong"), "exactly one"),
        (lambda r: setattr(r.calls[0].promises[0], "type_code", "unknown"), "unknown promise type"),
        (lambda r: setattr(r.calls[0].promises[0], "replica_index", 999), "invalid replica index"),
        (lambda r: setattr(r.calls[0].promises[0], "replica_index", 0), "operator replica"),
        (
            lambda r: setattr(r.calls[0].promises[0], "quote", "Выдуманная цитата"),
            "operator replica",
        ),
        (
            lambda r: setattr(r.calls[0].promises[0], "deadline_text", "в пятницу"),
            "actual transcript",
        ),
    ],
)
async def test_ungrounded_extraction_is_rejected(call, extraction, mutation, expected):
    mutation(extraction)
    with pytest.raises(LLMError, match=expected):
        await extract_with(lambda _: httpx.Response(200, json=llm_response(extraction)), call)


@pytest.mark.parametrize("mode", ["no_types", "client_only"])
async def test_skip_llm_when_no_promises_possible(call, mode):
    types = DEFAULT_TYPES if mode == "client_only" else []
    if mode == "client_only":
        call.transcript = call.transcript[:1]

    def handler(request):
        pytest.fail("LLM should not be called")

    result = await extract_with(handler, call, types)
    assert result.calls[0].call_id == call.id and result.calls[0].promises == []


async def test_no_deadline_and_no_promises_are_valid(call, extraction):
    extraction.calls[0].promises[0].deadline_text = None
    result = await extract_with(lambda _: httpx.Response(200, json=llm_response(extraction)), call)
    assert result.calls[0].promises[0].deadline_text is None
    extraction.calls[0].promises = []
    assert (
        await extract_with(lambda _: httpx.Response(200, json=llm_response(extraction)), call)
    ).calls[0].promises == []
