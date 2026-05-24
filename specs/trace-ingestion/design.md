# trace-ingestion: Design

## Overview

A single FastAPI process serves both the OTLP HTTP receiver (`POST /v1/traces`) and the web UI (`GET /`, `GET /traces/{trace_id}`). Incoming OTLP payloads — protobuf or JSON — are parsed into Pydantic models, flattened into a canonical Span shape, and persisted to PostgreSQL in a single transaction per request. Storage is OTel-generic: standard OTel + GenAI semconv attributes become typed columns, everything else (including the entire `ssspy.*` namespace) goes to JSONB. The detail view renders a generic span tree by default and includes an agent-specific partial above it when the root span carries `ssspy.agent.name = "go-phish"`.

---

## Stack

| Layer | Choice | Rationale |
|---|---|---|
| Python | 3.12+ | Per handoff |
| Web | FastAPI + uvicorn | Async, mounts both API and HTML routes |
| Schemas | Pydantic v2 | Per handoff |
| Settings | `pydantic-settings` | Env-based config, Pydantic-native |
| Database | PostgreSQL 16 + asyncpg | Per handoff |
| Migrations | Alembic | Per handoff |
| SQL | SQLAlchemy 2.0 Core (no ORM) | Async query builder; Alembic integration; no ORM overhead |
| OTLP decode | `opentelemetry-proto` | Generated Python protobuf classes for `ExportTraceServiceRequest` |
| Templates | Jinja2 (via `fastapi.templating.Jinja2Templates`) | Per handoff |
| Frontend | HTMX (CDN-loaded) | Per handoff; no build step |
| Logging | stdlib `logging` + custom key=value formatter | Readable in dev; no need for JSON logging at personal scale |
| Tests | pytest + pytest-asyncio + httpx | Standard async stack |

No celery, no redis, no queue. Ingest is synchronous inside the request handler — at personal scale a full go-phish trace (13 spans, ~0.5 MB worst case) commits in a few ms.

---

## Repo structure

```
ssspy/
├── pyproject.toml
├── alembic.ini
├── alembic/
│   ├── env.py
│   └── versions/
│       └── 0001_initial.py
├── src/ssspy/
│   ├── __init__.py
│   ├── main.py                 # FastAPI app factory + lifespan
│   ├── cli.py                  # `ssspy serve`
│   ├── config.py               # pydantic-settings Settings
│   ├── db.py                   # async engine + session dependency
│   ├── logging.py              # stdlib logging setup
│   │
│   ├── models/                 # Pydantic v2
│   │   ├── __init__.py
│   │   ├── otlp.py             # OTLP HTTP wire models (subset we use)
│   │   └── span.py             # canonical Span + Trace (post-normalization)
│   │
│   ├── ingest/
│   │   ├── __init__.py
│   │   ├── routes.py           # POST /v1/traces
│   │   ├── decode.py           # protobuf | JSON → Pydantic OtlpRequest
│   │   ├── normalize.py        # OtlpRequest → list[CanonicalSpan]
│   │   └── store.py            # batch upsert into traces + spans
│   │
│   ├── store/
│   │   ├── __init__.py
│   │   ├── traces.py           # list + get-by-id queries
│   │   └── spans.py            # spans-by-trace query
│   │
│   └── web/
│       ├── routes.py           # GET /, GET /traces/{trace_id}, GET /health
│       ├── templates/
│       │   ├── base.html
│       │   ├── list.html
│       │   ├── detail.html
│       │   ├── _span.html      # recursive macro for one span node
│       │   └── _agents/
│       │       └── go-phish.html
│       └── static/
│           └── style.css
├── tests/
│   ├── conftest.py
│   ├── fixtures/
│   │   ├── full.otlp.bin       # captured OTLP protobuf body, full investigation
│   │   └── skip-llm.otlp.bin
│   ├── ingest/
│   │   ├── test_decode.py
│   │   ├── test_normalize.py
│   │   ├── test_store.py
│   │   └── test_routes.py
│   └── web/
│       └── test_routes.py
└── scripts/
    └── capture_fixtures.py     # one-shot: run go-phish through a recording proxy, save bodies
```

