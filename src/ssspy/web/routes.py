"""Web UI routes: trace list and detail."""
from __future__ import annotations

from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.ext.asyncio import AsyncConnection

from ssspy.db import get_db
from ssspy.store.traces import get_trace, list_traces

router = APIRouter()

TEMPLATES_DIR = Path(__file__).parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


# Long-payload keys per CONTRACT.md / TI-5: shown collapsed with pretty JSON.
_LONG_PAYLOAD_KEYS = frozenset(
    {"ssspy.tool.input", "ssspy.tool.output", "ssspy.investigation.outcome"}
)


def render_attr_value(key: str, value: object) -> str:
    """Render an attribute value as HTML.

    Long-payload keys are wrapped in a collapsed ``<details>`` with pretty JSON;
    everything else is rendered inside a ``<code>`` block. Truncation flags
    (``*.truncated``) are not rendered through this filter — the parent span
    template surfaces them as badges.
    """
    import html
    import json

    if key in _LONG_PAYLOAD_KEYS:
        pretty = value
        if isinstance(value, str):
            try:
                pretty = json.dumps(json.loads(value), indent=2, sort_keys=True)
            except (TypeError, ValueError):
                pretty = value
        elif isinstance(value, (dict, list)):
            pretty = json.dumps(value, indent=2, sort_keys=True, default=str)
        return (
            "<details><summary>(payload)</summary>"
            f"<pre>{html.escape(str(pretty))}</pre></details>"
        )

    if isinstance(value, (dict, list)):
        rendered = json.dumps(value, indent=2, sort_keys=True, default=str)
        return f"<code><pre>{html.escape(rendered)}</pre></code>"
    return f"<code>{html.escape(str(value))}</code>"


def span_display_attrs(span) -> list[tuple[str, object]]:
    """Return all displayable attributes for a span in stable alphabetical order.

    Merges the typed hot columns and the JSONB attributes dict. Truncation
    flags are filtered out — they're rendered as badges next to their content
    attribute.
    """
    merged: dict[str, object] = {}
    for field in (
        "gen_ai_operation",
        "gen_ai_provider",
        "gen_ai_request_model",
        "gen_ai_response_model",
        "gen_ai_response_id",
        "gen_ai_finish_reason",
        "input_tokens",
        "output_tokens",
        "cache_read_tokens",
        "cache_creation_tokens",
        "tool_name",
        "tool_call_id",
    ):
        v = getattr(span, field, None)
        if v is not None:
            merged[field.replace("_", ".")] = v
    for k, v in span.attributes.items():
        if k.endswith(".truncated"):
            continue
        merged[k] = v
    return sorted(merged.items(), key=lambda kv: kv[0])


templates.env.filters["render_attr_value"] = render_attr_value
templates.env.globals["render_attr_value"] = render_attr_value
templates.env.globals["span_display_attrs"] = span_display_attrs


@router.get("/", response_class=HTMLResponse)
async def trace_list(
    request: Request,
    page: int = 1,
    agent: str | None = None,
    version: str | None = None,
    url: str | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
    db: AsyncConnection = Depends(get_db),
) -> HTMLResponse:
    rows = await list_traces(
        db,
        page=page,
        size=50,
        agent=agent,
        version=version,
        url=url,
        since=since,
        until=until,
    )
    return templates.TemplateResponse(
        request,
        "list.html",
        {
            "rows": rows,
            "filters": {
                "agent": agent,
                "version": version,
                "url": url,
                "since": since,
                "until": until,
            },
            "page": page,
        },
    )


@router.get("/traces/{trace_id}", response_class=HTMLResponse)
async def trace_detail(
    trace_id: str,
    request: Request,
    db: AsyncConnection = Depends(get_db),
) -> HTMLResponse:
    try:
        tid = bytes.fromhex(trace_id)
    except ValueError as e:
        raise HTTPException(404, detail="invalid trace_id") from e
    trace = await get_trace(db, tid)
    if trace is None:
        raise HTTPException(404, detail="trace not found")

    from ssspy.store.spans import list_spans_for_trace

    spans = await list_spans_for_trace(db, tid)
    span_ids = {s.span_id for s in spans}
    spans_by_parent: dict[bytes | None, list] = {}
    for s in spans:
        parent = s.parent_span_id if s.parent_span_id in span_ids else None
        spans_by_parent.setdefault(parent, []).append(s)
    for kids in spans_by_parent.values():
        kids.sort(key=lambda s: s.start_time)

    agent_name = trace.root.attributes.get("ssspy.agent.name") if trace.root else None
    return templates.TemplateResponse(
        request,
        "detail.html",
        {
            "trace": trace,
            "roots": spans_by_parent.get(None, []),
            "spans_by_parent": spans_by_parent,
            "agent_name": agent_name,
        },
    )
