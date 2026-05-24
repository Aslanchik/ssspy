# trace-ingestion: Tasks

Tasks are ordered. Each must be complete and verifiable before the next begins. If a task surfaces a spec problem, stop — update the relevant spec, re-review, then continue. One commit per task.

Status: `[ ]` todo · `[x]` done · `[~]` in progress

---

## T-01: [ ] Project bootstrap

**Satisfies:** TI-7, TI-8

- Create `pyproject.toml` with the project metadata and the dependency set listed in design.md: `fastapi`, `uvicorn[standard]`, `pydantic>=2`, `pydantic-settings`, `sqlalchemy[asyncio]>=2`, `asyncpg`, `alembic`, `opentelemetry-proto`, `jinja2`, `typer`, plus dev dependencies `pytest`, `pytest-asyncio`, `httpx`
- Declare a console script entry point: `ssspy = ssspy.cli:app`
- Create the package skeleton under `src/ssspy/`: empty `__init__.py`, `config.py` (the `Settings` class from design.md, reading `SSSPY_*` env vars), `logging.py` (stdlib `logging` config with the key=value formatter shown in design.md), `cli.py` (Typer app with `serve` command — body can be a stub that prints the loaded settings for now), and a minimal `main.py` (`create_app(settings) -> FastAPI` returning an app with no routes mounted yet)
- Create `tests/conftest.py` with a pytest fixture that constructs `Settings` from a fixed dict (no env reads)
- Create `.env.example` listing every `SSSPY_*` variable with safe defaults
- The package must be installable with `pip install -e .` in editable mode

**Verified when:** `pip install -e .[dev]` succeeds in a fresh virtualenv; `ssspy serve --help` prints the Typer usage; `pytest` runs with zero collected tests and exits 0; `python -c "from ssspy.main import create_app; from ssspy.config import Settings; create_app(Settings(database_url='postgres://x/y'))"` returns a `FastAPI` instance with no error.

---

## T-02: [ ] Database setup + initial migration

**Satisfies:** TI-2, TI-7

- Create `src/ssspy/db.py`: an async SQLAlchemy engine factory keyed off `Settings.database_url`, a `get_db()` FastAPI dependency yielding an `AsyncConnection`, and a `run_migrations()` helper that invokes Alembic programmatically
- Create `alembic.ini` at the repo root with the standard layout; `alembic/env.py` configured to read `DATABASE_URL` from environment and run migrations against the SQLAlchemy engine
- Create `alembic/versions/0001_initial.py` containing the full DDL from design.md verbatim: `pg_trgm` extension, the `traces` table, the `spans` table with all hot columns and JSONB columns, the generated `duration_ms` column, the FK from `spans.trace_id` to `traces.trace_id` with `ON DELETE CASCADE`, and every index listed (including the three root-span partial indexes and the GIN indexes)
- Wire `run_migrations()` into the FastAPI lifespan in `main.py` so migrations apply at startup
- Add a `tests/conftest.py` fixture `db_engine` that creates a disposable test database (`ssspy_test`), applies migrations, yields the engine, and drops the database afterwards. The fixture must be safe to run alongside the existing `gophish` Postgres instance

**Verified when:** `alembic upgrade head` against a fresh Postgres database creates two tables (`traces`, `spans`) with the expected columns and indexes; `\d spans` in psql shows all hot columns, both JSONB columns, the generated `duration_ms` column, and the eight indexes; the test fixture runs and tears down cleanly via `pytest tests/conftest.py::test_db_engine_smoke -k db_engine` (one trivial smoke test added in this task).

---

## T-03: [ ] Pydantic schemas — OTLP wire and canonical Span

**Satisfies:** TI-2, TI-8

- Create `src/ssspy/models/otlp.py` containing the OTLP HTTP v1 trace request types as Pydantic v2 models with `populate_by_name=True` and `alias` for the camelCase JSON field names: `OtlpAnyValue` (union of value variants), `OtlpKvList`, `OtlpArrayValue`, `OtlpKeyValue`, `OtlpStatus`, `OtlpSpan`, `OtlpScopeSpans`, `OtlpResourceSpans`, `OtlpRequest` (the top-level `ExportTraceServiceRequest` shape)
- Implement `OtlpRequest.from_protobuf(envelope: ExportTraceServiceRequest) -> OtlpRequest` using `google.protobuf.json_format.MessageToDict(envelope, preserving_proto_field_name=False)` then `cls.model_validate(...)`
- Create `src/ssspy/models/span.py` with `CanonicalSpan` (the full field set from design.md: identity fields, status, the 12 typed hot fields, `attributes: dict`, `resource: dict`) and a `Trace` view model (`trace_id`, `start_time`, `end_time`, `span_count`, `root: CanonicalSpan | None`) used by the read layer
- Unit tests in `tests/ingest/test_models.py`:
  - `OtlpAnyValue` correctly distinguishes string / bool / int (string-encoded) / double / array / kvlist variants
  - `OtlpRequest.from_protobuf` round-trips a hand-built `ExportTraceServiceRequest` with one span (use the `opentelemetry-proto` Python types to build it)
  - `CanonicalSpan.model_validate` round-trips through `model_dump()` without loss for a span carrying all hot fields populated

