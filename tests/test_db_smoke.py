from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine


async def test_db_engine_smoke(db_engine: AsyncEngine) -> None:
    """Migrations applied: traces + spans exist with the expected indexes."""
    async with db_engine.connect() as conn:
        tables = (
            await conn.execute(
                text(
                    "SELECT tablename FROM pg_tables WHERE schemaname='public' "
                    "ORDER BY tablename"
                )
            )
        ).scalars().all()
        assert {"spans", "traces"}.issubset(set(tables))

        idx = (
            await conn.execute(
                text(
                    "SELECT indexname FROM pg_indexes "
                    "WHERE tablename IN ('traces','spans') "
                    "ORDER BY indexname"
                )
            )
        ).scalars().all()
        # All 8 spans indexes from design.md + traces primary/btree.
        expected = {
            "spans_attrs_gin",
            "spans_model",
            "spans_parent",
            "spans_pkey",
            "spans_root_agent_name",
            "spans_root_agent_version",
            "spans_root_target_url_trgm",
            "spans_tool_name",
            "spans_trace_id",
            "traces_pkey",
            "traces_start_time_desc",
        }
        assert expected.issubset(set(idx))
