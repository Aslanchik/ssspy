from __future__ import annotations

import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncEngine

from ssspy.ingest.decode import decode_request
from ssspy.ingest.normalize import normalize_request
from ssspy.ingest.store import store_spans
from ssspy.store.spans import list_spans_for_trace

from tests.ingest._fixtures import load_full_otlp_body


@pytest_asyncio.fixture
async def seeded_engine(db_engine: AsyncEngine) -> AsyncEngine:
    body = load_full_otlp_body()
    spans, _ = normalize_request(decode_request(body, "application/x-protobuf"))
    async with db_engine.begin() as conn:
        await store_spans(conn, spans)
    return db_engine


async def test_list_spans_for_trace_chronological(seeded_engine: AsyncEngine) -> None:
    body = load_full_otlp_body()
    norm, _ = normalize_request(decode_request(body, "application/x-protobuf"))
    tid = norm[0].trace_id

    async with seeded_engine.connect() as conn:
        spans = await list_spans_for_trace(conn, tid)

    assert len(spans) == 13
    starts = [s.start_time for s in spans]
    assert starts == sorted(starts)
    root = next(s for s in spans if s.parent_span_id is None)
    assert root.attributes.get("ssspy.investigation.target_url") == "https://example.com"
    # Tool spans round-trip with their JSONB intact.
    tool_spans = [s for s in spans if s.tool_name]
    assert len(tool_spans) == 4
    assert any(
        "ssspy.tool.input" in s.attributes and "ssspy.tool.output" in s.attributes
        for s in tool_spans
    )
