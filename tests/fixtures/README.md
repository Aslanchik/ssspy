# Captured OTLP fixtures

Two binary protobuf bodies live here:

- `full.otlp.bin` — one full go-phish investigation (root + 4 phases + chat + tool spans)
- `skip-llm.otlp.bin` — the 5-span minimum (root + 4 phases, no LLM/tool children)

`tests/ingest/_fixtures.py` prefers these files when present and falls back to
synthetic envelopes when they aren't, so the test suite runs in either state.

## Re-capturing

```sh
# Terminal 1: stand up the one-shot recorder.
python scripts/capture_fixtures.py --out tests/fixtures/full.otlp.bin

# Terminal 2: run go-phish against the recorder.
OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4318 \
    ./gophish https://example.com
```

The recorder writes the first POST body it receives and exits. Repeat with
`--skip-llm` and the `skip-llm.otlp.bin` output path for the second fixture.
