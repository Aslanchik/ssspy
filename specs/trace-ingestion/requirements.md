# trace-ingestion: Requirements

## Overview

ssspy ingests OpenTelemetry traces from agents instrumented per the go-phish `CONTRACT.md` (the only consumer in scope today). Traces arrive via OTLP HTTP on a configurable port and are persisted to PostgreSQL. A minimal web UI lists recent traces and renders a per-trace span tree with full attribute and payload inspection. The storage layer is OTel-generic — no schema names, columns, or tables encode go-phish-specific concepts; agent-specific knowledge lives in the rendering layer only. The agent runs independently of ssspy: if ssspy is not listening, the OTLP export silently no-ops on the agent side and the agent's own work is unaffected.

Scope: OTLP HTTP ingest, persistence, and a read-only UI for browsing traces. No grading, judging, or eval-run features in this pass — those are Slices 2-4.

---

## TI-1: OTLP HTTP receiver

ssspy accepts OpenTelemetry traces over OTLP HTTP at a configurable endpoint.

**Acceptance criteria:**
- The receiver exposes `POST /v1/traces` on the configured listen address; the default port is `4318` (the OTel default, matching go-phish's default export endpoint)
- The endpoint accepts `Content-Type: application/x-protobuf` (the format Go's `otlptracehttp` exporter sends by default) and `Content-Type: application/json` (OTLP JSON)
- Successful ingestion returns HTTP `200` with an empty `ExportTraceServiceResponse` body, per the OTLP spec
- Malformed requests (unparseable protobuf/JSON, missing required fields) return HTTP `400` with a brief error message; the receiver does not crash and continues serving other requests
- Requests larger than a configurable maximum body size (default `4 MB`) are rejected with HTTP `413`; the limit accommodates a full go-phish trace (13 spans × up to 32 KB payload each ≈ 0.5 MB worst case, with headroom)
- The listen address and port are configurable via environment variables (specifics in `design.md`)
- The receiver accepts batched span exports (multiple resource spans in one request, multiple scope spans per resource, multiple spans per scope) as defined by OTLP

---

## TI-2: Span normalization and persistence

OTLP payloads are normalized into a canonical Span shape and persisted to PostgreSQL.

**Acceptance criteria:**
- The OTLP `AnyValue` attribute encoding (string, int, double, bool, bytes, array variants) is flattened into native Python/JSON types during normalization; downstream code never handles raw OTLP `AnyValue` objects
- Every span emitted by go-phish per `CONTRACT.md` round-trips through ingest and persistence without loss: every attribute that arrived in the OTLP payload is readable from storage afterwards
- Standard OTel GenAI semconv attributes (v1.41.0) defined in `CONTRACT.md` are persisted as queryable columns:
  - `gen_ai.operation.name`, `gen_ai.provider.name`
  - `gen_ai.request.model`, `gen_ai.response.model`, `gen_ai.response.id`
  - `gen_ai.response.finish_reasons` (first element extracted into a scalar column)
  - `gen_ai.usage.input_tokens`, `gen_ai.usage.output_tokens`
  - `gen_ai.usage.cache_creation.input_tokens`, `gen_ai.usage.cache_read.input_tokens`
  - `gen_ai.tool.name`, `gen_ai.tool.call.id`
- Span identity fields are persisted as native columns: `trace_id` (16 bytes), `span_id` (8 bytes), `parent_span_id` (8 bytes, nullable for root), `name`, `kind`, `start_time`, `end_time`, `status_code`, `status_message`
- `status_code` distinguishes `Ok`, `Error`, and `Unset`; the receiver does not coerce `Unset` to `Ok` (go-phish leaves most non-root spans with status `Unset`, and that distinction is preserved)
- All other attributes — including the `ssspy.*` extension namespace defined in `CONTRACT.md` — are persisted to a JSONB column on the span row; nothing about `ssspy.*` is hard-coded into table or column names
- Resource attributes (e.g., `service.name`, `telemetry.sdk.*`) are persisted to a separate JSONB column on the span row
- Truncation flags emitted by the agent (`*.truncated` boolean attributes) are persisted alongside their content attributes in the JSONB; ssspy does not perform its own truncation
- A `traces` row exists for every distinct trace_id seen; it tracks the trace's earliest start_time, latest end_time, root_span_id (when known), span_count, and ingestion timestamp

---

## TI-3: Idempotency and out-of-order tolerance

Re-ingesting the same span is a no-op, and child spans may arrive before the root.

**Acceptance criteria:**
- Re-POSTing a span (same `span_id`) is a no-op: existing rows are not modified; no error is returned to the client; the request is treated as a successful ingest
- Spans arriving in any order — including child spans before their parent or before the root span — are accepted; a child span does not require its parent to already exist in storage
- The trace's aggregate metadata (`start_time`, `end_time`, `span_count`, `root_span_id`) is updated incrementally as spans arrive; ingestion never blocks waiting for a particular span
- When the root span (the one with no parent) arrives, the trace's `root_span_id` and `end_time` are populated from that span
- A trace that never receives its root span is still browsable: it appears in the list view with the data available, and the detail view renders whatever spans have arrived
- Ingestion of one span does not block or fail ingestion of other spans in the same batch; per-span errors are isolated where reasonable
- The OTLP `partial_success` response field is populated when some spans in a batch fail to ingest (the OTLP spec permits this); full-batch failures return HTTP `400`

---

## TI-4: Trace list view

The home page lists recently ingested traces with enough metadata to pick one to inspect.

**Acceptance criteria:**
- The route `GET /` renders a paginated list of traces ordered by start_time descending
- Each list row shows: the trace's start_time, the root span's `name`, its duration, its status, and any agent-identifying attributes present on the root span (e.g., `ssspy.agent.name`, `ssspy.agent.version`, `ssspy.investigation.target_url` for go-phish — but the columns are populated from JSONB, not from agent-specific schema)
- The list supports filtering by: agent name (exact match), target URL (substring match), agent version (exact match), and a start_time date range
- The list supports pagination with a fixed page size (specified in `design.md`); navigation uses query parameters
- Each list row links to the trace's detail view at `GET /traces/{trace_id}`
- The list page loads in under 200 ms for a database holding up to 10,000 traces (personal-scale upper bound; not a hard SLA)

---

## TI-5: Trace detail view

The detail view renders the full span tree for a single trace with payload inspection.

**Acceptance criteria:**
- The route `GET /traces/{trace_id}` (hex-encoded trace_id) renders the trace's spans as a nested tree reflecting parent–child relationships derived from `parent_span_id`
- Each span node displays: `name`, `start_time` relative to the trace start (e.g., `+0.12s`), duration, `status_code`, and `status_message` when present
- Each span node is expandable to show all attributes (native columns and JSONB) and all Resource attributes; attributes are presented in a stable order (alphabetical within each group) so visual diffs across traces are meaningful
- Long string attributes (those subject to the `CONTRACT.md` payload size policy: `ssspy.tool.input`, `ssspy.tool.output`, `ssspy.investigation.outcome`) are rendered collapsed by default with an explicit expand affordance; the truncation flag (when present) is shown next to the attribute
- JSON-encoded string attributes are pretty-printed when expanded
- A trace that has not yet received its root span renders the available spans with a visible "incomplete trace" indicator
- Orphan spans (spans whose `parent_span_id` is not present in the trace) are rendered at the top level alongside the root, not silently dropped

---

## TI-6: Agent-aware rendering hook

The detail view may render an agent-specific layout when the root span identifies a known agent. This is a UI affordance, not a storage concern.

**Acceptance criteria:**
- The default rendering (TI-5) works for any OTel trace, regardless of agent
- When the root span carries `ssspy.agent.name = "go-phish"`, the detail view renders an additional agent-specific section above the generic span tree: target URL, agent version, phase-ordered view of the four phases (`fetch`, `hypothesis`, `enrichment`, `synthesis`), and the structured outcomes of `hypothesis` and `synthesis` (parsed from `ssspy.investigation.outcome`)
- The agent-specific section is implemented as a separate template included conditionally; adding a future agent does not require modifying the generic rendering path
- If an attribute the agent-specific template expects is missing or malformed, the section degrades to the generic tree rather than erroring

---

## TI-7: Configuration and bootstrap

ssspy starts from a single CLI entry point with database migrations applied automatically.

**Acceptance criteria:**
- A `ssspy serve` CLI command starts the HTTP server (OTLP receiver + web UI on the same FastAPI app)
- Database migrations run automatically at server startup; if migrations fail, the process exits non-zero with a clear error
- Configuration is read from environment variables: at minimum `DATABASE_URL` (PostgreSQL connection string), `SSSPY_LISTEN_ADDR` (default `0.0.0.0:4318`), and `SSSPY_MAX_BODY_BYTES` (default `4194304`)
- The OTLP receiver and the web UI are served from the same process and the same port (FastAPI mounts both); the web UI is reachable at the same host:port as `/v1/traces`
- Logs are structured (JSON or key=value, specified in `design.md`) and include at minimum: HTTP method, path, status, duration, and span count for ingest requests

---

## TI-8: Pydantic typing discipline

All trace data crossing module boundaries is typed.

**Acceptance criteria:**
- OTLP request bodies are parsed into Pydantic v2 models; the receiver never passes raw dicts or protobuf objects past the normalization layer
- The canonical Span shape used internally is a Pydantic v2 model with explicit field types
- Where attributes flow into JSONB, the JSONB contents are still Pydantic-validated dicts at write time (string keys, JSON-serializable values); the validator rejects values that cannot be JSON-encoded
- A failed Pydantic validation in the ingest path produces an HTTP 400 response with a brief reason; it does not crash the receiver

---

## TI-9: Ingest failure modes

Receiver behavior under malformed input, oversized payloads, and database failures is well-defined.

**Acceptance criteria:**
- Unparseable protobuf or JSON: HTTP `400`, no rows written
- Valid OTLP envelope containing zero spans: HTTP `200`, no rows written, no error
- Span missing required fields (no `span_id`, no `trace_id`, no `name`, no `start_time`, no `end_time`): the span is rejected; other spans in the same batch still ingest; the OTLP `partial_success` field records the rejected count
- Payload exceeding `SSSPY_MAX_BODY_BYTES`: HTTP `413` before parsing; no partial write
- Database unavailable mid-request: HTTP `503`; the client (the agent's exporter) is expected to retry per OTLP semantics; no partial state is left in the database for that batch (transactional ingest)
- Operational errors (DB connection lost, disk full) are logged at error level with enough context to debug; the process does not exit on a single failed request

---

## TI-10: Real-trace verification

Slice 1 is considered done only after a real go-phish trace has flowed through end-to-end.

**Acceptance criteria:**
- Running go-phish against a live URL with `OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:<ssspy-port>` produces a trace that appears in ssspy's list view within seconds of the investigation completing
- The trace's detail view shows the expected span count (5 for `--skip-llm`, 13+ for a full investigation), the correct parent–child hierarchy (root → 4 phases → chat/tool children), and the expected attributes (`ssspy.investigation.target_url`, `ssspy.agent.version`, `gen_ai.*` on chat spans, `ssspy.tool.input`/`output` on tool spans, `ssspy.screenshot.sha256` on the hypothesis chat span)
- The captured fixtures (`tests/fixtures/full.otlp.json`, `tests/fixtures/skip-llm.otlp.json`) can be replayed via the receiver and produce byte-identical row state in the database across runs
- A second ingest of the same trace produces zero new or modified rows (TI-3 idempotency, verified end-to-end)

---

## Out of scope

- File-based trace ingestion (stdouttrace NDJSON, `OTEL_FILE_EXPORTER_PATH`). OTLP HTTP is the only supported ingest path in Slice 1.
- gRPC OTLP transport (`:4317`). HTTP only.
- Authentication, authorization, or multi-tenancy. ssspy is a single-user localhost tool.
- Real-time streaming or SSE updates to the UI. Polling/refresh is sufficient.
- Trace retention, TTL, or deletion. Traces accumulate indefinitely; pruning is deferred.
- OTel metrics or logs. Traces only.
- Sampling or rate limiting at the receiver. The agent emits at full fidelity and ssspy accepts what arrives.
- Cost or latency APM dashboards. Aggregate metrics views are Slice 4's concern.
- Grading UI, rubric storage, or judge orchestration. Slices 2-3.
- Eval runs and version comparison. Slice 4.
- Support for agents other than go-phish. Storage is generic OTel and *can* accept other agents' traces; rendering is not exercised against any agent other than go-phish in this slice. Adding agent-specific UI for a second agent is a future slice.
- An OTel Collector deployment. ssspy is the collector for this slice's purposes; if a separate Collector is introduced later, it sits in front of ssspy and ssspy's receiver contract is unchanged.
