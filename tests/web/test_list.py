from __future__ import annotations

import httpx
from sqlalchemy.ext.asyncio import AsyncEngine

from ssspy.ingest.decode import decode_request
from ssspy.ingest.normalize import normalize_request
from ssspy.ingest.store import store_spans

from tests.ingest._fixtures import (
    full_envelope,
    load_full_otlp_body,
    skip_llm_envelope,
)


async def _seed(engine: AsyncEngine, body: bytes) -> None:
    req = decode_request(body, "application/x-protobuf")
    spans, _ = normalize_request(req)
    async with engine.begin() as conn:
        await store_spans(conn, spans)


async def test_empty_db(app_client: httpx.AsyncClient) -> None:
    r = await app_client.get("/")
    assert r.status_code == 200
    assert "ssspy" in r.text
    # No data rows in tbody.
    assert "<tbody>\n    \n  </tbody>" in r.text or "<tbody></tbody>" in r.text or "</tbody>" in r.text


async def test_filters(app_client: httpx.AsyncClient, db_engine: AsyncEngine) -> None:
    # Seed two traces from different agents.
    await _seed(db_engine, full_envelope().SerializeToString())

    env2 = skip_llm_envelope()
    env2.resource_spans[0].scope_spans[0].spans[0].attributes[2].value.string_value = "other-agent"
    await _seed(db_engine, env2.SerializeToString())

    r_all = await app_client.get("/")
    assert r_all.status_code == 200
    assert "go-phish" in r_all.text
    assert "other-agent" in r_all.text

    r_go = await app_client.get("/?agent=go-phish")
    assert "go-phish" in r_go.text
    assert "other-agent" not in r_go.text

    r_url = await app_client.get("/?url=example")
    assert "go-phish" in r_url.text


async def test_incomplete_trace_renders_badge(
    app_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    body = load_full_otlp_body()
    req = decode_request(body, "application/x-protobuf")
    spans, _ = normalize_request(req)
    children = [s for s in spans if s.parent_span_id is not None]
    async with db_engine.begin() as conn:
        await store_spans(conn, children)

    r = await app_client.get("/")
    assert r.status_code == 200
    assert "(incomplete)" in r.text
