import pytest
from pydantic import ValidationError

from app.config import Settings
from app.models import CallInput, Replica


def test_default_settings():
    settings = Settings(_env_file=None)
    assert settings.llm_model == "qwen/qwen3.8-27b"
    assert "lm-studio" not in repr(settings.llm_api_key)


@pytest.mark.parametrize("url", ["http://localhost:1234", "http://localhost:1234/v1/"])
def test_base_url_normalization(url):
    assert Settings(_env_file=None, llm_base_url=url).llm_base_url == "http://localhost:1234/v1"


@pytest.mark.parametrize(
    "url", ["file:///tmp", "bad", "http://x?a=1", "http://a:b@x", "http://x#f"]
)
def test_invalid_base_url(url):
    with pytest.raises(ValidationError):
        Settings(_env_file=None, llm_base_url=url)


@pytest.mark.parametrize("zone", ["Bogus/Zone", "../UTC"])
def test_invalid_timezone(zone):
    with pytest.raises(ValidationError):
        Settings(_env_file=None, app_timezone=zone)


def test_speaker_normalization():
    assert Replica(speaker=" Оператор ", text="Текст").speaker == "operator"
    assert Replica(speaker="Клиент", text="Текст").speaker == "client"
    with pytest.raises(ValidationError):
        Replica(speaker=1, text="Текст")


def call_data():
    return {
        "id": "call-1",
        "started_at": "2026-10-29T10:00:00+05:00",
        "ended_at": "2026-10-29T10:05:00+05:00",
        "operator_id": 1,
        "client_phone": "+70000000000",
        "transcript": [{"speaker": "operator", "text": "Перезвоню завтра"}],
    }


def test_call_time_validation():
    data = call_data()
    assert CallInput(**data).ended_at.hour == 5
    data["ended_at"] = "2026-10-29T09:00:00+05:00"
    with pytest.raises(ValidationError):
        CallInput(**data)
    data["ended_at"] = "2026-10-29T10:05:00"
    with pytest.raises(ValidationError):
        CallInput(**data)


def test_transcript_limit():
    data = call_data()
    data["transcript"] = [{"speaker": "operator", "text": "x" * 10000}] * 5
    with pytest.raises(ValidationError, match="40000"):
        CallInput(**data)