---

## Configuration

`src/ssspy/config.py`:

```python
class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="SSSPY_", env_file=".env")

    database_url: PostgresDsn
    listen_addr: str = "0.0.0.0:4318"
    max_body_bytes: int = 4 * 1024 * 1024     # 4 MB
    log_level: str = "INFO"
```

Read once at startup; injected as a FastAPI dependency where needed.

---

## Database schema (Alembic 0001)

```sql
CREATE EXTENSION IF NOT EXISTS pg_trgm;

CREATE TABLE traces (
    trace_id      BYTEA PRIMARY KEY,
    root_span_id  BYTEA,
    start_time    TIMESTAMPTZ NOT NULL,
    end_time      TIMESTAMPTZ,                  -- NULL until root arrives
    span_count    INTEGER NOT NULL DEFAULT 0,
    ingested_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX traces_start_time_desc ON traces (start_time DESC);

CREATE TABLE spans (
    span_id        BYTEA PRIMARY KEY,
    trace_id       BYTEA NOT NULL REFERENCES traces(trace_id) ON DELETE CASCADE,
    parent_span_id BYTEA,                       -- NULL for root
    name           TEXT NOT NULL,
    kind           SMALLINT NOT NULL,           -- OTel SpanKind enum (0..5)
    start_time     TIMESTAMPTZ NOT NULL,
    end_time       TIMESTAMPTZ NOT NULL,
    duration_ms    INTEGER GENERATED ALWAYS AS
                     (EXTRACT(EPOCH FROM (end_time - start_time)) * 1000)::INT STORED,
    status_code    TEXT NOT NULL,               -- 'Ok' | 'Error' | 'Unset'
    status_message TEXT,

    -- GenAI semconv v1.41.0 — typed columns
    gen_ai_operation          TEXT,
    gen_ai_provider           TEXT,
    gen_ai_request_model      TEXT,
    gen_ai_response_model     TEXT,
    gen_ai_response_id        TEXT,
    gen_ai_finish_reason      TEXT,             -- first element of finish_reasons[]
    input_tokens              INTEGER,
    output_tokens             INTEGER,
    cache_read_tokens         INTEGER,
    cache_creation_tokens     INTEGER,
    tool_name                 TEXT,
    tool_call_id              TEXT,

    -- Everything else (ssspy.*, future attrs)
    attributes JSONB NOT NULL DEFAULT '{}'::jsonb,
    resource   JSONB NOT NULL DEFAULT '{}'::jsonb
);
CREATE INDEX spans_trace_id  ON spans (trace_id);
CREATE INDEX spans_parent    ON spans (parent_span_id);
CREATE INDEX spans_tool_name ON spans (tool_name) WHERE tool_name IS NOT NULL;
CREATE INDEX spans_model     ON spans (gen_ai_request_model) WHERE gen_ai_request_model IS NOT NULL;
CREATE INDEX spans_attrs_gin ON spans USING gin (attributes jsonb_path_ops);

-- Accelerators for the list view (filters on root spans)
CREATE INDEX spans_root_agent_name
    ON spans ((attributes->>'ssspy.agent.name'))
    WHERE parent_span_id IS NULL;
CREATE INDEX spans_root_agent_version
    ON spans ((attributes->>'ssspy.agent.version'))
    WHERE parent_span_id IS NULL;
CREATE INDEX spans_root_target_url_trgm
    ON spans USING gin ((attributes->>'ssspy.investigation.target_url') gin_trgm_ops)
    WHERE parent_span_id IS NULL;
```

**Note on `traces.span_count`:** denormalized; updated on every insert. Source of truth is `COUNT(*) FROM spans WHERE trace_id = ?`; the column is for cheap list-view rendering.