**Verified when:** `pytest tests/ingest/test_models.py -v` passes all three test cases.

---

## T-04: [ ] OTLP decode

**Satisfies:** TI-1, TI-2, TI-9

- Create `src/ssspy/ingest/decode.py` with `decode_request(body: bytes, content_type: str) -> OtlpRequest` and a `DecodeError` exception type
- The function dispatches on content type: `"application/x-protobuf"` → `ExportTraceServiceRequest().ParseFromString(body)` then `OtlpRequest.from_protobuf(pb)`; `"application/json"` → `OtlpRequest.model_validate_json(body)`; anything else → raise `DecodeError`
- Protobuf parse failures (`google.protobuf.message.DecodeError`) and JSON validation failures (`pydantic.ValidationError`) are caught and re-raised as `DecodeError` with a brief message
- Unit tests in `tests/ingest/test_decode.py`:
  - Valid protobuf body decodes to a populated `OtlpRequest`
  - Valid OTLP JSON body decodes equivalently to the same `OtlpRequest`
  - Unsupported content type raises `DecodeError`
  - Truncated/malformed protobuf raises `DecodeError` (not a bare protobuf exception)
  - Malformed JSON raises `DecodeError`

**Verified when:** `pytest tests/ingest/test_decode.py -v` passes all five cases.

---

## T-05: [ ] OTLP normalization

**Satisfies:** TI-2, TI-8, TI-9

- Create `src/ssspy/ingest/normalize.py` with:
  - `flatten_value(av: OtlpAnyValue) -> Any` per the design.md implementation (handles every variant; protobuf int64-as-string conversion is explicit)
  - The `HOT_ATTRS` mapping table from design.md
  - `normalize_span(otlp: OtlpSpan, resource: dict) -> CanonicalSpan` — flattens attrs, lifts hot fields into typed columns and removes them from `attributes`, handles the `gen_ai.response.finish_reasons` special case, decodes hex trace_id/span_id to bytes when arriving via JSON, treats empty `parent_span_id` as `None`, converts unix-nano timestamps to UTC `datetime`
  - `normalize_request(req: OtlpRequest) -> tuple[list[CanonicalSpan], list[RejectedSpan]]` — iterates ResourceSpans → ScopeSpans → Span; per-span errors (missing required fields: trace_id, span_id, name, start_time, end_time) append to the rejected list, do not raise
- `RejectedSpan` is a small Pydantic model: `{span_id_hex: str | None, reason: str}`
- Unit tests in `tests/ingest/test_normalize.py`:
  - `flatten_value` correctly converts every `AnyValue` variant
  - Hot-attr promotion: a span with `gen_ai.request.model` ends up with the column populated and the key absent from `attributes`
  - `gen_ai.response.finish_reasons` first-element extraction; empty list yields `None`
  - Empty `parent_span_id` (both `None` and `b""`/`""`) normalizes to `None`
  - Required-field violation places the span in `rejected`, not in the normalized list, without raising
  - Round-trip a fixture's normalized spans: `attributes` JSONB contains every `ssspy.*` key from the fixture; no data lost

**Verified when:** `pytest tests/ingest/test_normalize.py -v` passes all six cases.

---

## T-06: [ ] Span persistence layer

**Satisfies:** TI-2, TI-3, TI-9

- Create `src/ssspy/ingest/store.py` with `store_spans(conn: AsyncConnection, spans: list[CanonicalSpan]) -> None`
- The function runs two statements inside a transaction:
  1. Pre-insert trace stubs: `INSERT INTO traces (trace_id, start_time) VALUES (...) ON CONFLICT (trace_id) DO NOTHING` for every distinct trace_id in the batch (synthetic start_time = `now()`, immediately corrected in step 2)
  2. The CTE from design.md: `WITH new_spans AS (INSERT ... ON CONFLICT (span_id) DO NOTHING RETURNING ...), agg AS (...) INSERT INTO traces ... ON CONFLICT (trace_id) DO UPDATE SET ...`
