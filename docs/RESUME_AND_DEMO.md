# Resume bullets, demo script, interview notes

## What you can honestly claim today (mock-LLM benchmark, 10 synthetic incidents)
The service layer below is implemented and tested locally; the container files, CI workflow, real-provider path and LangGraph port have not been run (see README, Verification status).
These are measured mechanics, but say "approximated token counts, synthetic incident set" if asked.

- Built a hierarchical multi-agent incident-triage system (supervisor + ReAct specialists + self-reflecting critic) on a LangGraph-compatible state-graph with typed state, reducers and per-step checkpoints.
- Instrumented every request with per-span tracing (tokens in/out, tokens/sec, TTFT, latency, cost-per-request) and ran a 5-config ablation of state-management optimizations; structured state handoff and tool scoping cut input tokens per request by ~65% (4,530 -> 1,602) at equal task success.
- Shipped it as a service: HTTP API with bearer auth, validation and concurrency limits; per-run budgets/deadlines; SQLite checkpoints with resume of failed runs; Prometheus metrics and structured JSON logs; graceful shutdown that drains in-flight runs.
- Added deterministic guardrails around the LLM: tool-observation hallucination stripping, supervisor routing validation/repair, recursion and revision caps; 126 automated tests covering failure modes, resume-from-checkpoint, prompt-injection through poisoned logs, and the full pipeline over real HTTP/SSE clients (Anthropic and OpenAI-compatible).

## Claim only AFTER you run it with a real model
Run `python -m triage.benchmark --llm env` (see README) and then replace the bracketed numbers:

- "Model routing (small model for routing/extraction/review, large model for fix generation) reduced cost-per-request by [X]% and p50 latency by [Y]% on [N] incidents, with [Z]% task success vs [W]% baseline."

Do not use the -89% / simulated latency figures: they depend on placeholder prices and simulated speeds.

## Technical discovery bullet (only once you've done it)
- "Led discovery with [role] on [team]: interviewed [N] stakeholders, wrote requirements and success metrics ([docs/discovery.md]), which changed [specific design decision]."
Fill this from the real conversation; leave it out if you haven't done one.

## 3-minute demo script
1. `python -m triage.cli INC-101 --config baseline --trace baseline.json` then `--config optimized --trace optimized.json`. Point at the Metrics line: same answer, fewer tokens, lower cost.
2. Open `README.md` architecture diagram; explain supervisor -> specialists -> back to supervisor.
3. Show the reflection loop: INC-101 needs 1 revision (critic asks for a regression test), INC-102 needs 0.
4. Open `optimized.json`: show one trace tree (request -> agents -> llm/tool spans) and a cached tool span (`cache_hit: true`).
5. `python -m triage.benchmark`: walk the ablation table; say which optimization buys what.

## Questions to be ready for
- *Why not just one big agent?* Specialists have small prompts and tools, are cheaper to route to cheaper models, and are testable in isolation.
- *How do you stop loops and hallucinated tool output?* Recursion limit, max 2 revisions, max 4 ReAct steps; observations after an Action are discarded; the supervisor cannot FINISH early.
- *What is mocked?* The model (and therefore latency/price realism). Tools, graph, state, parsing, tracing, HTTP clients are real and tested.
- *What would you do next?* Real-model benchmark, incidents without stack traces, OpenTelemetry/Langfuse export, approval-gated draft PR tool.
