"""POST /v1/traces — OTLP HTTP receiver."""
from __future__ import annotations

import logging
import time

from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import JSONResponse
from google.protobuf.json_format import MessageToDict
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import (
    ExportTraceServiceResponse,
)
from sqlalchemy.ext.asyncio import AsyncConnection

from ssspy.config import Settings
from ssspy.db import get_db
from ssspy.ingest.decode import DecodeError, decode_request
from ssspy.ingest.normalize import normalize_request
from ssspy.ingest.store import store_spans
from ssspy.logging import log_kv

router = APIRouter()

log = logging.getLogger("ssspy.ingest")


def _get_settings(request: Request) -> Settings:
    return request.app.state.settings


@router.post("/v1/traces")
async def ingest_traces(
    request: Request,
    settings: Settings = Depends(_get_settings),
    db: AsyncConnection = Depends(get_db),
) -> Response:
    started = time.monotonic()
    ct = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()

    # Enforce size cap before parsing.
    cl = request.headers.get("content-length")
    if cl is not None:
        try:
            if int(cl) > settings.max_body_bytes:
                return Response(status_code=413)
        except ValueError:
            pass
    body = await request.body()
    if len(body) > settings.max_body_bytes:
        return Response(status_code=413)

    try:
        otlp_req = decode_request(body, ct)
    except DecodeError as e:
        log_kv(log, logging.WARNING, "decode_failed", method="POST", path="/v1/traces", reason=str(e))
        return Response(status_code=400, content=str(e), media_type="text/plain")

    spans, rejected = normalize_request(otlp_req)
    await store_spans(db, spans)

    resp = ExportTraceServiceResponse()
    if rejected:
        resp.partial_success.rejected_spans = len(rejected)
        resp.partial_success.error_message = "; ".join(r.reason for r in rejected[:5])

    dur_ms = int((time.monotonic() - started) * 1000)
    log_kv(
        log,
        logging.INFO,
        "ingest",
        method="POST",
        path="/v1/traces",
        status=200,
        spans=len(spans),
        rejected=len(rejected),
        dur_ms=dur_ms,
    )

    if ct == "application/json":
        return JSONResponse(content=MessageToDict(resp))
    return Response(
        content=resp.SerializeToString(),
        media_type="application/x-protobuf",
    )


@router.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}
