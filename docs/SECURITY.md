# Security model

## What is untrusted

| Input | Why it is untrusted |
|---|---|
| Incident text (`POST /v1/triage`) | Supplied by callers |
| Log lines and source code returned by tools | Written by third parties / attackers (log injection, hostile repos) |
| Model output | Can be wrong, invented, or steered by the above |

## Controls

| Threat | Control | Where | Tested |
|---|---|---|---|
| Prompt injection via logs/code | Instruction-like lines stripped from tool output; output wrapped in `<tool_output>` and prompts say never to obey it; tools are read-only so a successful injection still cannot act | `guardrails.sanitize`, `tools.ToolBox`, `agents.UNTRUSTED` | `test_guardrails` (poisoned log source end to end) |
| Model invents tool results | Everything after an `Action:` line is discarded; the real observation is used | `agents.react` | `test_providers` |
| Model drives control flow | Routing validated and repaired; step limit; revision cap; call/token/cost budget; wall-clock deadline | `agents`, `graph`, `guardrails.Budget` | `test_failures` |
| Path traversal / sandbox escape | Tool allow-list, argument regexes, `..` and absolute paths rejected, files indexed only if they resolve inside the root (symlinks included) | `tools` | `test_guardrails` |
| Unauthenticated access | Bearer token, constant-time comparison; server refuses a non-loopback bind without a token | `server` | `test_server`, `test_store_metrics_settings` |
| Oversized / malformed requests | Content-Type, Content-Length, body-size, JSON-shape and field validation; socket timeout | `server`, `pipeline.validate_incident` | `test_server` |
| Resource exhaustion | Concurrency cap with `429 Retry-After`; per-run budget and deadline | `server`, `pipeline` | `test_server` |
| SQL injection | Parameterized queries only; run ids matched against `^[0-9a-f]{32}$` before use | `store`, `server` | `test_server`, `test_store_metrics_settings` |
| Secret leakage | API keys read only from the environment; error text passes through `redact_secrets`; logs carry ids and numbers, never prompts or completions; stack traces never reach clients | `guardrails`, `server`, `logging_setup` | `test_failures`, `test_server` |
| Container hardening (container files only) | Non-root user, read-only root filesystem, all capabilities dropped, no-new-privileges, loopback-published port | `Dockerfile`, `docker-compose.yml` | not built here |

## Residual risks (read these)

- **Sanitization is a heuristic.** A determined attacker can phrase an injection that matches none of the
  patterns. The structural defences (read-only tools, allow-listed arguments, sandbox root, budgets) are
  what limit the damage; do not add write-capable tools without an approval gate.
- **No TLS in the process.** Run it behind a TLS-terminating reverse proxy for anything beyond loopback.
- **One shared bearer token** (no per-user identity, rotation, or rate limiting per caller).
- **Data at rest.** SQLite stores incident text, code snippets and model output. Apply your retention policy
  and protect the volume; there is no built-in expiry.
- **Model provider.** With `TRIAGE_LLM=env`, prompts containing log and code excerpts are sent to the
  provider you configure. Confirm that is acceptable for your data.
- **Not independently audited or load-tested.**

## Reporting a vulnerability
Open a private security advisory on the repository (or email the maintainer) rather than a public issue.
