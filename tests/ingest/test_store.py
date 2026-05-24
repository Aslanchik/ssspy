from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from ssspy.ingest.decode import decode_request
from ssspy.ingest.normalize import normalize_request
from ssspy.ingest.store import store_spans

from ._fixtures import load_full_otlp_body


async def _normalized_full_spans() -> list:
    body = load_full_otlp_body()
    req = decode_request(body, "application/x-protobuf")
    spans, rejected = normalize_request(req)
    assert rejected == []
    return spans


async def test_full_trace_inserts_all_rows(db_engine: AsyncEngine) -> None:
    spans = await _normalized_full_spans()
    expected_count = len(spans)

    async with db_engine.connect() as conn:
        async with conn.begin():
            await store_spans(conn, spans)

        traces = (await conn.execute(text("SELECT trace_id, root_span_id, start_time, end_time, span_count FROM traces"))).all()
        assert len(traces) == 1
        t = traces[0]
        assert t.span_count == expected_count
        assert t.end_time is not None  # root present → end_time populated
        assert t.root_span_id is not None

        rows = (await conn.execute(text("SELECT COUNT(*) FROM spans"))).scalar_one()
        assert rows == expected_count

        # The root span has no parent.
        root = (
            await conn.execute(
                text("SELECT span_id FROM spans WHERE parent_span_id IS NULL")
            )
        ).all()
        assert len(root) == 1

        # Tool span has tool_name promoted and ssspy.tool.input preserved.
        tool = (
            await conn.execute(
                text(
                    "SELECT tool_name, attributes->>'ssspy.tool.input' AS ti "
                    "FROM spans WHERE tool_name IS NOT NULL ORDER BY name LIMIT 1"
                )
            )
        ).one()
        assert tool.tool_name is not None
        assert tool.ti is not None and "{" in tool.ti


async def _insert(engine: AsyncEngine, spans: list) -> None:
    async with engine.begin() as conn:
        await store_spans(conn, spans)


async def test_reinsert_same_trace_is_noop(db_engine: AsyncEngine) -> None:
    spans = await _normalized_full_spans()
    await _insert(db_engine, spans)
    async with db_engine.connect() as conn:
        before = (
            await conn.execute(
                text("SELECT span_count, start_time, end_time FROM traces LIMIT 1")
            )
        ).one()
    await _insert(db_engine, spans)
    async with db_engine.connect() as conn:
        after = (
            await conn.execute(
                text("SELECT span_count, start_time, end_time FROM traces LIMIT 1")
            )
        ).one()
        spans_after = (await conn.execute(text("SELECT COUNT(*) FROM spans"))).scalar_one()
    assert spans_after == len(spans)
    assert before == after


async def test_children_before_root_then_root(db_engine: AsyncEngine) -> None:
    spans = await _normalized_full_spans()
    root = next(s for s in spans if s.parent_span_id is None)
    children = [s for s in spans if s.parent_span_id is not None]

    await _insert(db_engine, children)
    async with db_engine.connect() as conn:
        row = (
            await conn.execute(
                text("SELECT root_span_id, end_time, span_count, start_time FROM traces LIMIT 1")
            )
        ).one()
    assert row.root_span_id is None
    assert row.end_time is None
    assert row.span_count == len(children)
    min_child_start = min(s.start_time for s in children)
    assert row.start_time == min_child_start

    await _insert(db_engine, [root])
    async with db_engine.connect() as conn:
        row2 = (
            await conn.execute(
                text("SELECT root_span_id, end_time, start_time, span_count FROM traces LIMIT 1")
            )
        ).one()
    assert row2.root_span_id == root.span_id
    assert row2.end_time == root.end_time
    assert row2.start_time == min(root.start_time, min_child_start)
    assert row2.span_count == len(spans)


async def test_orphan_child_persists(db_engine: AsyncEngine) -> None:
    spans = await _normalized_full_spans()
    # Take one leaf and pretend its parent is unknown to ingest.
    leaf = next(s for s in spans if s.tool_name is not None)
    leaf = leaf.model_copy(update={"parent_span_id": b"\xde\xad\xbe\xef\xde\xad\xbe\xef"})

    async with db_engine.connect() as conn:
        async with conn.begin():
            await store_spans(conn, [leaf])
        rows = (await conn.execute(text("SELECT COUNT(*) FROM spans"))).scalar_one()
        assert rows == 1
