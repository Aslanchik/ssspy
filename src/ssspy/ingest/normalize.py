"""Flatten OTLP envelopes into ``CanonicalSpan`` rows.

This module knows nothing about specific agents: hot fields are the GenAI
semconv set defined by the OTel spec. Agent-specific attributes (the
``ssspy.*`` namespace) flow into the JSONB ``attributes`` column unchanged.
"""
from __future__ import annotations

import base64
from datetime import datetime, timezone
from typing import Any, Callable

from ssspy.models.otlp import OtlpAnyValue, OtlpRequest, OtlpSpan
from ssspy.models.span import CanonicalSpan, RejectedSpan

# GenAI semconv v1.41.0 hot attributes lifted into typed columns.
HOT_ATTRS: dict[str, tuple[str, Callable[[Any], Any]]] = {
    "gen_ai.operation.name": ("gen_ai_operation", str),
    "gen_ai.provider.name": ("gen_ai_provider", str),
    "gen_ai.request.model": ("gen_ai_request_model", str),
    "gen_ai.response.model": ("gen_ai_response_model", str),
    "gen_ai.response.id": ("gen_ai_response_id", str),
    "gen_ai.usage.input_tokens": ("input_tokens", int),
    "gen_ai.usage.output_tokens": ("output_tokens", int),
    "gen_ai.usage.cache_read.input_tokens": ("cache_read_tokens", int),
    "gen_ai.usage.cache_creation.input_tokens": ("cache_creation_tokens", int),
    "gen_ai.tool.name": ("tool_name", str),
    "gen_ai.tool.call.id": ("tool_call_id", str),
}

_STATUS_BY_CODE = {0: "Unset", 1: "Ok", 2: "Error"}


def flatten_value(av: OtlpAnyValue) -> Any:
    """Convert an ``OtlpAnyValue`` to a native Python value."""
    if av.string_value is not None:
        return av.string_value
    if av.bool_value is not None:
        return av.bool_value
    if av.int_value is not None:
        return int(av.int_value)
    if av.double_value is not None:
        return av.double_value
    if av.bytes_value is not None:
        return base64.b64decode(av.bytes_value)
    if av.array_value is not None:
        return [flatten_value(v) for v in av.array_value.values]
    if av.kvlist_value is not None:
        return {kv.key: flatten_value(kv.value) for kv in av.kvlist_value.values}
    return None


def _nano_to_dt(ns: str | int) -> datetime:
    n = int(ns)
    return datetime.fromtimestamp(n / 1e9, tz=timezone.utc)


def _decode_id(value: str | bytes | None) -> bytes | None:
    if value is None:
        return None
    if isinstance(value, bytes):
        return value or None
    if value == "":
        return None
    return bytes.fromhex(value)


def normalize_span(otlp: OtlpSpan, resource: dict[str, Any]) -> CanonicalSpan:
    attrs: dict[str, Any] = {kv.key: flatten_value(kv.value) for kv in otlp.attributes}

    hot: dict[str, Any] = {}
    for key, (field, caster) in HOT_ATTRS.items():
        if key in attrs:
            try:
                hot[field] = caster(attrs.pop(key))
            except (TypeError, ValueError):
                # Couldn't cast — leave it in the JSONB so the data isn't lost.
                attrs[key] = attrs.get(key)

    if "gen_ai.response.finish_reasons" in attrs:
        fr = attrs.pop("gen_ai.response.finish_reasons")
        hot["gen_ai_finish_reason"] = (
            fr[0] if isinstance(fr, list) and fr else None
        )

    trace_id = _decode_id(otlp.trace_id)
    span_id = _decode_id(otlp.span_id)
    parent = _decode_id(otlp.parent_span_id)
    if trace_id is None or span_id is None:
        raise _MissingField("trace_id" if trace_id is None else "span_id")
    if not otlp.name:
        raise _MissingField("name")
    if not otlp.start_time_unix_nano or otlp.start_time_unix_nano == "0":
        raise _MissingField("start_time")
    if not otlp.end_time_unix_nano or otlp.end_time_unix_nano == "0":
        raise _MissingField("end_time")

    status_code = (
        _STATUS_BY_CODE.get(otlp.status.code, "Unset") if otlp.status else "Unset"
    )
    status_message = otlp.status.message if otlp.status else None

    return CanonicalSpan(
        trace_id=trace_id,
        span_id=span_id,
        parent_span_id=parent,
        name=otlp.name,
        kind=otlp.kind,
        start_time=_nano_to_dt(otlp.start_time_unix_nano),
        end_time=_nano_to_dt(otlp.end_time_unix_nano),
        status_code=status_code,
        status_message=status_message,
        attributes=attrs,
        resource=resource,
        **hot,
    )


class _MissingField(Exception):
    def __init__(self, field: str) -> None:
        self.field = field
        super().__init__(f"missing required field: {field}")


def normalize_request(
    req: OtlpRequest,
) -> tuple[list[CanonicalSpan], list[RejectedSpan]]:
    spans: list[CanonicalSpan] = []
    rejected: list[RejectedSpan] = []
    for rs in req.resource_spans:
        resource: dict[str, Any] = {}
        if rs.resource is not None:
            resource = {kv.key: flatten_value(kv.value) for kv in rs.resource.attributes}
        for ss in rs.scope_spans:
            for sp in ss.spans:
                try:
                    spans.append(normalize_span(sp, resource))
                except _MissingField as e:
                    rejected.append(
                        RejectedSpan(span_id_hex=sp.span_id or None, reason=str(e))
                    )
                except Exception as e:
                    rejected.append(
                        RejectedSpan(
                            span_id_hex=sp.span_id or None,
                            reason=f"normalize failed: {e}",
                        )
                    )
    return spans, rejected
