from __future__ import annotations

import asyncio
from pathlib import Path
from typing import AsyncIterator

from alembic import command
from alembic.config import Config
from fastapi import Request
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, create_async_engine

_engine: AsyncEngine | None = None


def get_engine(database_url: str) -> AsyncEngine:
    """Return a singleton async engine for the given URL.

    Subsequent calls with the same URL reuse the engine; a different URL replaces it.
    """
    global _engine
    if _engine is None:
        _engine = create_async_engine(database_url, future=True, pool_pre_ping=True)
    return _engine


async def dispose_engine() -> None:
    global _engine
    if _engine is not None:
        await _engine.dispose()
        _engine = None


async def get_db(request: Request) -> AsyncIterator[AsyncConnection]:
    """FastAPI dependency yielding a transactional AsyncConnection."""
    engine: AsyncEngine = request.app.state.engine
    async with engine.connect() as conn:
        async with conn.begin():
            yield conn


def _alembic_config(database_url: str) -> Config:
    repo_root = Path(__file__).resolve().parents[2]
    cfg = Config(str(repo_root / "alembic.ini"))
    cfg.set_main_option("script_location", str(repo_root / "alembic"))
    cfg.set_main_option("sqlalchemy.url", database_url)
    return cfg


async def run_migrations(database_url: str) -> None:
    """Apply alembic migrations to head. Runs in a worker thread."""
    cfg = _alembic_config(database_url)
    await asyncio.to_thread(command.upgrade, cfg, "head")
