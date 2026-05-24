from __future__ import annotations

from pathlib import Path

import httpx
import pytest_asyncio
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from sqlalchemy.ext.asyncio import AsyncEngine

from ssspy.config import Settings
from ssspy.ingest.routes import router as ingest_router
from ssspy.web.routes import router as web_router


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
    app.include_router(web_router)
    static_dir = Path(__file__).resolve().parents[2] / "src" / "ssspy" / "web" / "static"
    app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield client
