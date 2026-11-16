# Changelog

## 1.0.0

### Added
- **HTTP service** (`triage.server`): `/v1/triage`, `/v1/runs`, `/v1/runs/{id}`, `/trace`, `/resume`, `/healthz`, `/readyz`, `/metrics`; bearer auth, validation, body/concurrency limits, graceful shutdown.
- **Run lifecycle**: statuses (`completed`, `needs_review`, `failed`, `budget_exceeded`, `timeout`), partial results on failure, SQLite persistence of runs/checkpoints/traces, resume from checkpoint.
- **Guardrails**: per-run budgets (calls, tokens, cost) and deadline; tool allow-list with validated arguments; sandboxed code source; prompt-injection scrubbing of tool output; secret redaction.
- **Observability**: Prometheus metrics, JSON structured logs, trace status/error on spans.
- **Real model support** (`triage.providers`): streaming Anthropic and OpenAI-compatible clients with measured TTFT and provider token usage.
- **Pluggable data sources**: `LogSource` / `CodeSource` interfaces.
- Packaging and delivery: `pyproject.toml`, Dockerfile, docker-compose, Makefile, CI (mypy + tests + benchmark + container smoke test).
- Docs: architecture, security model, runbook.

### Changed
- Tool output is wrapped in `<tool_output>` tags and agent prompts mark it as untrusted (adds about 7% to input tokens in the baseline and about 18% in the optimized profile, where prompts are smaller; benchmark refreshed).
- `code_investigator` handles an unreadable file instead of raising.
- Suffix resolution of file names is exact (`Service.java` no longer matches `OrderService.java`).

### Initial build (pre-1.0)
- Supervisor + ReAct specialists + self-reflecting critic on a LangGraph-compatible state-graph runtime; tracing with tokens, cost, tokens/sec, TTFT; 5-config optimization ablation; LangGraph port.