- An empty input list is a no-op; the function returns without issuing any SQL
- The rows passed to the CTE are serialized as a single JSONB document and unpacked with `jsonb_to_recordset` so the batch is one round-trip
- Integration tests in `tests/ingest/test_store.py` (use the `db_engine` fixture from T-02):
  - Insert one full trace (the `full.otlp` fixture spans normalized) — verify `traces` row exists with correct `root_span_id`, `start_time`, `end_time`, `span_count = 13`; `spans` table has 13 rows; the root span has `parent_span_id IS NULL`; one tool span has its `gen_ai.tool.name` and `attributes->>'ssspy.tool.input'` populated
  - Re-insert the same trace — verify zero new rows, `span_count` unchanged, `start_time`/`end_time` unchanged
  - Insert children before root — `traces.end_time` is `NULL` until the root span row is inserted, then equals the root's `end_time`; `traces.start_time` is correct (min) at every step
  - Insert a span with `parent_span_id` not present in storage — span persists; tree assembly is the read layer's concern

**Verified when:** `pytest tests/ingest/test_store.py -v` passes all four cases against a real Postgres test database.

---

## T-07: [ ] OTLP receiver route

**Satisfies:** TI-1, TI-3, TI-9

- Create `src/ssspy/ingest/routes.py` with `POST /v1/traces` implementing the handler shape from design.md
- Wire the route into `main.py`'s `create_app`
- The handler:
  - Reads body with the `SSSPY_MAX_BODY_BYTES` cap enforced *before* parsing; oversize → HTTP 413, no parse attempted
  - Calls `decode_request`; `DecodeError` → HTTP 400 with the reason as the response body
  - Calls `normalize_request`; runs `store_spans` inside the route's DB transaction
  - Returns HTTP 200 with a serialized `ExportTraceServiceResponse`; if `rejected` is non-empty, populates `partial_success.rejected_spans` and `partial_success.error_message` (first 5 reasons joined)
  - Selects response media type to match request: `application/x-protobuf` returns serialized bytes; `application/json` returns `MessageToDict(resp)`
- Add a `GET /health` endpoint returning `{"status": "ok"}`
- Integration tests in `tests/ingest/test_routes.py` (use FastAPI `TestClient` + the `db_engine` fixture):
  - POST the protobuf fixture body → 200, DB now contains the expected trace and span count
  - POST the same body again → 200, DB row count unchanged (idempotency end-to-end)
  - POST OTLP JSON equivalent of the same fixture → 200, identical DB state to the protobuf case
  - POST malformed body → 400, DB unchanged
  - POST a body larger than `SSSPY_MAX_BODY_BYTES` → 413, DB unchanged
  - POST unsupported content type → 400 or 415 (whichever the handler chose; the test asserts the chosen behaviour)
  - POST a batch where one span is missing `trace_id` → 200, `partial_success.rejected_spans = 1`, the valid spans land in the DB

**Verified when:** `pytest tests/ingest/test_routes.py -v` passes all seven cases.

---

## T-08: [ ] Read-side queries

**Satisfies:** TI-4, TI-5

- Create `src/ssspy/store/traces.py` with:
  - `list_traces(conn, *, page: int, size: int, agent: str | None, version: str | None, url: str | None, since: datetime | None, until: datetime | None) -> list[TraceListRow]` — runs the SQL from design.md (left join `traces` to root span; filter on root JSONB; order by start_time desc; offset/limit)
  - `get_trace(conn, trace_id: bytes) -> Trace | None` — fetches the trace row + its root span (if present) hydrated into a `Trace` Pydantic model
- Create `src/ssspy/store/spans.py` with:
  - `list_spans_for_trace(conn, trace_id: bytes) -> list[CanonicalSpan]` — fetches all spans for a trace ordered by `start_time`; reconstructs `CanonicalSpan` from row columns and the JSONB blobs
