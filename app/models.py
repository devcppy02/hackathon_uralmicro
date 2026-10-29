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


CallBatch = Annotated[list[CallInput], Field(min_length=1, max_length=100)]


class PromiseTypeCreate(StrictModel):
    code: str = Field(min_length=2, max_length=64, pattern=r"^[a-z][a-z0-9_]+$")
    name: str = Field(min_length=1, max_length=200)
    description: str = Field(min_length=10, max_length=2000)


class PromiseType(PromiseTypeCreate):
    created_at: AwareDatetime


class TaskStatus(StrEnum):
    CREATED = "created"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    OVERDUE = "overdue"


class CallStatus(StrEnum):
    PENDING = "pending"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"


class TaskUpdate(StrictModel):
    status: Literal[TaskStatus.IN_PROGRESS, TaskStatus.COMPLETED]


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
    deadline_source: Literal["explicit", "default_24h"]
    due_at: AwareDatetime
    status: TaskStatus
    created_at: AwareDatetime
    updated_at: AwareDatetime
    completed_at: AwareDatetime | None


class TaskPage(StrictModel):
    items: list[Task]
    total: int
    limit: int
    offset: int


class CallDetail(CallInput):
    status: CallStatus
    attempts: int
    error: str | None
    created_at: AwareDatetime
    updated_at: AwareDatetime
    tasks: list[Task]


class CallSummary(StrictModel):
    id: str
    operator_id: int
    status: CallStatus
    attempts: int
    error: str | None
    created_at: AwareDatetime
    updated_at: AwareDatetime


class CallPage(StrictModel):
    items: list[CallSummary]
    total: int
    limit: int
    offset: int


class AcceptedCall(StrictModel):
    id: str
    duplicate: bool
    status: CallStatus


class BatchAccepted(StrictModel):
    accepted: int
    duplicates: int
    calls: list[AcceptedCall]


class ExtractedPromise(StrictModel):
    type_code: str = Field(min_length=2, max_length=64)
    title: str = Field(min_length=1, max_length=500)
    quote: str = Field(min_length=1, max_length=10000)
    replica_index: int = Field(ge=0, strict=True)
    deadline_text: str | None = Field(max_length=500)
    deadline_at: AwareDatetime | None


class Extraction(StrictModel):
    promises: list[ExtractedPromise] = Field(max_length=50)
