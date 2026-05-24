from __future__ import annotations

import httpx
import pytest_asyncio
from fastapi import FastAPI
from google.protobuf.json_format import MessageToDict
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import (
    ExportTraceServiceRequest,
    ExportTraceServiceResponse,
)
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from ssspy.config import Settings
from ssspy.ingest.routes import router as ingest_router

from ._fixtures import full_envelope, load_full_otlp_body


@pytest_asyncio.fixture
async def app_client(db_engine: AsyncEngine):
    app = FastAPI()
    app.state.engine = db_engine
    app.state.settings = Settings(
        database_url="postgresql+asyncpg://unused",
        listen_addr="127.0.0.1:0",
        max_body_bytes=1024 * 1024,
        log_level="WARNING",
    )
    app.include_router(ingest_router)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


async def _count_rows(engine: AsyncEngine, table: str) -> int:
    async with engine.connect() as conn:
        return (await conn.execute(text(f"SELECT COUNT(*) FROM {table}"))).scalar_one()


async def test_post_protobuf_inserts_trace(
    app_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    body = load_full_otlp_body()
    r = await app_client.post(
        "/v1/traces",
        content=body,
        headers={"content-type": "application/x-protobuf"},
    )
    assert r.status_code == 200, r.text
    pb_resp = ExportTraceServiceResponse()
    pb_resp.ParseFromString(r.content)
    assert pb_resp.partial_success.rejected_spans == 0

    assert await _count_rows(db_engine, "traces") == 1
    assert await _count_rows(db_engine, "spans") == 13


async def test_idempotent_repost(
    app_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    body = load_full_otlp_body()
    await app_client.post(
        "/v1/traces", content=body, headers={"content-type": "application/x-protobuf"}
    )
    spans_before = await _count_rows(db_engine, "spans")
    r = await app_client.post(
        "/v1/traces", content=body, headers={"content-type": "application/x-protobuf"}
    )
    assert r.status_code == 200
    assert await _count_rows(db_engine, "spans") == spans_before


async def test_post_json_equivalent_state(
    app_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    pb = ExportTraceServiceRequest()
    pb.ParseFromString(load_full_otlp_body())
    # MessageToDict default base64-encodes byte ids; we re-encode to hex
    # to mirror the wire JSON spec.
    from ssspy.models.otlp import _hexify_span_ids

    json_body = MessageToDict(pb, preserving_proto_field_name=False)
    _hexify_span_ids(json_body)
    r = await app_client.post(
        "/v1/traces",
        json=json_body,
        headers={"content-type": "application/json"},
    )
    assert r.status_code == 200, r.text
    assert await _count_rows(db_engine, "spans") == 13


async def test_malformed_body_400(app_client: httpx.AsyncClient) -> None:
    r = await app_client.post(
        "/v1/traces",
        content=b"\xff\xff\xfftrash",
        headers={"content-type": "application/x-protobuf"},
    )
    assert r.status_code == 400


async def test_oversize_body_413(app_client: httpx.AsyncClient) -> None:
    big = b"\x00" * (2 * 1024 * 1024)
    r = await app_client.post(
        "/v1/traces",
        content=big,
        headers={"content-type": "application/x-protobuf"},
    )
    assert r.status_code == 413


async def test_unsupported_content_type_400(app_client: httpx.AsyncClient) -> None:
    r = await app_client.post(
        "/v1/traces", content=b"{}", headers={"content-type": "text/plain"}
    )
    assert r.status_code == 400


async def test_partial_success_when_a_span_is_invalid(
    app_client: httpx.AsyncClient, db_engine: AsyncEngine
) -> None:
    env = full_envelope()
    env.resource_spans[0].scope_spans[0].spans[0].name = ""
    r = await app_client.post(
        "/v1/traces",
        content=env.SerializeToString(),
        headers={"content-type": "application/x-protobuf"},
    )
    assert r.status_code == 200
    pb_resp = ExportTraceServiceResponse()
    pb_resp.ParseFromString(r.content)
    assert pb_resp.partial_success.rejected_spans == 1
    assert await _count_rows(db_engine, "spans") == 12


async def test_health(app_client: httpx.AsyncClient) -> None:
    r = await app_client.get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}
