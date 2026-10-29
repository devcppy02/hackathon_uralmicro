from datetime import UTC, datetime
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)


def utcnow() -> datetime:
    return datetime.now(UTC)


def timestamp(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="microseconds")


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class Replica(StrictModel):
    speaker: Literal["operator", "client"]
    text: str = Field(min_length=1, max_length=10000)

    @field_validator("speaker", mode="before")
    @classmethod
    def normalize_speaker(cls, value: object) -> object:
        if isinstance(value, str):
            value = value.strip().lower()
            return {"оператор": "operator", "клиент": "client"}.get(value, value)
        return value


class CallInput(StrictModel):
    id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_.:-]+$")
    started_at: AwareDatetime
    ended_at: AwareDatetime
    operator_id: int = Field(gt=0, strict=True)
    client_phone: str = Field(min_length=1, max_length=64)
    transcript: list[Replica] = Field(min_length=1, max_length=500)

    @field_validator("started_at", "ended_at")
    @classmethod
    def normalize_time(cls, value: datetime) -> datetime:
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def valid_call(self) -> "CallInput":
        if self.ended_at < self.started_at:
            raise ValueError("ended_at must be on or after started_at")
        if sum(len(replica.text) for replica in self.transcript) > 40000:
            raise ValueError("Transcript must not exceed 40000 characters")
        return self


CallBatch = Annotated[list[CallInput], Field(min_length=1, max_length=10)]


class PromiseTypeUpdate(StrictModel):
    name: str = Field(min_length=1, max_length=200)
    description: str = Field(min_length=10, max_length=2000)


class PromiseType(PromiseTypeUpdate):
    code: str = Field(min_length=2, max_length=64, pattern=r"^[a-z][a-z0-9_]+$")


class TaskStatus(StrEnum):
    CREATED = "created"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    OVERDUE = "overdue"


class TaskUpdate(StrictModel):
    status: Literal[TaskStatus.IN_PROGRESS, TaskStatus.COMPLETED]
    actor: str = Field(default="operator", min_length=1, max_length=100)
    reason: str = Field(default="", max_length=1000)


class Task(StrictModel):
    id: str
    call_id: str
    operator_id: int
    client_phone: str
    type_code: str
    title: str
    quote: str
    replica_index: int
    deadline_text: str | None
    deadline_source: Literal["explicit", "default_24h", "manual"]
    due_at: AwareDatetime
    status: TaskStatus
    created_at: AwareDatetime
    updated_at: AwareDatetime
    completed_at: AwareDatetime | None
    revision: int = 0


class TaskPage(StrictModel):
    items: list[Task]
    total: int
    limit: int
    offset: int


class CallsResult(StrictModel):
    processed: int
    duplicates: int
    tasks_created: int
    tasks: list[Task]
    types_created: list[PromiseType] = Field(default_factory=list)
    completions_suggested: int = 0
    warnings: list[str] = Field(default_factory=list)


class ExtractedPromise(StrictModel):
    type_code: str = Field(min_length=2, max_length=64)
    title: str = Field(min_length=1, max_length=500)
    quote: str = Field(min_length=1, max_length=10000)
    replica_index: int = Field(ge=0, strict=True)
    deadline_text: str | None = Field(max_length=500)
    deadline_at: AwareDatetime | None


class CallExtraction(StrictModel):
    call_id: str
    promises: list[ExtractedPromise] = Field(max_length=50)


class Extraction(StrictModel):
    calls: list[CallExtraction] = Field(max_length=10)


class AutoTypeExtraction(Extraction):
    new_types: list[PromiseType] = Field(max_length=50)


class TypeMatch(StrictModel):
    existing_type_code: str | None


class TaskCorrection(StrictModel):
    title: str | None = Field(default=None, min_length=1, max_length=500)
    type_code: str | None = Field(default=None, pattern=r"^[a-z][a-z0-9_]+$")
    due_at: AwareDatetime | None = None
    reason: str = Field(min_length=3, max_length=1000)
    actor: str = Field(default="operator", min_length=1, max_length=100)

    @model_validator(mode="after")
    def has_changes(self) -> "TaskCorrection":
        if self.title is None and self.type_code is None and self.due_at is None:
            raise ValueError("Provide title, type_code or due_at")
        return self


class CompletionEvidence(StrictModel):
    task_id: str
    call_id: str
    quote: str = Field(min_length=1, max_length=10000)
    replica_index: int = Field(ge=0, strict=True)
    explanation: str = Field(min_length=1, max_length=1000)


class CompletionExtraction(StrictModel):
    confirmations: list[CompletionEvidence] = Field(max_length=100)


class CompletionDecision(StrictModel):
    accept: bool
    actor: str = Field(default="operator", min_length=1, max_length=100)
    reason: str = Field(default="", max_length=1000)


class TelegramLink(StrictModel):
    operator_id: int = Field(gt=0, strict=True)


def phone_key(phone: str) -> str:
    digits = "".join(char for char in phone if char.isdigit())
    return "7" + digits[1:] if len(digits) == 11 and digits.startswith("8") else digits
