"""Synthetic fixture builders.

These mirror the go-phish CONTRACT.md trace shapes closely enough to exercise
ingest. T-12 replaces the loaders below with captured binary fixtures.
"""
from __future__ import annotations

import os
from typing import Iterable

from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import (
    ExportTraceServiceRequest,
)
from opentelemetry.proto.common.v1.common_pb2 import AnyValue, ArrayValue, KeyValue
from opentelemetry.proto.resource.v1.resource_pb2 import Resource
from opentelemetry.proto.trace.v1.trace_pb2 import (
    ResourceSpans,
    ScopeSpans,
    Span,
    Status,
)

FIXTURES_DIR = os.path.dirname(__file__) + "/../fixtures"

TRACE_HEX = "0123456789abcdef0123456789abcdef"
ROOT_SPAN_HEX = "11" * 8


def _kv(key: str, **av_kwargs: object) -> KeyValue:
    return KeyValue(key=key, value=AnyValue(**av_kwargs))


def _span(
    span_id_hex: str,
    parent_hex: str | None,
    name: str,
    start_ns: int,
    end_ns: int,
    attrs: Iterable[KeyValue] = (),
    kind: int = Span.SpanKind.SPAN_KIND_INTERNAL,
    status: int = Status.STATUS_CODE_UNSET,
) -> Span:
    sp = Span(
        trace_id=bytes.fromhex(TRACE_HEX),
        span_id=bytes.fromhex(span_id_hex),
        name=name,
        kind=kind,
        start_time_unix_nano=start_ns,
        end_time_unix_nano=end_ns,
        status=Status(code=status),
        attributes=list(attrs),
    )
    if parent_hex:
        sp.parent_span_id = bytes.fromhex(parent_hex)
    return sp


def full_envelope() -> ExportTraceServiceRequest:
    """Build a 13-span trace shaped like a go-phish full investigation.

    root → 4 phases; phase2 has 1 chat (hypothesis); phase3 has 1 chat + 4 tools;
    phase4 has 1 chat. Total = 1 + 4 + 1 + 1 + 4 + 1 = ... no: 1 + 4 + (1) + (1+4) + (1) = 12.
    Add one extra chat span under phase2 for tool-discovery to reach 13. The exact
    distribution doesn't matter for ingest tests — only the structural shape does.
    """
    base = 1_700_000_000_000_000_000

    def hex_(n: int) -> str:
        return f"{n:016x}"

    root = _span(
        ROOT_SPAN_HEX,
        None,
        "ssspy.investigation",
        base,
        base + 5_000_000_000,
        [
            _kv("ssspy.investigation.id", string_value="11111111-2222-3333-4444-555555555555"),
            _kv("ssspy.investigation.target_url", string_value="https://example.com"),
            _kv("ssspy.agent.name", string_value="go-phish"),
            _kv("ssspy.agent.version", string_value="abcdef012345"),
        ],
        status=Status.STATUS_CODE_OK,
    )

    phases = []
    phase_ids = ["20", "21", "22", "23"]
    phase_names = ["fetch", "hypothesis", "enrichment", "synthesis"]
    for i, (sid, ph) in enumerate(zip(phase_ids, phase_names), start=1):
        attrs = [
            _kv("ssspy.investigation.phase", string_value=ph),
            _kv("ssspy.investigation.phase_index", int_value=i),
        ]
        if ph in ("hypothesis", "synthesis"):
            attrs.append(
                _kv(
                    "ssspy.investigation.outcome",
                    string_value='{"brand":"Example","confidence":"high"}',
                )
            )
        phases.append(
            _span(
                hex_(int(sid, 16)),
                ROOT_SPAN_HEX,
                f"ssspy.phase.{ph}",
                base + i * 100_000_000,
                base + i * 1_000_000_000,
                attrs,
            )
        )

    # Children under each phase.
    hypothesis_id = hex_(int("21", 16))
    enrichment_id = hex_(int("22", 16))
    synthesis_id = hex_(int("23", 16))

    chat_attrs_base = [
        _kv("gen_ai.operation.name", string_value="chat"),
        _kv("gen_ai.provider.name", string_value="anthropic"),
        _kv("gen_ai.request.model", string_value="claude-sonnet-4-5"),
        _kv("gen_ai.response.model", string_value="claude-sonnet-4-5"),
        _kv("gen_ai.response.id", string_value="msg_01"),
        _kv("gen_ai.usage.input_tokens", int_value=1000),
        _kv("gen_ai.usage.output_tokens", int_value=200),
        _kv("gen_ai.usage.cache_read.input_tokens", int_value=100),
        _kv("gen_ai.usage.cache_creation.input_tokens", int_value=50),
        _kv(
            "gen_ai.response.finish_reasons",
            array_value=ArrayValue(values=[AnyValue(string_value="end_turn")]),
        ),
    ]

    children: list[Span] = []
    # Phase 2 (hypothesis): 2 chat spans (one with screenshot hash)
    children.append(
        _span(
            hex_(0x30),
            hypothesis_id,
            "chat claude-sonnet-4-5",
            base + 300_000_000,
            base + 400_000_000,
            chat_attrs_base
            + [
                _kv("ssspy.screenshot.content_type", string_value="image/png"),
                _kv("ssspy.screenshot.size_bytes", int_value=12345),
                _kv("ssspy.screenshot.sha256", string_value="a" * 64),
            ],
            kind=Span.SpanKind.SPAN_KIND_CLIENT,
        )
    )
    children.append(
        _span(
            hex_(0x31),
            hypothesis_id,
            "chat claude-sonnet-4-5",
            base + 400_000_000,
            base + 450_000_000,
            chat_attrs_base,
            kind=Span.SpanKind.SPAN_KIND_CLIENT,
        )
    )
    # Phase 3 (enrichment): 1 chat + 4 tool calls
    children.append(
        _span(
            hex_(0x40),
            enrichment_id,
            "chat claude-sonnet-4-5",
            base + 500_000_000,
            base + 600_000_000,
            chat_attrs_base,
            kind=Span.SpanKind.SPAN_KIND_CLIENT,
        )
    )
    for i, tool in enumerate(["dns_lookup", "whois", "tls_cert", "reverse_ip"], start=1):
        children.append(
            _span(
                hex_(0x50 + i),
                enrichment_id,
                f"execute_tool {tool}",
                base + 600_000_000 + i * 10_000_000,
                base + 700_000_000 + i * 10_000_000,
                [
                    _kv("gen_ai.tool.name", string_value=tool),
                    _kv("gen_ai.tool.call.id", string_value=f"toolu_{i:02d}"),
                    _kv("ssspy.tool.input", string_value=f'{{"q":"{tool}"}}'),
                    _kv(
                        "ssspy.tool.output",
                        string_value=f'{{"result":"data for {tool}"}}',
                    ),
                ],
            )
        )
    # Phase 4 (synthesis): 1 chat
    children.append(
        _span(
            hex_(0x70),
            synthesis_id,
            "chat claude-sonnet-4-5",
            base + 800_000_000,
            base + 900_000_000,
            chat_attrs_base,
            kind=Span.SpanKind.SPAN_KIND_CLIENT,
        )
    )

    all_spans = [root] + phases + children
    assert len(all_spans) == 13, f"expected 13 spans, got {len(all_spans)}"

    resource = Resource(
        attributes=[
            _kv("service.name", string_value="go-phish"),
            _kv("telemetry.sdk.language", string_value="go"),
        ]
    )
    return ExportTraceServiceRequest(
        resource_spans=[
            ResourceSpans(resource=resource, scope_spans=[ScopeSpans(spans=all_spans)])
        ]
    )


