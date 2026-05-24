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