**Cascade delete:** `spans` rows go with the trace if the trace row is deleted. Manual `DELETE FROM traces WHERE trace_id = ?` is the supported deletion path (not exposed in UI in Slice 1).

---

## Pydantic models

### `models/otlp.py`

A minimal subset of the OTLP v1 trace schema — enough to validate what flows through the receiver. We do **not** model every OTLP field, only those we persist or route on.

```python
class OtlpAnyValue(BaseModel):
    string_value: str | None = Field(None, alias="stringValue")
    bool_value:   bool | None = Field(None, alias="boolValue")
    int_value:    str | None = Field(None, alias="intValue")     # protobuf JSON: int64 as string
    double_value: float | None = Field(None, alias="doubleValue")
    array_value:  "OtlpArrayValue | None" = Field(None, alias="arrayValue")
    kvlist_value: "OtlpKvList | None" = Field(None, alias="kvlistValue")
    bytes_value:  str | None = Field(None, alias="bytesValue")   # base64
    model_config = ConfigDict(populate_by_name=True)

class OtlpKeyValue(BaseModel):
    key: str
    value: OtlpAnyValue

class OtlpSpan(BaseModel):
    trace_id:  str                           # hex (when arriving via JSON) or bytes-as-hex
    span_id:   str
    parent_span_id: str | None = Field(None, alias="parentSpanId")
    name: str
    kind: int = 0                            # OTel SpanKind
    start_time_unix_nano: str = Field(alias="startTimeUnixNano")
    end_time_unix_nano:   str = Field(alias="endTimeUnixNano")
    attributes: list[OtlpKeyValue] = []
    status: "OtlpStatus | None" = None
    model_config = ConfigDict(populate_by_name=True)

# ResourceSpans, ScopeSpans, ExportTraceServiceRequest etc. — analogous.
```

The `from_protobuf(envelope: ExportTraceServiceRequest) -> OtlpRequest` classmethod uses `google.protobuf.json_format.MessageToDict(envelope, preserving_proto_field_name=False)` then `OtlpRequest.model_validate(...)`. Protobuf and JSON paths converge after this point.

### `models/span.py`

Canonical Span — what the rest of the codebase operates on:

```python
class CanonicalSpan(BaseModel):
    trace_id:       bytes                    # 16 bytes
    span_id:        bytes                    # 8 bytes
    parent_span_id: bytes | None
    name:           str
    kind:           int
    start_time:     datetime
    end_time:       datetime
    status_code:    Literal["Ok", "Error", "Unset"]
    status_message: str | None

    # GenAI semconv hot fields, populated when present
    gen_ai_operation:        str | None = None
    gen_ai_provider:         str | None = None
    gen_ai_request_model:    str | None = None
    gen_ai_response_model:   str | None = None
    gen_ai_response_id:      str | None = None
    gen_ai_finish_reason:    str | None = None
    input_tokens:            int | None = None
    output_tokens:           int | None = None
    cache_read_tokens:       int | None = None
    cache_creation_tokens:   int | None = None
    tool_name:               str | None = None
    tool_call_id:            str | None = None

    attributes: dict[str, Any] = Field(default_factory=dict)
    resource:   dict[str, Any] = Field(default_factory=dict)
```

`attributes` is the flattened, hot-field-stripped attribute dict: anything that became a typed column is removed from here. `resource` is the flattened Resource attributes verbatim.

---

## Ingest pipeline

### `ingest/routes.py`

```python
@router.post("/v1/traces")
async def ingest_traces(request: Request, settings: Settings = Depends(get_settings),
                        db: AsyncConnection = Depends(get_db)) -> Response:
    body = await request.body()
    if len(body) > settings.max_body_bytes:
        return Response(status_code=413)

    ct = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    try:
        otlp_req = decode_request(body, ct)
    except DecodeError as e:
        return Response(status_code=400, content=str(e), media_type="text/plain")

    spans, rejected = normalize_request(otlp_req)
    await store_spans(db, spans)

    resp = ExportTraceServiceResponse()
    if rejected:
        resp.partial_success.rejected_spans = len(rejected)
        resp.partial_success.error_message = "; ".join(r.reason for r in rejected[:5])

    if ct == "application/json":
        return JSONResponse(content=MessageToDict(resp))
    return Response(content=resp.SerializeToString(), media_type="application/x-protobuf")
```