def skip_llm_envelope() -> ExportTraceServiceRequest:
    """5-span trace: root + 4 phases, no chat/tool spans (mimics --skip-llm)."""
    base = 1_700_000_010_000_000_000

    def hex_(n: int) -> str:
        return f"{n:016x}"

    root = _span(
        ROOT_SPAN_HEX,
        None,
        "ssspy.investigation",
        base,
        base + 1_000_000_000,
        [
            _kv("ssspy.investigation.id", string_value="aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"),
            _kv("ssspy.investigation.target_url", string_value="https://example.com"),
            _kv("ssspy.agent.name", string_value="go-phish"),
            _kv("ssspy.agent.version", string_value="dev"),
        ],
        status=Status.STATUS_CODE_OK,
    )
    phases = []
    for i, ph in enumerate(["fetch", "hypothesis", "enrichment", "synthesis"], start=1):
        phases.append(
            _span(
                hex_(0x100 + i),
                ROOT_SPAN_HEX,
                f"ssspy.phase.{ph}",
                base + i * 100_000_000,
                base + (i + 1) * 100_000_000,
                [
                    _kv("ssspy.investigation.phase", string_value=ph),
                    _kv("ssspy.investigation.phase_index", int_value=i),
                ],
            )
        )
    resource = Resource(attributes=[_kv("service.name", string_value="go-phish")])
    return ExportTraceServiceRequest(
        resource_spans=[
            ResourceSpans(
                resource=resource,
                scope_spans=[ScopeSpans(spans=[root] + phases)],
            )
        ]
    )


def load_full_otlp_body() -> bytes:
    """Return the protobuf body for the full trace fixture.

    Prefers ``tests/fixtures/full.otlp.bin`` (captured by T-12) when present,
    otherwise falls back to the synthetic envelope above so earlier tasks can
    run before fixtures are captured.
    """
    captured = os.path.join(FIXTURES_DIR, "full.otlp.bin")
    if os.path.exists(captured):
        with open(captured, "rb") as f:
            return f.read()
    return full_envelope().SerializeToString()


def load_skip_llm_otlp_body() -> bytes:
    captured = os.path.join(FIXTURES_DIR, "skip-llm.otlp.bin")
    if os.path.exists(captured):
        with open(captured, "rb") as f:
            return f.read()
    return skip_llm_envelope().SerializeToString()
