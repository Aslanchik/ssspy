"""Read-side queries for the ``traces`` table."""
from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from ssspy.models.span import CanonicalSpan, Trace


class TraceListRow(BaseModel):
    trace_id: bytes
    start_time: datetime
    end_time: datetime | None = None
    span_count: int
    root_name: str | None = None
    status_code: str | None = None
    duration_ms: int | None = None
    agent_name: str | None = None
    agent_version: str | None = None
    target_url: str | None = None


_LIST_SQL = text(
    """
    SELECT
        t.trace_id, t.start_time, t.end_time, t.span_count,
        s.name AS root_name, s.status_code, s.duration_ms,
        s.attributes->>'ssspy.agent.name'               AS agent_name,
        s.attributes->>'ssspy.agent.version'            AS agent_version,
        s.attributes->>'ssspy.investigation.target_url' AS target_url
    FROM traces t
    LEFT JOIN spans s ON s.span_id = t.root_span_id
    WHERE  (CAST(:agent AS text)   IS NULL OR s.attributes->>'ssspy.agent.name' = CAST(:agent AS text))
       AND (CAST(:version AS text) IS NULL OR s.attributes->>'ssspy.agent.version' = CAST(:version AS text))
       AND (CAST(:url AS text)     IS NULL OR s.attributes->>'ssspy.investigation.target_url' ILIKE '%' || CAST(:url AS text) || '%')
       AND (CAST(:since AS timestamptz) IS NULL OR t.start_time >= CAST(:since AS timestamptz))
       AND (CAST(:until AS timestamptz) IS NULL OR t.start_time <  CAST(:until AS timestamptz))
    ORDER BY t.start_time DESC
    LIMIT :size OFFSET :offset
    """
)


async def list_traces(
    conn: AsyncConnection,
    *,
    page: int = 1,
    size: int = 50,
    agent: str | None = None,
    version: str | None = None,
    url: str | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
) -> list[TraceListRow]:
    offset = max(0, (max(page, 1) - 1) * size)
    rows = (
        await conn.execute(
            _LIST_SQL,
            {
                "agent": agent,
                "version": version,
                "url": url,
                "since": since,
                "until": until,
                "size": size,
                "offset": offset,
            },
        )
    ).mappings().all()
    return [TraceListRow(**dict(r)) for r in rows]


_GET_TRACE_SQL = text(
    """
    SELECT
        t.trace_id, t.root_span_id, t.start_time, t.end_time, t.span_count
    FROM traces t WHERE t.trace_id = :tid
    """
)


_GET_ROOT_SPAN_SQL = text(
    """
    SELECT * FROM spans WHERE span_id = :sid
    """
)


def _row_to_span(row: dict[str, object]) -> CanonicalSpan:
    return CanonicalSpan(
        trace_id=row["trace_id"],
        span_id=row["span_id"],
        parent_span_id=row.get("parent_span_id"),
        name=row["name"],
        kind=row["kind"],
        start_time=row["start_time"],
        end_time=row["end_time"],
        status_code=row["status_code"],
        status_message=row.get("status_message"),
        gen_ai_operation=row.get("gen_ai_operation"),
        gen_ai_provider=row.get("gen_ai_provider"),
        gen_ai_request_model=row.get("gen_ai_request_model"),
        gen_ai_response_model=row.get("gen_ai_response_model"),
        gen_ai_response_id=row.get("gen_ai_response_id"),
        gen_ai_finish_reason=row.get("gen_ai_finish_reason"),
        input_tokens=row.get("input_tokens"),
        output_tokens=row.get("output_tokens"),
        cache_read_tokens=row.get("cache_read_tokens"),
        cache_creation_tokens=row.get("cache_creation_tokens"),
        tool_name=row.get("tool_name"),
        tool_call_id=row.get("tool_call_id"),
        attributes=row.get("attributes") or {},
        resource=row.get("resource") or {},
    )


async def get_trace(conn: AsyncConnection, trace_id: bytes) -> Trace | None:
    row = (await conn.execute(_GET_TRACE_SQL, {"tid": trace_id})).mappings().first()
    if row is None:
        return None
    root: CanonicalSpan | None = None
    if row["root_span_id"] is not None:
        rs = (
            await conn.execute(_GET_ROOT_SPAN_SQL, {"sid": row["root_span_id"]})
        ).mappings().first()
        if rs is not None:
            root = _row_to_span(dict(rs))
    return Trace(
        trace_id=row["trace_id"],
        start_time=row["start_time"],
        end_time=row["end_time"],
        span_count=row["span_count"],
        root_span_id=row["root_span_id"],
        root=root,
    )
