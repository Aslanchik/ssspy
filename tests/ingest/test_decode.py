from __future__ import annotations

import pytest
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import (
    ExportTraceServiceRequest,
)
from opentelemetry.proto.common.v1.common_pb2 import AnyValue, KeyValue
from opentelemetry.proto.resource.v1.resource_pb2 import Resource
from opentelemetry.proto.trace.v1.trace_pb2 import ResourceSpans, ScopeSpans, Span, Status

from ssspy.ingest.decode import DecodeError, decode_request


def _envelope() -> ExportTraceServiceRequest:
    span = Span(
        trace_id=bytes.fromhex("0123456789abcdef0123456789abcdef"),
        span_id=bytes.fromhex("1122334455667788"),
        name="root",
        kind=Span.SpanKind.SPAN_KIND_INTERNAL,
        start_time_unix_nano=1_700_000_000_000_000_000,
        end_time_unix_nano=1_700_000_001_000_000_000,
        status=Status(code=Status.STATUS_CODE_OK),
        attributes=[
            KeyValue(key="ssspy.agent.name", value=AnyValue(string_value="go-phish")),
        ],
    )
    return ExportTraceServiceRequest(
        resource_spans=[
            ResourceSpans(
                resource=Resource(),
                scope_spans=[ScopeSpans(spans=[span])],
            )
        ]
    )


def test_protobuf_body_decodes() -> None:
    body = _envelope().SerializeToString()
    req = decode_request(body, "application/x-protobuf")
    assert len(req.resource_spans) == 1
    assert req.resource_spans[0].scope_spans[0].spans[0].name == "root"


def test_json_body_decodes_to_same_shape() -> None:
    proto_req = decode_request(_envelope().SerializeToString(), "application/x-protobuf")
    # Round-trip via JSON: dump the validated model and reparse.
    json_body = proto_req.model_dump_json(by_alias=True)
    json_req = decode_request(json_body.encode(), "application/json")
    assert json_req == proto_req


def test_unsupported_content_type_raises() -> None:
    with pytest.raises(DecodeError, match="unsupported content-type"):
        decode_request(b"{}", "text/plain")


def test_malformed_protobuf_raises_decode_error() -> None:
    with pytest.raises(DecodeError, match="protobuf"):
        decode_request(b"\xff\xff\xff\xffnot a protobuf", "application/x-protobuf")


def test_malformed_json_raises_decode_error() -> None:
    with pytest.raises(DecodeError):
        decode_request(b"{not json}", "application/json")
