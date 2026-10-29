from datetime import timedelta

import pytest

from app.deadlines import resolve_deadline
from app.models import ExtractedPromise


@pytest.mark.parametrize(
    "text, minutes",
    [
        ("через час", 60),
        ("через 2 часа", 120),
        ("в течение двух часов", 120),
        ("через полчаса", 30),
        ("в течение получаса", 30),
        ("через полтора часа", 90),
        ("через 1,5 часа", 90),
        ("через 15 минут", 15),
        ("в ближайшие тридцать минут", 30),
        ("через день", 1440),
        ("через 2 дня", 2880),
        ("через сутки", 1440),
        ("через неделю", 10080),
        ("в течение 2 недель", 20160),
    ],
)
def test_relative_deadline_is_based_on_call_end(call, promise_json, text, minutes):
    promise = ExtractedPromise(
        **{**promise_json, "deadline_text": text, "deadline_at": "2099-01-01T00:00:00Z"}
    )
    due, source = resolve_deadline(promise, call.ended_at)
    assert due == call.ended_at + timedelta(minutes=minutes)
    assert source == "explicit"


@pytest.mark.parametrize(
    "text", [None, "скоро", "позже", "как только выясню", "на днях", "непонятно"]
)
def test_default_24_hours(call, promise_json, text):
    promise = ExtractedPromise(**{**promise_json, "deadline_text": text})
    assert resolve_deadline(promise, call.ended_at) == (
        call.ended_at + timedelta(hours=24),
        "default_24h",
    )


def test_explicit_calendar_deadline(call, promise_json):
    promise = ExtractedPromise(
        **{
            **promise_json,
            "deadline_text": "завтра в 10:00",
            "deadline_at": "2026-10-30T10:00:00+05:00",
        }
    )
    assert resolve_deadline(promise, call.ended_at) == (promise.deadline_at, "explicit")


def test_past_explicit_deadline_not_moved_forward(call, promise_json):
    promise = ExtractedPromise(
        **{
            **promise_json,
            "deadline_text": "сегодня в 09:00",
            "deadline_at": "2026-10-29T09:00:00+05:00",
        }
    )
    assert resolve_deadline(promise, call.ended_at)[0] < call.ended_at


def test_overflow_falls_back_to_default(call, promise_json):
    promise = ExtractedPromise(**{**promise_json, "deadline_text": "через " + "9" * 400 + " дней"})
    assert resolve_deadline(promise, call.ended_at)[1] == "default_24h"