### `ingest/decode.py`

Two entry points, one Pydantic output:

```python
def decode_request(body: bytes, content_type: str) -> OtlpRequest:
    if content_type == "application/x-protobuf":
        pb = ExportTraceServiceRequest()
        pb.ParseFromString(body)                 # raises DecodeError
        return OtlpRequest.from_protobuf(pb)
    if content_type == "application/json":
        return OtlpRequest.model_validate_json(body)
    raise DecodeError(f"unsupported content-type: {content_type}")
```

### `ingest/normalize.py`

Flattens the OTLP `AnyValue` union and routes hot semconv attributes into typed fields. Returns `(canonical_spans, rejected)` so per-span failures don't fail the batch.

```python
HOT_ATTRS: dict[str, tuple[str, Callable[[Any], Any]]] = {
    "gen_ai.operation.name":     ("gen_ai_operation",         str),
    "gen_ai.provider.name":      ("gen_ai_provider",          str),
    "gen_ai.request.model":      ("gen_ai_request_model",     str),
    "gen_ai.response.model":     ("gen_ai_response_model",    str),
    "gen_ai.response.id":        ("gen_ai_response_id",       str),
    "gen_ai.usage.input_tokens":            ("input_tokens",  int),
    "gen_ai.usage.output_tokens":           ("output_tokens", int),
    "gen_ai.usage.cache_read.input_tokens": ("cache_read_tokens",     int),
    "gen_ai.usage.cache_creation.input_tokens": ("cache_creation_tokens", int),
    "gen_ai.tool.name":          ("tool_name",                str),
    "gen_ai.tool.call.id":       ("tool_call_id",             str),
}
# gen_ai.response.finish_reasons is special-cased — first element extracted.

def flatten_value(av: OtlpAnyValue) -> Any:
    if av.string_value is not None:  return av.string_value
    if av.bool_value   is not None:  return av.bool_value
    if av.int_value    is not None:  return int(av.int_value)         # protobuf int64 as str
    if av.double_value is not None:  return av.double_value
    if av.bytes_value  is not None:  return base64.b64decode(av.bytes_value)
    if av.array_value  is not None:  return [flatten_value(v) for v in av.array_value.values]
    if av.kvlist_value is not None:  return {kv.key: flatten_value(kv.value) for kv in av.kvlist_value.values}
    return None

def normalize_span(otlp: OtlpSpan, resource: dict[str, Any]) -> CanonicalSpan:
    attrs = {kv.key: flatten_value(kv.value) for kv in otlp.attributes}
    hot: dict[str, Any] = {}
    for key, (field, caster) in HOT_ATTRS.items():
        if key in attrs:
            hot[field] = caster(attrs.pop(key))
    if "gen_ai.response.finish_reasons" in attrs:
        fr = attrs.pop("gen_ai.response.finish_reasons")
        hot["gen_ai_finish_reason"] = fr[0] if isinstance(fr, list) and fr else None

    return CanonicalSpan(
        trace_id=bytes.fromhex(otlp.trace_id) if isinstance(otlp.trace_id, str) else otlp.trace_id,
        span_id=bytes.fromhex(otlp.span_id) if isinstance(otlp.span_id, str) else otlp.span_id,
        parent_span_id=(bytes.fromhex(otlp.parent_span_id)
                        if otlp.parent_span_id else None) or None,   # empty bytes → None
        name=otlp.name,
        kind=otlp.kind,
        start_time=nano_to_dt(otlp.start_time_unix_nano),
        end_time=nano_to_dt(otlp.end_time_unix_nano),
        status_code=status_code_str(otlp.status),
        status_message=otlp.status.message if otlp.status else None,
        attributes=attrs,
        resource=resource,
        **hot,
    )
```

