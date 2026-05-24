from __future__ import annotations

import httpx
from sqlalchemy.ext.asyncio import AsyncEngine

from ssspy.ingest.decode import decode_request
from ssspy.ingest.normalize import normalize_request
from ssspy.ingest.store import store_spans

from tests.ingest._fixtures import TRACE_HEX, load_full_otlp_body


async def _seed_full(engine: AsyncEngine) -> None:
    body = load_full_otlp_body()
    spans, _ = normalize_request(decode_request(body, "application/x-protobuf"))
    async with engine.begin() as conn:
        await store_spans(conn, spans)


async def test_detail_renders_full_tree(
    app_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    await _seed_full(db_engine)
    r = await app_client.get(f"/traces/{TRACE_HEX}")
    assert r.status_code == 200, r.text
    body = r.text
    # Every phase span name should appear.
    for ph in ("fetch", "hypothesis", "enrichment", "synthesis"):
        assert f"ssspy.phase.{ph}" in body
    # 13 span <details> blocks.
    assert body.count('<details class="span"') == 13
    # Tool spans show their tool name.
    assert "dns_lookup" in body
    # Long-payload key is wrapped in a (closed) details.
    assert "ssspy.tool.input" in body


async def test_detail_unknown_404(app_client: httpx.AsyncClient) -> None:
    r = await app_client.get("/traces/" + "0" * 32)
    assert r.status_code == 404


async def test_detail_incomplete_trace_renders(
    app_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    body = load_full_otlp_body()
    spans, _ = normalize_request(decode_request(body, "application/x-protobuf"))
    children = [s for s in spans if s.parent_span_id is not None]
    async with db_engine.begin() as conn:
        await store_spans(conn, children)
    r = await app_client.get(f"/traces/{TRACE_HEX}")
    assert r.status_code == 200
    assert "(incomplete)" in r.text
    # All 12 children appear.
    assert r.text.count('<details class="span"') == 12


async def test_long_payload_pretty_printed(
    app_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    await _seed_full(db_engine)
    r = await app_client.get(f"/traces/{TRACE_HEX}")
    body = r.text
    # The hypothesis outcome contains "brand": "Example" — pretty-printed should
    # have newline + indent inside the <pre>.
    assert "ssspy.investigation.outcome" in body
    assert '"brand"' in body
    # Closed by default — <details><summary>(payload)</summary> appears.
    assert "<details><summary>(payload)</summary>" in body
