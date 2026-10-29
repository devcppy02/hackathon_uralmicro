from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    database_path: Path = Path("data/promises.sqlite3")
    app_timezone: str = "Asia/Yekaterinburg"
    llm_base_url: str = "http://172.20.10.6:1234/v1"
    llm_model: str = Field(default="qwen/qwen3.8-27b", min_length=1)
    llm_api_key: SecretStr = SecretStr("lm-studio")
    llm_timeout_seconds: float = Field(default=120, gt=0, le=600)
    llm_max_tokens: int = Field(default=4096, ge=256, le=32768)
    worker_poll_seconds: float = Field(default=1, gt=0)
    max_processing_attempts: int = Field(default=3, ge=1, le=10)
    retry_base_seconds: float = Field(default=10, gt=0)
    overdue_poll_seconds: float = Field(default=15, gt=0)
    api_token: SecretStr = SecretStr("")
    webhook_token: SecretStr = SecretStr("")

    @field_validator("app_timezone")
    @classmethod
    def valid_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError) as error:
            raise ValueError("Unknown IANA timezone") from error
        return value

    @field_validator("llm_base_url")
    @classmethod
    def valid_base_url(cls, value: str) -> str:
        from urllib.parse import urlsplit

        value = value.rstrip("/")
        parsed = urlsplit(value)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("LLM_BASE_URL must be an HTTP(S) URL")
        if parsed.query or parsed.fragment or parsed.username or parsed.password:
            raise ValueError("LLM_BASE_URL must not contain credentials, query or fragment")
        return value if parsed.path.endswith("/v1") else value + "/v1"
