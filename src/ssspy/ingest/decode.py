"""Body → ``OtlpRequest`` for both wire encodings."""
from __future__ import annotations

from google.protobuf.message import DecodeError as ProtoDecodeError
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import (
    ExportTraceServiceRequest,
)
from pydantic import ValidationError

from ssspy.models.otlp import OtlpRequest


class DecodeError(Exception):
    """Raised when an OTLP body cannot be decoded into an OtlpRequest."""


def decode_request(body: bytes, content_type: str) -> OtlpRequest:
    """Parse an OTLP HTTP request body into the unified Pydantic shape.

    Raises ``DecodeError`` for unsupported content types and for malformed bodies.
    """
    ct = (content_type or "").lower().strip()
    if ct == "application/x-protobuf":
        pb = ExportTraceServiceRequest()
        try:
            pb.ParseFromString(body)
        except ProtoDecodeError as e:
            raise DecodeError(f"malformed protobuf: {e}") from e
        try:
            return OtlpRequest.from_protobuf(pb)
        except ValidationError as e:
            raise DecodeError(f"protobuf envelope failed validation: {e.error_count()} errors") from e
    if ct == "application/json":
        try:
            return OtlpRequest.model_validate_json(body)
        except ValidationError as e:
            raise DecodeError(f"malformed OTLP JSON: {e.error_count()} errors") from e
        except ValueError as e:
            raise DecodeError(f"malformed JSON: {e}") from e
    raise DecodeError(f"unsupported content-type: {content_type!r}")
