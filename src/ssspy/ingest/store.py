"""Batch upsert ``CanonicalSpan`` rows into the ``traces`` and ``spans`` tables.

Idempotent at span granularity. Trace aggregates derive only from
newly-inserted spans, so re-POSTing a trace does not double-count.
"""
from __future__ import annotations

import json
from typing import Any, Iterable

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from ssspy.models.span import CanonicalSpan


_STUB_SQL = text(
    """
    INSERT INTO traces (trace_id, start_time)
    SELECT CAST(decode(value, 'hex') AS bytea), now()
    FROM jsonb_array_elements_text(CAST(:trace_ids AS jsonb))
    ON CONFLICT (trace_id) DO NOTHING
    """
)


_INSERT_SQL = text(
    """
    WITH new_spans AS (
        INSERT INTO spans (
            span_id, trace_id, parent_span_id, name, kind, start_time, end_time,
            status_code, status_message,
            gen_ai_operation, gen_ai_provider, gen_ai_request_model, gen_ai_response_model,
            gen_ai_response_id, gen_ai_finish_reason,
            input_tokens, output_tokens, cache_read_tokens, cache_creation_tokens,
            tool_name, tool_call_id,
            attributes, resource
        )
        SELECT
            CAST(decode(span_id_hex, 'hex') AS bytea),
            CAST(decode(trace_id_hex, 'hex') AS bytea),
            CASE WHEN parent_span_id_hex IS NULL OR parent_span_id_hex = ''
                 THEN NULL ELSE CAST(decode(parent_span_id_hex, 'hex') AS bytea) END,
            name, kind, start_time, end_time, status_code, status_message,
            gen_ai_operation, gen_ai_provider, gen_ai_request_model, gen_ai_response_model,
            gen_ai_response_id, gen_ai_finish_reason,
            input_tokens, output_tokens, cache_read_tokens, cache_creation_tokens,
            tool_name, tool_call_id, attributes, resource
        FROM jsonb_to_recordset(CAST(:rows AS jsonb)) AS r(
            span_id_hex TEXT, trace_id_hex TEXT, parent_span_id_hex TEXT,
            name TEXT, kind SMALLINT,
            start_time TIMESTAMPTZ, end_time TIMESTAMPTZ,
            status_code TEXT, status_message TEXT,
            gen_ai_operation TEXT, gen_ai_provider TEXT, gen_ai_request_model TEXT,
            gen_ai_response_model TEXT, gen_ai_response_id TEXT, gen_ai_finish_reason TEXT,
            input_tokens INT, output_tokens INT, cache_read_tokens INT, cache_creation_tokens INT,
            tool_name TEXT, tool_call_id TEXT,
            attributes JSONB, resource JSONB
        )
        ON CONFLICT (span_id) DO NOTHING
        RETURNING trace_id, span_id, start_time, end_time, parent_span_id
    ),
    agg AS (
        SELECT
            trace_id,
            MIN(start_time) AS min_start,
            COUNT(*) AS new_count,
            MIN(end_time) FILTER (WHERE parent_span_id IS NULL) AS root_end,
            (array_agg(span_id) FILTER (WHERE parent_span_id IS NULL))[1] AS root_id
        FROM new_spans
        GROUP BY trace_id
    )
    INSERT INTO traces (trace_id, root_span_id, start_time, end_time, span_count)
    SELECT trace_id, root_id, min_start, root_end, new_count FROM agg
    ON CONFLICT (trace_id) DO UPDATE SET
        root_span_id = COALESCE(traces.root_span_id, EXCLUDED.root_span_id),
        start_time   = LEAST(traces.start_time, EXCLUDED.start_time),
        end_time     = COALESCE(traces.end_time, EXCLUDED.end_time),
        span_count   = traces.span_count + EXCLUDED.span_count
    """
)


def _row(s: CanonicalSpan) -> dict[str, Any]:
    return {
        "span_id_hex": s.span_id.hex(),
        "trace_id_hex": s.trace_id.hex(),
        "parent_span_id_hex": s.parent_span_id.hex() if s.parent_span_id else None,
        "name": s.name,
        "kind": s.kind,
        "start_time": s.start_time.isoformat(),
        "end_time": s.end_time.isoformat(),
        "status_code": s.status_code,
        "status_message": s.status_message,
        "gen_ai_operation": s.gen_ai_operation,
        "gen_ai_provider": s.gen_ai_provider,
        "gen_ai_request_model": s.gen_ai_request_model,
        "gen_ai_response_model": s.gen_ai_response_model,
        "gen_ai_response_id": s.gen_ai_response_id,
        "gen_ai_finish_reason": s.gen_ai_finish_reason,
        "input_tokens": s.input_tokens,
        "output_tokens": s.output_tokens,
        "cache_read_tokens": s.cache_read_tokens,
        "cache_creation_tokens": s.cache_creation_tokens,
        "tool_name": s.tool_name,
        "tool_call_id": s.tool_call_id,
        "attributes": _jsonable(s.attributes),
        "resource": _jsonable(s.resource),
    }


def _jsonable(d: dict[str, Any]) -> dict[str, Any]:
    """Make a dict JSON-serializable: bytes → hex, other values pass through."""
    out: dict[str, Any] = {}
    for k, v in d.items():
        out[k] = _coerce(v)
    return out


def _coerce(v: Any) -> Any:
    if isinstance(v, bytes):
        return v.hex()
    if isinstance(v, dict):
        return {k: _coerce(x) for k, x in v.items()}
    if isinstance(v, list):
        return [_coerce(x) for x in v]
    return v


async def store_spans(conn: AsyncConnection, spans: Iterable[CanonicalSpan]) -> None:
    """Persist a batch of spans inside the caller's transaction."""
    spans = list(spans)
    if not spans:
        return

    trace_ids = sorted({s.trace_id.hex() for s in spans})
    await conn.execute(_STUB_SQL, {"trace_ids": json.dumps(trace_ids)})

    rows = [_row(s) for s in spans]
    await conn.execute(_INSERT_SQL, {"rows": json.dumps(rows)})
