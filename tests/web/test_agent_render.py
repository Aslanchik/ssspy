from __future__ import annotations

import httpx
from sqlalchemy.ext.asyncio import AsyncEngine

from ssspy.ingest.decode import decode_request
from ssspy.ingest.normalize import normalize_request
from ssspy.ingest.store import store_spans

from tests.ingest._fixtures import (
    TRACE_HEX,
    full_envelope,
    load_full_otlp_body,
)


async def _seed(engine: AsyncEngine, body: bytes) -> None:
    spans, _ = normalize_request(decode_request(body, "application/x-protobuf"))
    async with engine.begin() as conn:
        await store_spans(conn, spans)


async def test_go_phish_partial_renders(
    app_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    await _seed(db_engine, load_full_otlp_body())
    r = await app_client.get(f"/traces/{TRACE_HEX}")
    assert r.status_code == 200
    body = r.text
    assert "https://example.com" in body
    assert "Phase 1: fetch" in body
    assert "Phase 2: hypothesis" in body
    assert "Phase 3: enrichment" in body
    assert "Phase 4: synthesis" in body
    assert '"brand"' in body


async def test_partial_absent_when_agent_not_go_phish(
    app_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    env = full_envelope()
    # Drop the ssspy.agent.name attribute on the root span.
    root = env.resource_spans[0].scope_spans[0].spans[0]
    del root.attributes[2]  # 0=id, 1=target_url, 2=agent_name
    await _seed(db_engine, env.SerializeToString())
    r = await app_client.get(f"/traces/{TRACE_HEX}")
    assert r.status_code == 200
    assert "agent-go-phish" not in r.text
    # Generic span tree still rendered.
    assert "ssspy.phase.fetch" in r.text


async def test_partial_handles_malformed_outcome(
    app_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    env = full_envelope()
    # Set hypothesis outcome to a non-JSON string.
    spans = env.resource_spans[0].scope_spans[0].spans
    hypothesis_span = next(s for s in spans if s.name == "ssspy.phase.hypothesis")
    for attr in hypothesis_span.attributes:
        if attr.key == "ssspy.investigation.outcome":
            attr.value.string_value = "not-actually-json"
    await _seed(db_engine, env.SerializeToString())
    r = await app_client.get(f"/traces/{TRACE_HEX}")
    assert r.status_code == 200
    assert "not-actually-json" in r.text
