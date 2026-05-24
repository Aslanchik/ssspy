"""initial schema: traces, spans

Revision ID: 0001
Revises:
Create Date: 2026-05-24
"""
from __future__ import annotations

from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")

    op.execute(
        """
        CREATE TABLE traces (
            trace_id      BYTEA PRIMARY KEY,
            root_span_id  BYTEA,
            start_time    TIMESTAMPTZ NOT NULL,
            end_time      TIMESTAMPTZ,
            span_count    INTEGER NOT NULL DEFAULT 0,
            ingested_at   TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    op.execute("CREATE INDEX traces_start_time_desc ON traces (start_time DESC)")

    op.execute(
        """
        CREATE TABLE spans (
            span_id        BYTEA PRIMARY KEY,
            trace_id       BYTEA NOT NULL REFERENCES traces(trace_id) ON DELETE CASCADE,
            parent_span_id BYTEA,
            name           TEXT NOT NULL,
            kind           SMALLINT NOT NULL,
            start_time     TIMESTAMPTZ NOT NULL,
            end_time       TIMESTAMPTZ NOT NULL,
            duration_ms    INTEGER GENERATED ALWAYS AS
                             ((EXTRACT(EPOCH FROM (end_time - start_time)) * 1000)::INT) STORED,
            status_code    TEXT NOT NULL,
            status_message TEXT,

            gen_ai_operation          TEXT,
            gen_ai_provider           TEXT,
            gen_ai_request_model      TEXT,
            gen_ai_response_model     TEXT,
            gen_ai_response_id        TEXT,
            gen_ai_finish_reason      TEXT,
            input_tokens              INTEGER,
            output_tokens             INTEGER,
            cache_read_tokens         INTEGER,
            cache_creation_tokens     INTEGER,
            tool_name                 TEXT,
            tool_call_id              TEXT,

            attributes JSONB NOT NULL DEFAULT '{}'::jsonb,
            resource   JSONB NOT NULL DEFAULT '{}'::jsonb
        )
        """
    )

    op.execute("CREATE INDEX spans_trace_id  ON spans (trace_id)")
    op.execute("CREATE INDEX spans_parent    ON spans (parent_span_id)")
    op.execute("CREATE INDEX spans_tool_name ON spans (tool_name) WHERE tool_name IS NOT NULL")
    op.execute("CREATE INDEX spans_model     ON spans (gen_ai_request_model) WHERE gen_ai_request_model IS NOT NULL")
    op.execute("CREATE INDEX spans_attrs_gin ON spans USING gin (attributes jsonb_path_ops)")
    op.execute(
        """
        CREATE INDEX spans_root_agent_name
            ON spans ((attributes->>'ssspy.agent.name'))
            WHERE parent_span_id IS NULL
        """
    )
    op.execute(
        """
        CREATE INDEX spans_root_agent_version
            ON spans ((attributes->>'ssspy.agent.version'))
            WHERE parent_span_id IS NULL
        """
    )
    op.execute(
        """
        CREATE INDEX spans_root_target_url_trgm
            ON spans USING gin ((attributes->>'ssspy.investigation.target_url') gin_trgm_ops)
            WHERE parent_span_id IS NULL
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS spans")
    op.execute("DROP TABLE IF EXISTS traces")
