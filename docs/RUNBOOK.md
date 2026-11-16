# Runbook

## Start / stop

```bash
export TRIAGE_SERVER_TOKEN=$(openssl rand -hex 24)   # required for any non-loopback bind
export TRIAGE_DB=/var/lib/triage/triage.db
python -m triage.server            # or: docker compose up --build
```
`SIGTERM` / `SIGINT` stop accepting connections and wait for in-flight runs to finish before exiting
(`server_stopped` is the last log line). Give your orchestrator a termination grace period at least as
long as `TRIAGE_RUN_TIMEOUT_S`.

Probes: liveness `GET /healthz`, readiness `GET /readyz` (checks the database). Both are unauthenticated.

## Configuration

| Variable | Default | Meaning |
|---|---|---|
| `TRIAGE_HOST` / `TRIAGE_PORT` | `127.0.0.1` / `8080` | Bind address. Non-loopback requires a token |
| `TRIAGE_SERVER_TOKEN` | unset | Bearer token for everything except health probes |
| `TRIAGE_ALLOW_INSECURE` | `false` | Allow a non-loopback bind without a token (don't) |
| `TRIAGE_DB` | `triage.db` | SQLite file |
| `TRIAGE_MAX_CONCURRENT` | `4` | Concurrent runs; more get `429` |
| `TRIAGE_MAX_BODY_BYTES` | `65536` | Request body limit |
| `TRIAGE_MAX_COST_USD` | `0.50` | Per-run cost cap |
| `TRIAGE_MAX_LLM_CALLS` | `40` | Per-run call cap |
| `TRIAGE_MAX_TOTAL_TOKENS` | `60000` | Per-run token cap |
| `TRIAGE_RUN_TIMEOUT_S` | `120` | Per-run wall-clock deadline |
| `TRIAGE_LLM` | `mock` | `env` = real model (see `providers.py` for `TRIAGE_PROVIDER`, `TRIAGE_MODEL_*`, `TRIAGE_PRICE_*`, API key) |
| `TRIAGE_DEFAULT_CONFIG` | `optimized` | Default optimization profile |
| `TRIAGE_LOG_LEVEL` | `INFO` | Log level |

Invalid values stop the process at startup with a message naming the variable.

## Run statuses and what to do

| Status | Meaning | Action |
|---|---|---|
| `completed` | Fix approved by the critic | none |
| `needs_review` | Finished but the critic never approved | A human should read the proposal; inspect the trace |
| `failed` | Node/LLM error or step limit | Read `error`; `POST /v1/runs/{id}/resume` once the cause (e.g. provider outage) is fixed |
| `budget_exceeded` | Call/token/cost cap | Decide whether the cap is too low or the run is looping (check the trace); resume re-starts with a fresh budget |
| `timeout` | Deadline | Check model latency; resume gets a fresh deadline |

```bash
curl -s -H "Authorization: Bearer $TRIAGE_SERVER_TOKEN" localhost:8080/v1/runs?status=failed
curl -s -X POST -H "Authorization: Bearer $TRIAGE_SERVER_TOKEN" localhost:8080/v1/runs/<id>/resume
curl -s -H "Authorization: Bearer $TRIAGE_SERVER_TOKEN" localhost:8080/v1/runs/<id>/trace | python -m json.tool
```

## Metrics and suggested alerts (Prometheus)

Counters reset on restart; durable history is in the `runs` table.

| Signal | Query | Suggested alert |
|---|---|---|
| Failure ratio | `sum(rate(triage_runs_total{status!="completed"}[15m])) / sum(rate(triage_runs_total[15m]))` | > 0.2 for 15m |
| p95 run latency | `histogram_quantile(0.95, sum by (le) (rate(triage_run_latency_seconds_bucket[15m])))` | above your SLO |
| Spend | `sum(increase(triage_cost_usd_total[1h]))` | above your hourly budget |
| Saturation | `sum(rate(triage_http_requests_total{code="429"}[5m]))` | > 0 sustained: raise `TRIAGE_MAX_CONCURRENT` or add replicas |
| Injection attempts | `increase(triage_injection_lines_removed_total[1h])` | > 0: review the source of the logs; also see `injection_suspected` log events |
| Tool errors | `sum(rate(triage_tool_calls_total{outcome="error"}[15m]))` | sustained rise suggests the model is producing malformed tool calls |
| 5xx | `sum(rate(triage_http_requests_total{code=~"5.."}[5m]))` | > 0 |

Note: with the mock model, latency and cost metrics are simulated.

## Capacity

A run holds one worker thread and one connection for its duration. Throughput is roughly
`TRIAGE_MAX_CONCURRENT / average run seconds`. For bursts, put a queue in front and call `run_incident`
from workers; for multiple replicas, replace the SQLite `Store` with a shared database.

## Backup and retention
Back up the SQLite file with `sqlite3 triage.db ".backup backup.db"` (safe while running, WAL mode).
There is no automatic expiry; delete old rows from `runs`, `checkpoints` and `traces` per your policy.

## Common problems

- **Server exits with "Refusing to listen"**: set `TRIAGE_SERVER_TOKEN` or bind to loopback.
- **Many `budget_exceeded`**: look at `per_agent` in a trace summary; a looping supervisor or oversized prompts (compact-state off) are the usual causes.
- **`failed` with an HTTP error from the provider**: the client retries 408/429/5xx with backoff before giving up; resume after the outage.
- **`database is locked`**: another process holds a long write; the store waits up to 10 s. Run a single server per database file.
