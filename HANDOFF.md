# ssspy — Observability & Eval Platform Handoff

## What we're building

`ssspy` ingests traces from agents instrumented with OpenTelemetry (initially go-phish, extensible to any OTel-emitting agent). It provides:

1. **Trace exploration** — browse investigations, explore span hierarchy, inspect payloads
2. **Human grading** — rate traces against rubrics you define
3. **LLM-as-judge grading** — run Claude as a grader against the same rubrics, compare to human grades
4. **Eval runs and version comparison** — track how agent outputs change across code versions
5. **Aggregate metrics and analysis** — spot patterns over time: calibration, agreement, per-criterion accuracy

The point: you can instrument your agents, grade their outputs, understand what's working, and use that insight to improve future agents.

## What go-phish is emitting (the contract)

go-phish's `CONTRACT.md` is the source of truth. Here are the cliff notes:

**Two export paths:**
- OTLP HTTP to `http://localhost:4318` (standard OTel protocol, any collector works)
- File fallback to `OTEL_FILE_EXPORTER_PATH` as newline-delimited JSON (stdouttrace format, not OTLP proto JSON — see note below)

**Four span types:**

1. `ssspy.investigation` (root)
   - Attributes: `ssspy.investigation.id` (UUID), `ssspy.investigation.target_url` (normalized), `ssspy.agent.name` ("go-phish"), `ssspy.agent.version` (git SHA or "dev")
   
2. `ssspy.phase.{fetch|hypothesis|enrichment|synthesis}`
   - Attributes: `ssspy.investigation.phase`, `ssspy.investigation.phase_index` (1–4)
   - Conditional: `ssspy.investigation.outcome` (JSON-encoded output, hypothesis/synthesis phases only, 32 KB inline limit)

3. `chat {model}` (LLM calls)
   - GenAI semconv attributes: `gen_ai.operation.name`, `gen_ai.provider.name`, `gen_ai.request.model`, token counts, finish reasons
   - ssspy extensions (hypothesis phase only): `ssspy.screenshot.content_type`, `ssspy.screenshot.size_bytes`, `ssspy.screenshot.sha256` (screenshots never inlined, only hashed)

4. `execute_tool {tool_name}` (tool calls, enrichment phase only)
   - Attributes: `gen_ai.tool.name`, `gen_ai.tool.call.id`, `ssspy.tool.input`, `ssspy.tool.output` (both 32 KB truncation, with `.truncated` flag if exceeded)

**Key contracts:**
- Payload truncation: 32 KB threshold, valid UTF-8 boundary
- Token math: `gen_ai.usage.input_tokens` is aggregate (raw + cache_read + cache_creation); individual cache fields also present
- Tool spans are siblings of LLM spans (children of phase span), not children of the LLM call that triggered them
- No redaction — full prompts, URLs, outputs are emitted as-is
- Semconv: pinned to v1.41.0 (hand-declared constants, not from the lagging Go package)

**Important quirks:**
- File exporter format is stdouttrace JSON, not OTLP proto JSON. Each line is a self-contained JSON object. Perfectly readable with `jq`, but if you want OTLP JSON from file, either run a local collector or write a thin adapter.
- `--skip-llm` mode produces only 5 spans (root + 4 phases, no chat or execute_tool spans). ssspy should handle this gracefully.
- Tool-to-LLM call correlation can't be inferred from span structure alone — it's embedded in Anthropic's response content blocks. If you need that mapping, extract it from the LLM span's response payload, don't trust parentage.

See go-phish's `CONTRACT.md` for the full spec.

---

## Stack

- **Language:** Python 3.12+
- **Web framework:** FastAPI
- **Schemas:** Pydantic v2, used aggressively for trace/span deserialization, rubric schemas, judge outputs. Type-safe parsing is foundational.
- **Structured LLM outputs:** `instructor` + Anthropic Python SDK for reliable judge grading
- **Database:** PostgreSQL + Alembic migrations
- **OTel ingestion:** `opentelemetry-api` + `opentelemetry-exporter-otlp` for OTLP HTTP receiver; custom JSON parser for file format
- **Frontend:** HTMX + Jinja templates (server-side rendering, no SPA)
- **Analysis:** pandas, scipy.stats for metrics (inter-rater agreement, calibration curves)
- **Plotting:** server-rendered matplotlib or plotly as HTML

---

## Four feature slices

Build in order. Each slice has its own requirements/design/tasks.

### Slice 1: `trace-ingestion`

**Done when:** you can run go-phish, point ssspy at its traces (OTLP or file), and browse them in a web UI with full span detail.

Core work:
- OTLP HTTP receiver (listens on `localhost:4318`)
- JSON file ingester (reads stdouttrace format from a watched path)
- Postgres schema for investigations, spans, span attributes (JSONB for arbitrary attrs)
- Trace list view (paginated, filterable)
- Trace detail view (span tree with payload inspection)
- Ingest go-phish's `CONTRACT.md` attributes as required columns in span table (investigation_id, phase, agent_name, etc.) for fast queries

