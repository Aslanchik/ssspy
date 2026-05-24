---
name: unit-test-expand
description: Increase test coverage by targeting untested branches and edge cases in Go packages. Use when the user asks to write tests, expand coverage, or test edge cases.
---

# Expand Unit Tests (Go)

Expand existing unit tests using Go's standard `testing` package.

1. **Analyze coverage**: Run `go test -coverprofile=coverage.out ./... && go tool cover -func=coverage.out` to identify untested functions and branches
2. **Identify gaps**: Review code for logical branches, error paths, boundary conditions, and empty/nil inputs
3. **Write tests** targeting:
   - Error handling paths (what happens when the DB is down, LLM call fails, container exits non-zero)
   - Boundary values (empty DOM, malformed JSON from container, missing env vars)
   - State transitions (investigation status changes)
   - Interface contracts (tool input/output schemas)
4. **Follow existing patterns**: use table-driven tests where there are multiple input/output cases; use `t.Helper()` in shared assertion helpers
5. **Verify improvement**: re-run coverage and confirm measurable increase

Focus areas by package:
- `internal/fetcher/` — JSON parsing, error propagation from container
- `internal/hypothesis/` — DOM summary extraction, tool_use response parsing
- `internal/db/` — query functions (use a real test DB, not mocks)
- `internal/report/` — formatting edge cases (empty fields, long strings)

Present new test code blocks only. Match the naming and structure of existing tests in the package.