**Required-field validation** (per TI-9): missing/empty `trace_id`, `span_id`, `name`, or either timestamp → the span is appended to `rejected`, not raised. The batch continues.

### `ingest/store.py`

Single CTE per request — idempotent at span granularity, recomputes trace aggregates only from newly-inserted spans:

```python
SQL = text("""
WITH new_spans AS (
    INSERT INTO spans (
        span_id, trace_id, parent_span_id, name, kind, start_time, end_time,
        status_code, status_message,
        gen_ai_operation, gen_ai_provider, gen_ai_request_model, gen_ai_response_model,
        gen_ai_response_id, gen_ai_finish_reason,
        input_tokens, output_tokens, cache_read_tokens, cache_creation_tokens,
        tool_name, tool_call_id,
        attributes, resource
    )
    SELECT * FROM jsonb_to_recordset(:rows::jsonb) AS r(
        span_id BYTEA, trace_id BYTEA, parent_span_id BYTEA, name TEXT, kind SMALLINT,
        start_time TIMESTAMPTZ, end_time TIMESTAMPTZ, status_code TEXT, status_message TEXT,
        gen_ai_operation TEXT, gen_ai_provider TEXT, gen_ai_request_model TEXT,
        gen_ai_response_model TEXT, gen_ai_response_id TEXT, gen_ai_finish_reason TEXT,
        input_tokens INT, output_tokens INT, cache_read_tokens INT, cache_creation_tokens INT,
        tool_name TEXT, tool_call_id TEXT, attributes JSONB, resource JSONB
    )
    ON CONFLICT (span_id) DO NOTHING
    RETURNING trace_id, span_id, start_time, end_time, parent_span_id
),
agg AS (
    SELECT
        trace_id,
        MIN(start_time) AS min_start,
        COUNT(*) AS new_count,
        MIN(end_time) FILTER (WHERE parent_span_id IS NULL) AS root_end,
        (array_agg(span_id) FILTER (WHERE parent_span_id IS NULL))[1] AS root_id
    FROM new_spans
    GROUP BY trace_id
)
INSERT INTO traces (trace_id, root_span_id, start_time, end_time, span_count)
SELECT trace_id, root_id, min_start, root_end, new_count FROM agg
ON CONFLICT (trace_id) DO UPDATE SET
    root_span_id = COALESCE(traces.root_span_id, EXCLUDED.root_span_id),
    start_time   = LEAST(traces.start_time, EXCLUDED.start_time),
    end_time     = COALESCE(traces.end_time, EXCLUDED.end_time),
    span_count   = traces.span_count + EXCLUDED.span_count;
""")
```

Implementation note: **we cannot reference a child span's `trace_id` before the parent trace row exists**, because `spans.trace_id` is `REFERENCES traces(trace_id)`. The CTE above inserts spans first — but the FK is checked at statement time, not at CTE-stage time. So we either:

