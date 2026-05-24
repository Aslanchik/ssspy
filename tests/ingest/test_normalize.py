from __future__ import annotations

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

from ssspy.ingest.decode import decode_request
from ssspy.ingest.normalize import flatten_value, normalize_request
from ssspy.models.otlp import OtlpAnyValue


def _kv(k: str, **av_kwargs: object) -> KeyValue:
    return KeyValue(key=k, value=AnyValue(**av_kwargs))


def test_flatten_value_all_variants() -> None:
    assert flatten_value(OtlpAnyValue(string_value="x")) == "x"
    assert flatten_value(OtlpAnyValue(bool_value=False)) is False
    assert flatten_value(OtlpAnyValue(int_value="123")) == 123
    assert flatten_value(OtlpAnyValue(double_value=2.5)) == 2.5
    av = OtlpAnyValue.model_validate(
        {"arrayValue": {"values": [{"stringValue": "a"}, {"intValue": "1"}]}}
    )
    assert flatten_value(av) == ["a", 1]
    kv = OtlpAnyValue.model_validate(
        {"kvlistValue": {"values": [
            {"key": "k", "value": {"boolValue": True}},
            {"key": "n", "value": {"intValue": "9"}},
        ]}}
    )
    assert flatten_value(kv) == {"k": True, "n": 9}
    assert flatten_value(OtlpAnyValue(bytes_value="aGk=")) == b"hi"
    assert flatten_value(OtlpAnyValue()) is None


def _envelope_one_span(*attrs: KeyValue, name: str = "chat claude") -> ExportTraceServiceRequest:
    span = Span(
        trace_id=bytes.fromhex("0123456789abcdef0123456789abcdef"),
        span_id=bytes.fromhex("1122334455667788"),
        name=name,
        kind=Span.SpanKind.SPAN_KIND_CLIENT,
        start_time_unix_nano=1_700_000_000_000_000_000,
        end_time_unix_nano=1_700_000_001_000_000_000,
        status=Status(code=Status.STATUS_CODE_UNSET),
        attributes=list(attrs),
    )
    resource = Resource(attributes=[_kv("service.name", string_value="go-phish")])
    return ExportTraceServiceRequest(
        resource_spans=[ResourceSpans(resource=resource, scope_spans=[ScopeSpans(spans=[span])])]
    )


def test_hot_attr_promotion_strips_key_from_attributes() -> None:
    env = _envelope_one_span(
        _kv("gen_ai.request.model", string_value="claude-sonnet-4-5"),
        _kv("ssspy.investigation.id", string_value="uuid-1"),
    )
    req = decode_request(env.SerializeToString(), "application/x-protobuf")
    spans, rejected = normalize_request(req)
    assert rejected == []
    assert len(spans) == 1
    s = spans[0]
    assert s.gen_ai_request_model == "claude-sonnet-4-5"
    assert "gen_ai.request.model" not in s.attributes
    assert s.attributes["ssspy.investigation.id"] == "uuid-1"
    assert s.resource["service.name"] == "go-phish"


def test_finish_reasons_first_element_else_none() -> None:
    env = _envelope_one_span(
        _kv(
            "gen_ai.response.finish_reasons",
            array_value=ArrayValue(values=[AnyValue(string_value="end_turn")]),
        ),
    )
    spans, _ = normalize_request(decode_request(env.SerializeToString(), "application/x-protobuf"))
    assert spans[0].gen_ai_finish_reason == "end_turn"

    env_empty = _envelope_one_span(
        _kv("gen_ai.response.finish_reasons", array_value=ArrayValue(values=[])),
    )
    spans, _ = normalize_request(decode_request(env_empty.SerializeToString(), "application/x-protobuf"))
    assert spans[0].gen_ai_finish_reason is None


def test_empty_parent_span_id_normalizes_to_none() -> None:
    env = _envelope_one_span()
    # No parent_span_id set on the protobuf → ends up empty string in JSON.
    spans, _ = normalize_request(decode_request(env.SerializeToString(), "application/x-protobuf"))
    assert spans[0].parent_span_id is None


def test_missing_required_field_is_rejected_not_raised() -> None:
    # Empty name → rejected.
    env = _envelope_one_span(name="")
    spans, rejected = normalize_request(decode_request(env.SerializeToString(), "application/x-protobuf"))
    assert spans == []
    assert len(rejected) == 1
    assert "name" in rejected[0].reason


def test_ssspy_attrs_round_trip_into_jsonb() -> None:
    env = _envelope_one_span(
        _kv("ssspy.investigation.id", string_value="uuid-2"),
        _kv("ssspy.investigation.phase", string_value="hypothesis"),
        _kv("ssspy.investigation.phase_index", int_value=2),
        _kv("ssspy.tool.input", string_value='{"url":"https://x"}'),
        _kv("ssspy.tool.input.truncated", bool_value=False),
        _kv(
            "nested",
            kvlist_value=KeyValueList(values=[_kv("k", string_value="v")]),
        ),
    )
    spans, _ = normalize_request(decode_request(env.SerializeToString(), "application/x-protobuf"))
    a = spans[0].attributes
    assert a["ssspy.investigation.id"] == "uuid-2"
    assert a["ssspy.investigation.phase"] == "hypothesis"
    assert a["ssspy.investigation.phase_index"] == 2
    assert a["ssspy.tool.input"] == '{"url":"https://x"}'
    assert a["ssspy.tool.input.truncated"] is False
    assert a["nested"] == {"k": "v"}