Design considerations for `design.md`:
- How to handle the two export formats (OTLP vs stdouttrace JSON). Can they coexist or pick one?
- Span table structure: flatten go-phish attributes as columns, or store all attributes as generic JSONB?
- Payload storage: inline up to 32 KB, spill large payloads to separate table?
- Trace lifecycle: when do old traces get deleted? (personal project; "never" is fine for now)

### Slice 2: `human-grading`

**Done when:** you can hand-grade 20+ go-phish traces against a rubric and see inter-grader agreement with yourself (if you grade the same trace twice).

Core work:
- Rubric schema (criteria, score types: binary/categorical/ordinal, definitions)
- Rubric CRUD (at least create/read; delete is optional)
- Grading UI: pick a trace, pick a rubric, score each criterion with justification text, submit
- Grade storage (trace_id, rubric_id, grader_id, scores, justifications)
- Grade view per trace (list of all grades for this trace + rubric)

Rubric to start with (for Slice 2 testing):
- "Phase 2 Hypothesis Quality" — 3 criteria: (1) Brand correctly identified (binary), (2) Confidence calibration (ordinal: overconfident / well-calibrated / underconfident), (3) Reasoning shown (binary)
- Once this works, add more rubrics as you need them

Design considerations:
- Rubric versioning — do old grades stay tied to old rubric versions, or assume rubric is static for now?
- Score types representation — how to store a mix of binary/categorical/ordinal scores in one table?
- Grading workflow — single-screen form or multi-step?

### Slice 3: `llm-judge`

**Done when:** you have 20 traces graded by you, run the judge against all 20, and can see per-criterion agreement (Cohen's kappa for categorical, etc.).

Core work:
- Judge orchestration: take a trace + rubric, call Claude with structured output to produce scores
- Store judge grades (alongside human grades, distinguished by grader type)
- Agreement analysis: per-criterion agreement, Cohen's kappa (categorical), Spearman (ordinal), accuracy (binary)
- Agreement view in UI: comparison table showing human grade vs judge grade per criterion, agreement metric, confidence in the disagreement
- Calibration check: when judge says "high confidence" (if your score schema includes confidence), how often is it right?

Design considerations:
- Judge prompt structure — single-pass or chain-of-thought? How much context from the trace?
- Confidence elicitation — does the judge produce a per-score confidence value?
- Failure modes — explicitly note potential positional bias, over/under-confidence. Document how you'd detect them.
- Which rubric criteria are the judge *allowed* to score? Some (e.g., "did the agent find the exfil destination") have clear signal in the trace. Others ("how well-reasoned is this") are fuzzy. Decide which are grader-safe.

### Slice 4: `eval-runs`

**Done when:** you change a go-phish prompt, run both old and new versions against the same test URLs, and see a side-by-side showing per-criterion accuracy shift.

Core work:
- Version tagging: each go-phish run gets tagged with a version (git SHA, semver, or whatever schema you use)
- Eval run concept: a batch of investigations all from the same version, all against the same test URL set, producing a set of traces
- Comparison UI: pick two versions, see metrics side-by-side (per-criterion accuracy, judge agreement, hallucination rate)
- Aggregate metrics view: across all traces of a version, show per-criterion score distribution (as a table and histogram), judge-vs-human agreement heatmap, calibration curve

Design considerations:
- How to define a "test URL set"? Start simple: a fixed list of 10–20 known-good phishing URLs (from PhishTank or your own labeled set)
- Version schema: git SHA for local dev, something user-friendly for production (neither needed for personal use; pick one and be consistent)
- Metrics definitions: what exactly is "hallucination rate"? (Probably: per-claim confidence vs actual correctness, plotted as a scatter to visualize calibration)
- Which metrics matter most for *your* eval? Don't compute everything; pick 3–5 that tell you what's working

---

## Working agreements

- **Specs before code.** Same rule as go-phish.
- **Real traces from day 1.** Don't mock traces. Run go-phish and point ssspy at actual output.
- **Dogfood the UI.** You're the user. If the grading interface feels awkward, that's a real signal — iterate until you enjoy using it.
- **Pydantic everywhere.** Traces are deserialized into Pydantic models. Rubrics are Pydantic. Judge outputs are instructor-validated Pydantic. No untyped dicts in the main paths.
- **When the judge does something dumb, document it first.** Create an `agent-notes.md`. "Judge confidently says brand X but it's actually Y; hypothesis is [guess]" — file it before changing anything. This is how you learn.
- **Commit per task.** Git history tells the story of capability progression.

---

## Out of scope for v1

- Multi-tenancy or auth (localhost single-user only)
- Real-time alerting
- Support for any agent other than go-phish (extensibility is free once it works; don't design for it upfront)
- Cost/latency APM (span timestamps are enough)
- Cloud deployment
- Automatic fixing or code generation (that's future work after you understand the data)
- Streaming/live updates (polling is fine)