- `TraceListRow` is a small Pydantic model with: `trace_id: bytes`, `start_time`, `end_time`, `span_count`, `root_name: str | None`, `status_code: str | None`, `duration_ms: int | None`, `agent_name: str | None`, `agent_version: str | None`, `target_url: str | None`
- Unit tests in `tests/store/test_traces.py` and `tests/store/test_spans.py` (use the `db_engine` fixture; seed with the full fixture's normalized spans):
  - `list_traces` with no filters returns the seeded trace
  - Each filter (agent, version, url substring, since, until) returns or excludes the trace correctly
  - Pagination: page=2 returns empty when only one trace exists
  - `get_trace` returns the populated trace with its root span; returns `None` for an unknown trace_id
  - `list_spans_for_trace` returns 13 spans in chronological order; round-trips JSONB attributes back into the Pydantic model

**Verified when:** `pytest tests/store/ -v` passes all cases.

---

## T-09: [ ] Web base layout and trace list view

**Satisfies:** TI-4

- Create `src/ssspy/web/routes.py` with the `GET /` handler from design.md; wire it into `main.py`
- Create `src/ssspy/web/templates/base.html`: minimal HTML5 shell with a `<title>`, a `<header>` showing "ssspy", the `<main>` block, and a single `<script>` tag loading HTMX from a pinned CDN URL
- Create `src/ssspy/web/templates/list.html`: a `<table>` with rows for each `TraceListRow` showing start_time, root span name (or "(incomplete)"), agent name, agent version, target URL, duration, status, span count, and a link to `/traces/{trace_id_hex}`; a `<form>` above the table with `<input>` fields for each filter (`agent`, `version`, `url`, `since`, `until`) submitting via GET to `/`; previous/next page links using `?page=` query param
- Create `src/ssspy/web/static/style.css` with minimal styling (table grid, status-code colour classes, alternating row background); mount the static dir on the FastAPI app
- Integration test in `tests/web/test_list.py` (use TestClient + `db_engine` fixture):
  - Empty DB → `GET /` returns 200, body contains "ssspy" and no `<tr>` rows
  - Seed with two traces (different agents) → `GET /` returns both; `GET /?agent=go-phish` returns only the matching one; `GET /?url=example` returns only the matching one
  - Incomplete trace (no root) → row renders with "(incomplete)" badge and no NULL exceptions

**Verified when:** `pytest tests/web/test_list.py -v` passes all three cases; manually opening `/` in a browser after seeding the DB shows the expected table.

---

## T-10: [ ] Trace detail view with recursive span tree

**Satisfies:** TI-5

- Add the `GET /traces/{trace_id}` handler from design.md to `src/ssspy/web/routes.py` (404 on unknown id; renders `detail.html`)
- Create `src/ssspy/web/templates/detail.html`: top-of-page metadata (trace_id hex, started/ended timestamps, span count, "incomplete" badge when end_time is null); an "agent partial" slot (empty in this task; populated in T-11); the `<ol class="span-tree">` rendering each root from the `roots` list
- Create `src/ssspy/web/templates/_span.html`: the recursive include from design.md — `<details>` per span showing name, duration, status; a `<dl>` of attributes; children rendered by recursing on `spans_by_parent.get(root.span_id, [])`
- Implement the `render_attr_value(key, value)` Jinja filter (registered in `main.py`): wraps long-payload keys (`ssspy.tool.input`, `ssspy.tool.output`, `ssspy.investigation.outcome`) in a collapsed `<details>` with pretty-printed JSON; renders all other values as a `<code>` block; attributes are listed alphabetically; the truncation flag (e.g. `ssspy.tool.output.truncated`) is rendered as a small badge next to its content attribute, not as its own row
- Orphan span handling: spans whose `parent_span_id` is not in the trace are placed in `roots` so they render at the top level alongside the real root
- Sibling ordering: spans at each tree level are sorted by `start_time` ascending
- Integration tests in `tests/web/test_detail.py`:
  - Seed with the full fixture → `GET /traces/{hex}` returns 200; HTML contains every span's name; the span tree's structure (verified by counting `<details>` elements with class `span` or similar) matches the 13-span hierarchy
  - Unknown trace_id → 404
  - Seed with children-only (no root) → HTML renders the available spans with the "incomplete" badge; no template exception
  - Long-payload attr → wrapped in `<details>` (closed by default); pretty-printed when expanded (verified by checking the response HTML contains both the attr key and a multi-line JSON snippet)

**Verified when:** `pytest tests/web/test_detail.py -v` passes all four cases; manually opening `/traces/{hex}` for the full fixture shows the four-phase tree with the expected expandable structure.

---

## T-11: [ ] go-phish agent rendering partial

**Satisfies:** TI-6

- Create `src/ssspy/web/templates/_agents/go-phish.html`: the partial included from `detail.html` when the root span carries `ssspy.agent.name = "go-phish"`. It renders:
  - The target URL and agent version pulled from the root span's attributes
  - The four phase spans in `phase_index` order, each showing its phase name, duration, and status
  - The `ssspy.investigation.outcome` JSON pretty-printed for the hypothesis and synthesis phase spans (the only phases where the outcome is emitted)
  - Each phase's child span count broken down by kind (e.g., "1 LLM call, 4 tool calls" for enrichment)
- The partial reads attributes defensively: missing or unparseable values cause the affected field to render blank rather than raising a template error
- Update `detail.html` to include the partial conditionally on `agent_name == "go-phish"` and pass the data it needs as template context (the root span and the spans-by-parent map)
- Integration tests in `tests/web/test_agent_render.py`:
  - Full fixture seed → `GET /traces/{hex}` contains the target URL string, "Phase 1: fetch", "Phase 2: hypothesis", "Phase 3: enrichment", "Phase 4: synthesis", and a hypothesis outcome snippet (`"brand":` substring)
  - A trace with `ssspy.agent.name` absent from the root span → partial is not rendered; the generic tree below is unaffected
  - A trace with malformed `ssspy.investigation.outcome` (non-JSON string) → partial renders without exception, the outcome field shows the raw string verbatim

**Verified when:** `pytest tests/web/test_agent_render.py -v` passes all three cases; manually opening the full fixture's detail page shows the agent section above the generic span tree.

---

## T-12: [ ] Fixture capture script + committed fixtures

**Satisfies:** TI-10

- Create `scripts/capture_fixtures.py`: stands up an `aiohttp` or stdlib `http.server` instance on a configurable port; the single handler accepts `POST /v1/traces`, writes `request.body` to a file specified on the command line, returns a valid `ExportTraceServiceResponse`, and exits the process cleanly after the request is served
- Document the capture procedure in a `tests/fixtures/README.md` (one short paragraph): start the script with the output path, run go-phish with `OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:<port>` and the desired flags, the script writes the binary protobuf and exits
- Run the script twice against go-phish to produce `tests/fixtures/full.otlp.bin` (full LLM-enabled investigation) and `tests/fixtures/skip-llm.otlp.bin` (5-span minimum)
- Commit the two `.bin` files
- Update the T-03 / T-04 / T-05 / T-06 / T-07 tests that loaded "the full fixture" to point at these committed files

**Verified when:** `tests/fixtures/full.otlp.bin` and `tests/fixtures/skip-llm.otlp.bin` exist; rerunning the full test suite against the committed fixtures still passes; `python scripts/capture_fixtures.py --help` prints usage.

---

## T-13: [ ] End-to-end smoke test

**Satisfies:** TI-1, TI-3, TI-5, TI-6, TI-10

Run against a clean Postgres database (fresh `ssspy` database, migrations applied) in a single terminal session:

- Start `ssspy serve` with `DATABASE_URL` pointing at the clean DB; confirm migrations apply at startup and the server is listening on `:4318`
- Run `gophish --skip-llm https://example.com` with `OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4318` (no file exporter)
- Confirm the trace appears at `GET /` within 5 seconds of go-phish completing; the row shows agent `go-phish`, target URL `https://example.com`, span count `5`, status `Ok`
- Open the trace detail page; confirm:
  - The agent partial renders with all four phase rows
  - The generic span tree shows the root with four phase children, no chat or tool spans
  - The hypothesis phase has its `ssspy.investigation.outcome` collapsed and expandable to the stub hypothesis JSON
- Run a full LLM investigation: `gophish https://example.com` with the same OTLP endpoint
- Confirm the new trace appears with span count `13` (or whatever the run actually produced)
- Open the detail page; confirm the enrichment phase has tool spans as siblings of the LLM call spans, screenshot SHA-256 is present on the hypothesis chat span, tool input/output are collapsed-by-default and expand to JSON, and the `gen_ai.usage.input_tokens` column is populated on every chat span
- Re-POST the full fixture body via `curl --data-binary @tests/fixtures/full.otlp.bin -H 'content-type: application/x-protobuf' http://localhost:4318/v1/traces`; confirm the response is 200, no new rows in `traces` or `spans` (idempotency end-to-end)
- Stop go-phish entirely; confirm `gophish https://example.com` still runs to completion with only a warning logged about the OTLP exporter being unreachable (independence requirement from the conversation: go-phish runs fine without ssspy)

**Verified when:** Every check above passes in one session; the database holds at least two distinct traces; the detail pages render correctly for both; the idempotent re-POST adds no rows.

---

## Out of scope for this task list

- Anything in Slices 2-4 (grading, judge, eval runs)
- Any non-`go-phish` agent rendering
- Performance benchmarking or load testing
- Deployment automation (Dockerfile, docker-compose for ssspy itself)
- CI configuration
- README or user-facing documentation beyond the fixture capture note