(a) **Drop the FK** — accept that orphan spans (whose trace row hasn't been created) can exist briefly. Cascade delete becomes a manual two-step.

(b) **Pre-insert trace stubs** — before the span CTE, `INSERT INTO traces (trace_id, start_time) ... ON CONFLICT DO NOTHING` for every distinct trace_id in the batch. Then the span CTE runs.

**Decision: (b).** Two statements per request inside one transaction; the FK stays for correctness. Stub insert uses a synthetic `start_time = now()` that the CTE then immediately overwrites with `LEAST(..., MIN(start_time))`.

---

## Web routes

### `GET /` — trace list

```python
@router.get("/")
async def list_traces(
    request: Request,
    page: int = 1,
    agent: str | None = None,
    version: str | None = None,
    url: str | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
    db: AsyncConnection = Depends(get_db),
):
    rows = await traces_store.list(db, page=page, size=50,
                                    agent=agent, version=version, url=url,
                                    since=since, until=until)
    return templates.TemplateResponse("list.html", {"request": request, "rows": rows, "filters": {...}})
```

The list query joins `traces` with the root span (one row per trace) and projects the JSONB attrs needed for display:

```sql
SELECT
    t.trace_id, t.start_time, t.end_time, t.span_count,
    s.name AS root_name, s.status_code, s.duration_ms,
    s.attributes->>'ssspy.agent.name'                  AS agent_name,
    s.attributes->>'ssspy.agent.version'               AS agent_version,
    s.attributes->>'ssspy.investigation.target_url'    AS target_url
FROM traces t
LEFT JOIN spans s ON s.span_id = t.root_span_id
WHERE  (:agent IS NULL   OR s.attributes->>'ssspy.agent.name' = :agent)
   AND (:version IS NULL OR s.attributes->>'ssspy.agent.version' = :version)
   AND (:url IS NULL     OR s.attributes->>'ssspy.investigation.target_url' ILIKE '%' || :url || '%')
   AND (:since IS NULL   OR t.start_time >= :since)
   AND (:until IS NULL   OR t.start_time <  :until)
ORDER BY t.start_time DESC
LIMIT :size OFFSET :offset;
```

LEFT JOIN handles the "no root yet" case — `root_name` and the JSONB extracts will be NULL; the template renders an "(incomplete)" badge.

### `GET /traces/{trace_id}` — detail

Fetch all spans for the trace, build a `parent → [children]` map in Python, render the recursive Jinja macro starting from each root-level span (the actual root + any orphans whose parent isn't in the trace).

```python
@router.get("/traces/{trace_id}")
async def trace_detail(trace_id: str, request: Request, db: AsyncConnection = Depends(get_db)):
    tid = bytes.fromhex(trace_id)
    trace = await traces_store.get(db, tid)
    if trace is None:
        raise HTTPException(404)
    spans = await spans_store.list_for_trace(db, tid)
    spans_by_parent: dict[bytes | None, list[Span]] = defaultdict(list)
    span_ids = {s.span_id for s in spans}
    for s in spans:
        # Treat orphan spans (parent not in trace) as top-level
        parent = s.parent_span_id if s.parent_span_id in span_ids else None
        spans_by_parent[parent].append(s)
    for kids in spans_by_parent.values():
        kids.sort(key=lambda s: s.start_time)
    agent_name = trace.root.attributes.get("ssspy.agent.name") if trace.root else None
    return templates.TemplateResponse("detail.html", {
        "request": request, "trace": trace,
        "roots": spans_by_parent[None], "spans_by_parent": spans_by_parent,
        "agent_name": agent_name,
    })
```

### Templates

`detail.html`:

```jinja
{% extends "base.html" %}
{% block content %}
  <h1>{{ trace.trace_id.hex() }}</h1>
  <dl class="trace-meta">
    <dt>Started</dt><dd>{{ trace.start_time }}</dd>
    <dt>Ended</dt>  <dd>{{ trace.end_time or "(incomplete)" }}</dd>
    <dt>Spans</dt>  <dd>{{ trace.span_count }}</dd>
  </dl>

  {% if agent_name == "go-phish" %}
    {% include "_agents/go-phish.html" %}
  {% endif %}

  <h2>Span tree</h2>
  <ol class="span-tree">
    {% for root in roots %}
      {% include "_span.html" with context %}
    {% endfor %}
  </ol>
{% endblock %}
```

`_span.html` (recursive include):

```jinja
{# expects: root, spans_by_parent #}
<li>
  <details>
    <summary>
      <code>{{ root.name }}</code>
      <span class="duration">{{ root.duration_ms }}ms</span>
      <span class="status status-{{ root.status_code|lower }}">{{ root.status_code }}</span>
    </summary>
    <dl class="attrs">
      {% for k, v in root.flat_attributes() %}
        <dt>{{ k }}</dt><dd>{{ render_attr_value(k, v) }}</dd>
      {% endfor %}
    </dl>
    {% set children = spans_by_parent.get(root.span_id, []) %}
    {% if children %}
      <ol class="span-tree">
        {% for child in children %}
          {% with root=child %}{% include "_span.html" %}{% endwith %}
        {% endfor %}
      </ol>
    {% endif %}
  </details>
</li>
```

`render_attr_value(k, v)` is a Jinja filter that wraps long-payload attrs (`ssspy.tool.input`, `ssspy.tool.output`, `ssspy.investigation.outcome`) in a `<details>` and pretty-prints JSON.

`_agents/go-phish.html` reads the root span's attributes and shows:
- target_url, agent_version
- The four phase spans in order (by `phase_index`)
- Phase 2 / Phase 4 `outcome` JSON pretty-printed in a callout

If any expected attribute is missing or unparseable, the partial silently renders empty fields. The generic tree below is unaffected.

---

## CLI

```python
# src/ssspy/cli.py
import uvicorn, typer
from .config import Settings
from .main import create_app

app = typer.Typer()

@app.command()
def serve():
    settings = Settings()
    host, port = settings.listen_addr.rsplit(":", 1)
    uvicorn.run(create_app(settings), host=host, port=int(port), log_level=settings.log_level.lower())

if __name__ == "__main__":
    app()
```

Migrations run during FastAPI lifespan startup:

```python
@asynccontextmanager
async def lifespan(app: FastAPI):
    cfg = Config("alembic.ini")
    await asyncio.to_thread(command.upgrade, cfg, "head")
    yield
```

---

## Logging

stdlib `logging` with a simple key=value formatter:

```
2026-05-21T15:36:20.123 INFO ingest method=POST path=/v1/traces status=200 spans=4 dur_ms=8
```

The ingest route logs: HTTP method/path/status/duration/spans-ingested/spans-rejected. The web routes log: method/path/status/duration. Errors log with traceback at `ERROR` level.

---

## Fixture capture

`scripts/capture_fixtures.py` is a one-shot tool: it stands up a minimal HTTP server on a free port that accepts `POST /v1/traces`, dumps the raw request body to a file, and exits after the first request. To capture: run the script, then run go-phish with `OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:<port>`. Output is binary protobuf — the exact wire bytes the real receiver will eat.

Two fixtures captured: `full.otlp.bin` (full LLM run), `skip-llm.otlp.bin` (5 spans). Both stored under `tests/fixtures/`. Tests POST these bytes directly through the receiver's TestClient to validate end-to-end ingest.

---

## Open decisions resolved here

**FastAPI for both receiver and UI, same port.** Per TI-7. Alternative — separate ports for ingest and UI — adds an operational surface (two listen addresses, two CORS configs, two log streams) for zero benefit at single-user scale. The receiver and UI never share a route, so collocation is safe.

**SQLAlchemy Core + asyncpg, not ORM.** The schema is simple (two tables), queries are explicit, and the ORM's session/identity-map machinery buys us nothing here. Core gives us parameterized SQL and Alembic compatibility; we keep raw `text()` for the ingest CTE.

**Pre-insert trace stubs before span insert.** Resolves the FK ordering question (see `ingest/store.py`). The stub row's `start_time` is `now()`, immediately corrected by the subsequent CTE's `LEAST(...)`. Two statements per request; both transactional.

**Trace aggregates updated only from newly-inserted spans.** Idempotent re-POST does not double-count `span_count` nor move `end_time` around. The CTE's `RETURNING` from the `ON CONFLICT DO NOTHING` insert gives us exactly the new spans; aggregates derive from that set.

**`end_time` of trace = root span's end_time only.** Not `MAX(end_time) over all spans`. Until the root arrives, `end_time` is NULL and the UI labels the trace "(incomplete)". This matches what users expect — a trace ends when the investigation ends, not when the latest in-flight child finishes.

**JSONB stores attributes minus hot fields.** When a hot field (e.g., `gen_ai.request.model`) is promoted to a typed column, it is removed from `attributes`. No duplication. The detail view's "attributes" list reads both columns and JSONB and merges them for display.

**Resource is per-span, not per-trace.** In OTLP the Resource is per-ResourceSpans (a batch of spans from the same source). Persisting it on `spans.resource` is correct per-span; it's redundant when all spans in a trace share a Resource but cheap (JSONB compression handles it) and avoids a join. Slice 1 doesn't query Resource.

**Pagination = offset/limit, page size 50.** Cursor-based pagination is more correct but unneeded at this scale. We can switch later without schema changes.

**Orphan spans render at top level.** A span whose `parent_span_id` isn't in the trace (e.g., partial-trace mid-flight, or buggy emitter) renders next to the root rather than vanishing. Per TI-5.

**Spans are sorted by `start_time` within each level.** Not by span_id order, not by insertion order. Sibling spans (e.g., the four tool spans under enrichment) appear in chronological order in the UI.

**No retention.** No background job, no TTL, no cron. Manual `DELETE FROM traces WHERE trace_id = ?` is the only deletion path; the cascade handles spans.

**Recursive Jinja include for span tree.** Alternative — render the tree to HTML in Python, return a fragment — is faster but harder to style. The trees we render are tiny (≤ ~50 spans); Jinja recursion is fine.

**`opentelemetry-proto`, not a hand-written protobuf parser.** Small dep, generated from the canonical schema, kept in sync with upstream. The alternative (parsing protobuf wire format by hand) is interview-trivia work with no payoff.

**HTMX served from CDN, no build step.** Per handoff. Keeps the project a pure Python install.

**Logging is key=value, not JSON.** At personal scale, line logs in a terminal beat structured logs piped into nothing. Easy to swap if needed.

---

## Files created

| File | Purpose |
|---|---|
| `pyproject.toml` | Dependencies, package metadata, entry points |
| `alembic.ini`, `alembic/env.py`, `alembic/versions/0001_initial.py` | Migrations |
| `src/ssspy/main.py` | FastAPI app factory + lifespan |
| `src/ssspy/cli.py` | `ssspy serve` |
| `src/ssspy/config.py` | Settings |
| `src/ssspy/db.py` | Async engine, session dep |
| `src/ssspy/logging.py` | Logging setup |
| `src/ssspy/models/otlp.py` | OTLP wire models |
| `src/ssspy/models/span.py` | Canonical Span model |
| `src/ssspy/ingest/routes.py` | POST /v1/traces |
| `src/ssspy/ingest/decode.py` | protobuf | JSON → Pydantic |
| `src/ssspy/ingest/normalize.py` | OTLP → CanonicalSpan |
| `src/ssspy/ingest/store.py` | Batch upsert SQL |
| `src/ssspy/store/traces.py` | List + get-by-id |
| `src/ssspy/store/spans.py` | Spans-by-trace |
| `src/ssspy/web/routes.py` | GET /, GET /traces/{id}, GET /health |
| `src/ssspy/web/templates/*.html` | Jinja templates |
| `src/ssspy/web/static/style.css` | Minimal styling |
| `tests/fixtures/full.otlp.bin` | Captured full-investigation OTLP body |
| `tests/fixtures/skip-llm.otlp.bin` | Captured skip-llm OTLP body |
| `tests/ingest/*.py` | Decode / normalize / store / route tests |
| `tests/web/test_routes.py` | List + detail render tests |
| `scripts/capture_fixtures.py` | Fixture capture utility |

---

## Out of scope

Anything not listed above. Specifically: no auth, no rate limiting, no gRPC, no file ingest, no SSE, no live updates, no metrics, no retention, no second-agent rendering, no judge integration, no grading UI, no batch admin API.
