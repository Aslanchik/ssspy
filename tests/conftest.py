from __future__ import annotations

import os
import uuid
from typing import AsyncIterator

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from ssspy.config import Settings
from ssspy.db import run_migrations

# Admin DB (used only to CREATE/DROP the per-test database).
ADMIN_DSN = os.environ.get(
    "SSSPY_TEST_ADMIN_DSN",
    "postgresql+asyncpg://gophish:gophish@localhost:5432/postgres",
)
# Template for the per-run test database DSN.
TEST_DSN_BASE = os.environ.get(
    "SSSPY_TEST_DSN_BASE",
    "postgresql+asyncpg://gophish:gophish@localhost:5432",
)


def _to_psycopg(dsn: str) -> str:
    return dsn.replace("postgresql+asyncpg://", "postgresql+psycopg://", 1)


@pytest.fixture
def settings() -> Settings:
    return Settings(
        database_url=f"{TEST_DSN_BASE}/ssspy_test",
        listen_addr="127.0.0.1:0",
        max_body_bytes=4 * 1024 * 1024,
        log_level="WARNING",
    )


@pytest_asyncio.fixture
async def db_engine() -> AsyncIterator[AsyncEngine]:
    """Yield an AsyncEngine bound to a freshly-created, migrated test database.

    The database name is randomized so parallel runs (and the existing gophish
    instance on the same Postgres) don't collide. The database is dropped on
    teardown.
    """
    db_name = f"ssspy_test_{uuid.uuid4().hex[:12]}"
    admin = create_async_engine(ADMIN_DSN, isolation_level="AUTOCOMMIT")
    try:
        async with admin.connect() as conn:
            await conn.exec_driver_sql(f'CREATE DATABASE "{db_name}"')
    finally:
        await admin.dispose()

    target_dsn = f"{TEST_DSN_BASE}/{db_name}"
    await run_migrations(target_dsn)
    engine = create_async_engine(target_dsn, future=True)
    try:
        yield engine
    finally:
        await engine.dispose()
        admin = create_async_engine(ADMIN_DSN, isolation_level="AUTOCOMMIT")
        try:
            async with admin.connect() as conn:
                await conn.exec_driver_sql(
                    f"SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                    f"WHERE datname = '{db_name}' AND pid <> pg_backend_pid()"
                )
                await conn.exec_driver_sql(f'DROP DATABASE IF EXISTS "{db_name}"')
        finally:
            await admin.dispose()
