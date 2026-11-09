from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


MAX_QUESTION_CHARS = 4000


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CloudEnableRequest(StrictModel):
    workspace_id: str = Field(min_length=1, max_length=100)
    connection_id: str = Field(min_length=1, max_length=100)
    connection_version: str | None = Field(default=None, max_length=100)


class CloudEnableResponse(StrictModel):
    enabled: bool
    workspace_id: str
    connection_id: str
    connection_version: str
    connection_name: str
    protocol: str
    base_url: str
    model: str


class AgentTaskCreate(StrictModel):
    workspace_id: str = Field(min_length=1, max_length=100)
    question: str = Field(min_length=1, max_length=MAX_QUESTION_CHARS)
    request_key: str = Field(min_length=8, max_length=100)
    connection_id: str | None = Field(default=None, max_length=100)
    connection_version: str | None = Field(default=None, max_length=100)

    @field_validator("question")
    @classmethod
    def clean_question(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("请输入问题")
        return value.strip()


class AgentEvent(StrictModel):
    at: datetime
    kind: str
    message: str


class SourceRecord(StrictModel):
    source_id: str
    workspace_id: str
    relative_path: str
    location: str
    version: str
    excerpt: str


class AgentTaskView(StrictModel):
    task_id: str
    request_key: str
    workspace_id: str
    connection_id: str
    connection_name: str
    connection_version: str
    base_url: str
    model: str
    status: Literal["queued", "running", "succeeded", "failed", "limit_reached"]
    created_at: datetime
    updated_at: datetime
    events: list[AgentEvent]
    answer: str | None = None
    citations: list[SourceRecord] = Field(default_factory=list)
    error_code: str | None = None
    error_message: str | None = None


class ConnectionTestResult(StrictModel):
    passed: bool
    kind: Literal["chat", "tool"]
    connection_id: str
    connection_version: str
    message: str
    response_text: str | None = None
