"""Pydantic v2 models for the OTLP HTTP v1 trace wire format.

Only the subset we actually persist or route on is modelled. Both protobuf
and JSON paths converge on ``OtlpRequest`` after parsing.
"""
from __future__ import annotations

import base64
from typing import Any

from google.protobuf.json_format import MessageToDict


def _b64_to_hex(s: str) -> str:
    return base64.b64decode(s).hex()


def _hexify_span_ids(req: dict[str, Any]) -> None:
    """Rewrite trace_id / span_id / parent_span_id from base64 to hex in-place."""
    for rs in req.get("resourceSpans", []) or []:
        for ss in rs.get("scopeSpans", []) or []:
            for sp in ss.get("spans", []) or []:
                for key in ("traceId", "spanId", "parentSpanId"):
                    val = sp.get(key)
                    if isinstance(val, str) and val:
                        sp[key] = _b64_to_hex(val)
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import (
    ExportTraceServiceRequest,
)
from pydantic import BaseModel, ConfigDict, Field, field_validator


_SPAN_KIND_BY_NAME = {
    "SPAN_KIND_UNSPECIFIED": 0,
    "SPAN_KIND_INTERNAL": 1,
    "SPAN_KIND_SERVER": 2,
    "SPAN_KIND_CLIENT": 3,
    "SPAN_KIND_PRODUCER": 4,
    "SPAN_KIND_CONSUMER": 5,
}

_STATUS_CODE_BY_NAME = {
    "STATUS_CODE_UNSET": 0,
    "STATUS_CODE_OK": 1,
    "STATUS_CODE_ERROR": 2,
}


class OtlpArrayValue(BaseModel):
    model_config = ConfigDict(populate_by_name=True)
    values: list["OtlpAnyValue"] = []


class OtlpKvList(BaseModel):
    model_config = ConfigDict(populate_by_name=True)
    values: list["OtlpKeyValue"] = []


class OtlpAnyValue(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    string_value: str | None = Field(None, alias="stringValue")
    bool_value: bool | None = Field(None, alias="boolValue")
    int_value: str | None = Field(None, alias="intValue")
    double_value: float | None = Field(None, alias="doubleValue")
    array_value: OtlpArrayValue | None = Field(None, alias="arrayValue")
    kvlist_value: OtlpKvList | None = Field(None, alias="kvlistValue")
    bytes_value: str | None = Field(None, alias="bytesValue")


class OtlpKeyValue(BaseModel):
    model_config = ConfigDict(populate_by_name=True)
    key: str
    value: OtlpAnyValue


class OtlpStatus(BaseModel):
    model_config = ConfigDict(populate_by_name=True)
    code: int = 0
    message: str | None = None

    @field_validator("code", mode="before")
    @classmethod
    def _coerce_code(cls, v: object) -> object:
        if isinstance(v, str):
            return _STATUS_CODE_BY_NAME.get(v, 0)
        return v


class OtlpSpan(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    trace_id: str = Field(alias="traceId")
    span_id: str = Field(alias="spanId")
    parent_span_id: str | None = Field(None, alias="parentSpanId")
    name: str = ""
    kind: int = 0
    start_time_unix_nano: str = Field("0", alias="startTimeUnixNano")
    end_time_unix_nano: str = Field("0", alias="endTimeUnixNano")
    attributes: list[OtlpKeyValue] = []
    status: OtlpStatus | None = None

    @field_validator("kind", mode="before")
    @classmethod
    def _coerce_kind(cls, v: object) -> object:
        if isinstance(v, str):
            return _SPAN_KIND_BY_NAME.get(v, 0)
        return v


class OtlpResource(BaseModel):
    model_config = ConfigDict(populate_by_name=True)
    attributes: list[OtlpKeyValue] = []


class OtlpScopeSpans(BaseModel):
    model_config = ConfigDict(populate_by_name=True)
    spans: list[OtlpSpan] = []


class OtlpResourceSpans(BaseModel):
    model_config = ConfigDict(populate_by_name=True)
    resource: OtlpResource | None = None
    scope_spans: list[OtlpScopeSpans] = Field(default_factory=list, alias="scopeSpans")


class OtlpRequest(BaseModel):
    """Top-level ``ExportTraceServiceRequest`` shape.

    Use ``OtlpRequest.from_protobuf`` to convert a parsed protobuf message;
    use ``OtlpRequest.model_validate_json`` for the JSON wire format.
    """

    model_config = ConfigDict(populate_by_name=True)

    resource_spans: list[OtlpResourceSpans] = Field(
        default_factory=list, alias="resourceSpans"
    )

    @classmethod
    def from_protobuf(cls, envelope: ExportTraceServiceRequest) -> "OtlpRequest":
        d: dict[str, Any] = MessageToDict(
            envelope, preserving_proto_field_name=False, use_integers_for_enums=True
        )
        # MessageToDict base64-encodes bytes fields; OTLP JSON spec uses hex
        # for trace_id/span_id/parent_span_id. Renormalize so the JSON and
        # protobuf paths converge on the same value shape.
        _hexify_span_ids(d)
        return cls.model_validate(d)


OtlpAnyValue.model_rebuild()
OtlpArrayValue.model_rebuild()
OtlpKvList.model_rebuild()
