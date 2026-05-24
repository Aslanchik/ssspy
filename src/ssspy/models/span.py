"""Canonical Span and Trace models used internally after normalization."""
from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

StatusCode = Literal["Ok", "Error", "Unset"]


class CanonicalSpan(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    trace_id: bytes
    span_id: bytes
    parent_span_id: bytes | None = None
    name: str
    kind: int = 0
    start_time: datetime
    end_time: datetime
    status_code: StatusCode = "Unset"
    status_message: str | None = None

    # GenAI semconv hot fields, populated when present.
    gen_ai_operation: str | None = None
    gen_ai_provider: str | None = None
    gen_ai_request_model: str | None = None
    gen_ai_response_model: str | None = None
    gen_ai_response_id: str | None = None
    gen_ai_finish_reason: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    cache_read_tokens: int | None = None
    cache_creation_tokens: int | None = None
    tool_name: str | None = None
    tool_call_id: str | None = None

    attributes: dict[str, Any] = Field(default_factory=dict)
    resource: dict[str, Any] = Field(default_factory=dict)

    @property
    def duration_ms(self) -> int:
        return int((self.end_time - self.start_time).total_seconds() * 1000)


class Trace(BaseModel):
    """A trace view model — header row plus its root span when available."""

    model_config = ConfigDict(populate_by_name=True)

    trace_id: bytes
    start_time: datetime
    end_time: datetime | None = None
    span_count: int = 0
    root_span_id: bytes | None = None
    root: CanonicalSpan | None = None


class RejectedSpan(BaseModel):
    """A span that failed normalization (missing required fields)."""

    span_id_hex: str | None = None
    reason: str
