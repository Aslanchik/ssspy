from __future__ import annotations

from datetime import datetime, timezone

from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import (
    ExportTraceServiceRequest,
)
from opentelemetry.proto.common.v1.common_pb2 import (
    AnyValue,
    ArrayValue,
    KeyValue,
    KeyValueList,
)
from opentelemetry.proto.resource.v1.resource_pb2 import Resource
from opentelemetry.proto.trace.v1.trace_pb2 import ResourceSpans, ScopeSpans, Span, Status

from ssspy.models.otlp import OtlpAnyValue, OtlpRequest
from ssspy.models.span import CanonicalSpan


def test_otlp_any_value_variants() -> None:
    assert OtlpAnyValue.model_validate({"stringValue": "x"}).string_value == "x"
    assert OtlpAnyValue.model_validate({"boolValue": True}).bool_value is True
    iv = OtlpAnyValue.model_validate({"intValue": "42"})
    assert iv.int_value == "42"  # OTLP JSON encodes int64 as string
    assert OtlpAnyValue.model_validate({"doubleValue": 1.5}).double_value == 1.5
    av = OtlpAnyValue.model_validate(
        {"arrayValue": {"values": [{"stringValue": "a"}, {"stringValue": "b"}]}}
    )
    assert av.array_value is not None
    assert [v.string_value for v in av.array_value.values] == ["a", "b"]
    kv = OtlpAnyValue.model_validate(
        {"kvlistValue": {"values": [{"key": "k", "value": {"stringValue": "v"}}]}}
    )
    assert kv.kvlist_value is not None
    assert kv.kvlist_value.values[0].key == "k"


def test_otlp_request_from_protobuf_round_trips() -> None:
    span = Span(
        trace_id=bytes.fromhex("0123456789abcdef0123456789abcdef"),
        span_id=bytes.fromhex("1122334455667788"),
        name="chat claude-sonnet-4-5",
        kind=Span.SpanKind.SPAN_KIND_CLIENT,
        start_time_unix_nano=1_700_000_000_000_000_000,
        end_time_unix_nano=1_700_000_001_000_000_000,
        status=Status(code=Status.STATUS_CODE_OK),
        attributes=[
            KeyValue(key="gen_ai.request.model", value=AnyValue(string_value="claude-sonnet-4-5")),
            KeyValue(key="gen_ai.usage.input_tokens", value=AnyValue(int_value=1234)),
            KeyValue(
                key="gen_ai.response.finish_reasons",
                value=AnyValue(
                    array_value=ArrayValue(values=[AnyValue(string_value="end_turn")])
                ),
            ),
            KeyValue(
                key="nested",
                value=AnyValue(
                    kvlist_value=KeyValueList(
                        values=[KeyValue(key="k", value=AnyValue(bool_value=True))]
                    )
                ),
            ),
        ],
    )
    resource = Resource(
        attributes=[KeyValue(key="service.name", value=AnyValue(string_value="go-phish"))]
    )
    envelope = ExportTraceServiceRequest(
        resource_spans=[
            ResourceSpans(resource=resource, scope_spans=[ScopeSpans(spans=[span])])
        ]
    )

    req = OtlpRequest.from_protobuf(envelope)
    assert len(req.resource_spans) == 1
    rs = req.resource_spans[0]
    assert rs.resource is not None
    assert rs.resource.attributes[0].key == "service.name"
    assert rs.resource.attributes[0].value.string_value == "go-phish"
    assert len(rs.scope_spans) == 1
    sp = rs.scope_spans[0].spans[0]
    assert sp.name == "chat claude-sonnet-4-5"
    assert sp.trace_id == "0123456789abcdef0123456789abcdef"
    assert sp.span_id == "1122334455667788"
    # protobuf int64 surfaces as string after MessageToDict
    assert sp.start_time_unix_nano == "1700000000000000000"
    # Hot attr present
    model_kv = next(kv for kv in sp.attributes if kv.key == "gen_ai.request.model")
    assert model_kv.value.string_value == "claude-sonnet-4-5"
    tokens_kv = next(kv for kv in sp.attributes if kv.key == "gen_ai.usage.input_tokens")
    assert tokens_kv.value.int_value == "1234"


def test_canonical_span_round_trips_all_hot_fields() -> None:
    now = datetime(2026, 5, 24, 12, 0, 0, tzinfo=timezone.utc)
    cs = CanonicalSpan(
        trace_id=b"\x00" * 16,
        span_id=b"\x00" * 8,
        parent_span_id=None,
        name="root",
        kind=1,
        start_time=now,
        end_time=now,
        status_code="Ok",
        status_message="done",
        gen_ai_operation="chat",
        gen_ai_provider="anthropic",
        gen_ai_request_model="claude-sonnet-4-5",
        gen_ai_response_model="claude-sonnet-4-5",
        gen_ai_response_id="msg_01",
        gen_ai_finish_reason="end_turn",
        input_tokens=10,
        output_tokens=20,
        cache_read_tokens=5,
        cache_creation_tokens=2,
        tool_name="fetch",
        tool_call_id="toolu_01",
        attributes={"ssspy.investigation.id": "uuid-123"},
        resource={"service.name": "go-phish"},
    )
    dumped = cs.model_dump()
    restored = CanonicalSpan.model_validate(dumped)
    assert restored == cs
    assert restored.duration_ms == 0
