"""Read-side queries for the ``spans`` table."""
from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from ssspy.models.span import CanonicalSpan
from ssspy.store.traces import _row_to_span

_LIST_FOR_TRACE_SQL = text(
    """
    SELECT * FROM spans WHERE trace_id = :tid ORDER BY start_time ASC, span_id ASC
    """
)


async def list_spans_for_trace(
    conn: AsyncConnection, trace_id: bytes
) -> list[CanonicalSpan]:
    rows = (await conn.execute(_LIST_FOR_TRACE_SQL, {"tid": trace_id})).mappings().all()
    return [_row_to_span(dict(r)) for r in rows]
