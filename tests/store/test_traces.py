from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncEngine

from ssspy.ingest.decode import decode_request
from ssspy.ingest.normalize import normalize_request
from ssspy.ingest.store import store_spans
from ssspy.store.traces import get_trace, list_traces

from tests.ingest._fixtures import load_full_otlp_body


@pytest_asyncio.fixture
async def seeded_engine(db_engine: AsyncEngine) -> AsyncEngine:
    body = load_full_otlp_body()
    spans, _ = normalize_request(decode_request(body, "application/x-protobuf"))
    async with db_engine.begin() as conn:
        await store_spans(conn, spans)
    return db_engine


async def test_list_no_filters_returns_seeded_trace(seeded_engine: AsyncEngine) -> None:
    async with seeded_engine.connect() as conn:
        rows = await list_traces(conn)
    assert len(rows) == 1
    assert rows[0].agent_name == "go-phish"
    assert rows[0].target_url == "https://example.com"
    assert rows[0].span_count == 13


async def test_each_filter(seeded_engine: AsyncEngine) -> None:
    async with seeded_engine.connect() as conn:
        assert len(await list_traces(conn, agent="go-phish")) == 1
        assert len(await list_traces(conn, agent="someone-else")) == 0
        assert len(await list_traces(conn, version="abcdef012345")) == 1
        assert len(await list_traces(conn, version="zzz")) == 0
        assert len(await list_traces(conn, url="example")) == 1
        assert len(await list_traces(conn, url="nope")) == 0
        future = datetime.now(timezone.utc) + timedelta(days=365)
        assert len(await list_traces(conn, since=future)) == 0
        assert len(await list_traces(conn, until=datetime(2000, 1, 1, tzinfo=timezone.utc))) == 0


async def test_pagination(seeded_engine: AsyncEngine) -> None:
    async with seeded_engine.connect() as conn:
        page1 = await list_traces(conn, page=1, size=50)
        page2 = await list_traces(conn, page=2, size=50)
    assert len(page1) == 1
    assert page2 == []


async def test_get_trace_known_and_unknown(seeded_engine: AsyncEngine) -> None:
    async with seeded_engine.connect() as conn:
        rows = await list_traces(conn)
        tid = rows[0].trace_id
        trace = await get_trace(conn, tid)
        assert trace is not None
        assert trace.span_count == 13
        assert trace.root is not None
        assert trace.root.attributes.get("ssspy.agent.name") == "go-phish"

        missing = await get_trace(conn, b"\x00" * 16)
        assert missing is None
