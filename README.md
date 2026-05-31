<p align="center">
  <img src="docs/logo.png" alt="ssspy" width="200">
</p>

<h1 align="center">ssspy</h1>

<p align="center">
  <em>Observability and eval platform for OpenTelemetry-instrumented agents.</em>
</p>

ssspy ingests traces from agents emitting OpenTelemetry spans (the GenAI semantic conventions plus an `ssspy.*` extension namespace), stores them in PostgreSQL, and exposes a web UI to browse investigations, inspect span hierarchies, and — once Slices 2–4 land — grade outputs against rubrics, run Claude as an LLM judge, and compare versions of an agent against eval test sets.

The first consumer is [go-phish](https://github.com/Aslanchik/go-phish), but the storage layer is OTel-generic: any OTLP HTTP exporter works, and agent-specific concerns live only in the rendering layer.

---

## Status

| Slice | Scope | State |
|---|---|---|
| **1. trace-ingestion** | OTLP HTTP receiver, Postgres persistence, trace list + detail UI, go-phish agent partial | **shipped** |
| 2. human-grading | Rubric schema + CRUD, grading UI, grade storage | not started |
| 3. llm-judge | Judge orchestration, agreement analysis (kappa, Spearman) | not started |
| 4. eval-runs | Version tagging, side-by-side comparison, aggregate metrics | not started |

See `specs/trace-ingestion/` for the requirements / design / tasks driving Slice 1.

---

## Quick start

### Prerequisites

- Python **3.12+** (`brew install python@3.12`)
- PostgreSQL **16** reachable somewhere — ssspy reuses the `go-phish-postgres-1` Docker container by default
- A git checkout of this repository

### Install

```sh
git clone https://github.com/Aslanchik/ssspy
cd ssspy
python3.12 -m venv .venv
.venv/bin/pip install -e ".[dev]"
```

### Configure

```sh
cp .env.example .env
# Edit .env if your Postgres is somewhere other than the go-phish container.
```

Defaults from `.env.example`:

```ini
SSSPY_DATABASE_URL=postgresql+asyncpg://gophish:gophish@localhost:5432/ssspy
SSSPY_LISTEN_ADDR=0.0.0.0:4318
SSSPY_MAX_BODY_BYTES=4194304
SSSPY_LOG_LEVEL=INFO
```

The `ssspy` database must exist before first boot:

```sh
docker exec go-phish-postgres-1 psql -U gophish -d postgres -c "CREATE DATABASE ssspy;"
```

### Run

```sh
.venv/bin/ssspy serve
```

Migrations apply at startup. Once you see `Uvicorn running on http://0.0.0.0:4318`:

- OTLP receiver: `POST http://localhost:4318/v1/traces`
- Web UI: `http://localhost:4318/`
- Health: `http://localhost:4318/health`

### Point go-phish at it

```sh
cd ~/Work/projects/go-phish
OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4318 ./gophish https://example.com
# or, no LLM calls, ~5 spans:
OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4318 ./gophish --skip-llm https://example.com
```

Refresh `/` — the new trace appears within a second.

---

## What you'll see

**Trace list** (`/`) — one row per investigation, filterable by agent, version, URL substring, and start-time range:

| started | name | agent | version | target url | duration | status | spans |
|---|---|---|---|---|---|---|---|
| 2026-05-31T15:42 | ssspy.investigation | go-phish | a1b2c3d4 | https://example.com | 4 217 ms | Ok | 13 |

**Trace detail** (`/traces/{trace_id_hex}`) — at the top, an agent-specific panel when the root span carries `ssspy.agent.name = "go-phish"`; below that, a generic recursive span tree with expandable attributes and pretty-printed payloads for `ssspy.tool.input`, `ssspy.tool.output`, and `ssspy.investigation.outcome`.

Incomplete traces (children present, root not yet ingested) render with an "(incomplete)" badge and the available spans listed at the top level alongside any future root.

---

## Concepts

### Span shape

Spans normalize into a `CanonicalSpan` with three regions:

1. **Identity columns** — `trace_id` (16 bytes), `span_id` (8 bytes), `parent_span_id`, `name`, `kind`, timestamps, status.
2. **Hot GenAI columns** — `gen_ai_request_model`, `input_tokens`, `tool_name`, `tool_call_id`, etc. These are the OTel GenAI semconv v1.41.0 attributes; they're promoted from the OTLP payload into typed columns so they're queryable without JSONB extraction.
3. **JSONB attributes** — everything else, including the entire `ssspy.*` extension namespace and any future agent's attributes. Resource attributes (`service.name`, `telemetry.sdk.*`) go to a separate JSONB column.

Hot fields are *removed* from `attributes` when promoted — no duplication.

### Idempotency and out-of-order tolerance

- Re-POSTing the same `span_id` is a no-op (`ON CONFLICT DO NOTHING`).
- Child spans may arrive before their parent or before the root. The trace row exists from the first span; `root_span_id` and `end_time` are populated when (and if) the root arrives.
- Trace aggregates (`start_time`, `end_time`, `span_count`) only update from *newly-inserted* spans, so re-ingesting a trace doesn't drift the counts.
- The OTLP `partial_success` response surfaces per-span rejections without failing the batch.

### Agent-aware rendering

The UI default works for any OTel trace. When the root span carries `ssspy.agent.name = "go-phish"`, an extra partial is included above the generic tree that shows target URL, agent version, four phase rows, and the structured outcome JSON. Adding another agent means dropping a new template into `src/ssspy/web/templates/_agents/{agent}.html` and toggling the include in `detail.html` — no storage changes required.

---

## Configuration

All settings are environment variables prefixed `SSSPY_`. `pydantic-settings` reads `.env` automatically.

| Variable | Default | Notes |
|---|---|---|
| `SSSPY_DATABASE_URL` | *(required)* | asyncpg DSN, e.g. `postgresql+asyncpg://user:pass@host/db` |
| `SSSPY_LISTEN_ADDR` | `0.0.0.0:4318` | OTel HTTP default port |
| `SSSPY_MAX_BODY_BYTES` | `4194304` | 4 MB — rejects oversize OTLP bodies with HTTP 413 before parsing |
| `SSSPY_LOG_LEVEL` | `INFO` | stdlib logging level (`DEBUG`/`INFO`/`WARNING`/`ERROR`) |

---

## HTTP API

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/v1/traces` | OTLP receiver — accepts `application/x-protobuf` or `application/json` |
| `GET` | `/` | Trace list with query params: `page`, `agent`, `version`, `url`, `since`, `until` |
| `GET` | `/traces/{trace_id_hex}` | Trace detail with span tree |
| `GET` | `/health` | Liveness check; returns `{"status": "ok"}` |
| `GET` | `/static/*` | CSS and other static assets |

### OTLP receiver behaviour

| Outcome | Status |
|---|---|
| Valid batch ingested | `200` + serialized `ExportTraceServiceResponse` |
| One or more spans missing required fields | `200` + `partial_success.rejected_spans` populated |
| Body bigger than `SSSPY_MAX_BODY_BYTES` | `413` (no parse attempted) |
| Malformed protobuf / JSON | `400` with reason in body |
| Unsupported `content-type` | `400` |

The response media type matches the request: protobuf in, protobuf out; JSON in, JSON out.

---

## Database

Two tables; see `alembic/versions/0001_initial.py` for the full DDL.

```
traces (one row per trace_id)
├── trace_id        BYTEA PK
├── root_span_id    BYTEA              -- NULL until root arrives
├── start_time      TIMESTAMPTZ        -- MIN(spans.start_time)
├── end_time        TIMESTAMPTZ        -- root.end_time, NULL otherwise
├── span_count      INTEGER
└── ingested_at     TIMESTAMPTZ

spans (one row per span_id)
├── span_id, trace_id, parent_span_id, name, kind, start_time, end_time
├── duration_ms                        -- GENERATED column
├── status_code, status_message
├── 12 GenAI hot columns               -- gen_ai_*, *_tokens, tool_name, tool_call_id
├── attributes JSONB                   -- everything else, incl. ssspy.* namespace
└── resource   JSONB                   -- OTel Resource attrs (service.name, etc.)
```

Indexes that matter for the UI: a GIN on `attributes` for free-form filters, partial B-tree indexes on root-span agent/version, and a pg_trgm GIN on root-span `target_url` for substring search.

### Migrations

```sh
# Apply head (also runs on app startup):
.venv/bin/alembic upgrade head

# Create a new revision:
.venv/bin/alembic revision -m "add foo"

# Roll back one:
.venv/bin/alembic downgrade -1
```

Alembic uses `psycopg` for sync DDL while the app uses `asyncpg` for the request path — the conversion happens in `alembic/env.py`.

---

## Development

### Run the tests

```sh
.venv/bin/pytest
```

Integration tests spin up a per-run `ssspy_test_<uuid>` database against the same Postgres instance and drop it on teardown. 42 tests should pass in ~5 seconds.

### Layout

```
src/ssspy/
├── main.py            FastAPI factory + lifespan (migrations on startup)
├── cli.py             `ssspy serve`
├── config.py          Settings (pydantic-settings)
├── db.py              async engine, get_db dependency, run_migrations
├── logging.py         stdlib key=value formatter
├── models/
│   ├── otlp.py        OTLP wire types (protobuf + JSON converge here)
│   └── span.py        CanonicalSpan / Trace
├── ingest/
│   ├── decode.py      body → OtlpRequest
│   ├── normalize.py   OtlpRequest → list[CanonicalSpan] + RejectedSpan
│   ├── store.py       batch upsert (stub-insert + CTE)
│   └── routes.py      POST /v1/traces, GET /health
├── store/
│   ├── traces.py      list_traces, get_trace
│   └── spans.py       list_spans_for_trace
└── web/
    ├── routes.py      GET /, GET /traces/{id}, Jinja filters
    ├── static/style.css
    └── templates/
        ├── base.html  list.html  detail.html  _span.html
        └── _agents/go-phish.html
```

### Capturing real OTLP fixtures

`tests/ingest/_fixtures.py` falls back to synthetic envelopes when binary fixtures aren't on disk. To capture real ones from go-phish:

```sh
# Terminal A
.venv/bin/python scripts/capture_fixtures.py --out tests/fixtures/full.otlp.bin

# Terminal B
OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4318 \
    /Users/aslanchik/Work/projects/go-phish/gophish https://example.com
```

The recorder accepts one POST, writes the body, returns a valid OTLP response, and exits. Repeat with `--skip-llm` and the `skip-llm.otlp.bin` output path.

After commit, `load_full_otlp_body()` and `load_skip_llm_otlp_body()` in the test helper use the binary files instead of the synthetic builders — same tests, real bytes on the wire.

### Specs-before-code

Each slice lives in `specs/{slice}/` with three documents:

- `requirements.md` — what's true when this slice is done
- `design.md` — how it's built (schema, models, route shapes)
- `tasks.md` — ordered, verifiable work items (one commit per task)

This is the contract: don't write code that isn't covered by a spec, and stop to update the spec if you hit a wall.

---

## Roadmap

The next three slices follow the same `requirements / design / tasks` pattern. None of them touch the ingest path; they layer on top of the existing schema.

- **Slice 2 — human-grading.** Rubric schema (criteria with binary / categorical / ordinal scores). Hand-grade traces against a rubric, store grades alongside the trace, compute self-agreement if you grade the same trace twice. Goal: 20+ go-phish traces graded.
- **Slice 3 — llm-judge.** Use `instructor` + Claude to score the same rubrics against the same traces, store judge grades separately, compute per-criterion agreement (Cohen's kappa for categorical, Spearman for ordinal, accuracy for binary). Goal: explicitly document the judge's failure modes (positional bias, over/under-confidence) before iterating on the prompt.
- **Slice 4 — eval-runs.** Version-tagged batches of investigations, side-by-side comparison between two versions on the same URL set, calibration curves and per-criterion accuracy shifts. Goal: prompt change → measurable delta.

---

## License

This is a personal project; no license is published. Treat the code as "all rights reserved" until that changes.
