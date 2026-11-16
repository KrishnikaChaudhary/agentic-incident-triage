"""Wire agents into a graph and run one incident end to end, with production behaviours:
validated input, per-run budget and deadline, failure isolation with partial results,
persistence + resume, metrics and structured logs.

Run statuses:
    completed        critic approved the fix
    needs_review     finished, but the critic never approved (human should look)
    failed           a node or the LLM client raised, or the graph looped
    budget_exceeded  per-run LLM-call / token / cost cap hit
    timeout          per-run wall-clock deadline hit
"""
from __future__ import annotations

import logging
import re
import time
import uuid

from .agents import Ctx, make_nodes, route
from .config import CONFIGS, Config
from .errors import BudgetExceeded, RunTimeout
from .graph import END, GraphRecursionError, GraphRunError, StateGraph
from .guardrails import Budget, redact_secrets
from .llm import MockLLM
from .logging_setup import event
from .observability import Tracer
from .settings import Settings
from .state import IncidentState
from .tools import CodeSource, LogSource, ToolBox

log = logging.getLogger("triage.pipeline")

WORKERS = ["log_analyst", "code_investigator", "fix_proposer", "critic"]
RESUMABLE = {"failed", "timeout", "budget_exceeded"}
_ID_RE = re.compile(r"[A-Za-z0-9_-]{1,40}")


def validate_incident(obj) -> dict:
    """Validate untrusted incident input; returns a clean {id, description} dict or raises ValueError."""
    if not isinstance(obj, dict):
        raise ValueError("incident must be a JSON object")
    desc = obj.get("description")
    if not isinstance(desc, str) or not (1 <= len(desc.strip()) <= 4000):
        raise ValueError("incident.description must be a string of 1-4000 characters")
    iid = obj.get("id") or f"REQ-{uuid.uuid4().hex[:8]}"
    if not isinstance(iid, str) or not _ID_RE.fullmatch(iid):
        raise ValueError("incident.id must match [A-Za-z0-9_-]{1,40}")
    return {"id": iid, "description": desc.strip()}


def build_graph(ctx: Ctx) -> StateGraph:
    nodes = make_nodes(ctx)
    g = StateGraph(IncidentState)
    for name, fn in nodes.items():
        g.add_node(name, fn)
    g.set_entry_point("supervisor")
    g.add_conditional_edges("supervisor", route, {**{w: w for w in WORKERS}, "FINISH": END})
    for w in WORKERS:
        g.add_edge(w, "supervisor")  # every worker reports back to the supervisor
    return g


class _Fanout:
    """Checkpointer that writes to several sinks (in-memory list for callers, SQLite for resume)."""

    def __init__(self, sinks):
        self.sinks = sinks

    def append(self, entry: dict) -> None:
        for s in self.sinks:
            s.append(entry)


def _status_for(cause: BaseException) -> str:
    if isinstance(cause, BudgetExceeded):
        return "budget_exceeded"
    if isinstance(cause, RunTimeout):
        return "timeout"
    return "failed"


def initial_state(incident: dict) -> dict:
    return {"incident": incident, "transcript": [], "hops": 0, "revisions": 0, "review": "none"}


def run_incident(incident: dict, cfg: Config | str = "optimized", llm=None, *, settings: Settings | None = None,
                 store=None, metrics=None, run_id: str | None = None, resume_state: dict | None = None,
                 attempt: int = 1, logs: LogSource | None = None, code: CodeSource | None = None) -> dict:
    """Run one incident. Never raises for model/tool/budget/timeout problems: inspect result['status'].
    With `settings`, the per-run budget and deadline are enforced; without, the run is unlimited.
    `logs` / `code` plug in your own LogSource / CodeSource (default: the bundled sample data)."""
    cfg = CONFIGS[cfg] if isinstance(cfg, str) else cfg
    run_id = run_id or uuid.uuid4().hex
    tracer = Tracer(incident["id"])
    budget = Budget(settings.max_cost_usd, settings.max_llm_calls, settings.max_total_tokens) if settings else None
    ctx = Ctx(llm or MockLLM(), tracer, ToolBox(tracer, cfg, incident["id"], logs=logs, code=code), cfg, budget)
    checkpoints: list = []
    sinks: list = [checkpoints]
    if store is not None:
        from .store import StoreCheckpointer
        if attempt == 1 and resume_state is None:
            store.create_run(run_id, incident, cfg.name)
        sinks.append(StoreCheckpointer(store, run_id, attempt))
    graph = build_graph(ctx).compile(checkpointer=_Fanout(sinks), recursion_limit=25)
    deadline = time.monotonic() + settings.run_timeout_s if settings else None

    event(log, logging.INFO, "run_started", run_id=run_id, incident=incident["id"], config=cfg.name, attempt=attempt,
          resumed=resume_state is not None)
    if metrics is not None:
        metrics.add_gauge("triage_runs_in_flight", 1)
    error: str | None = None
    try:
        with tracer.span("incident", "request", incident=incident["id"], config=cfg.name, run_id=run_id, attempt=attempt):
            final = graph.invoke(resume_state or initial_state(incident),
                                 start_at="supervisor" if resume_state else None, deadline=deadline)
        status = "completed" if final.get("review") == "approve" else "needs_review"
    except GraphRunError as exc:
        final, status, error = exc.state, _status_for(exc.cause), redact_secrets(str(exc))
    except GraphRecursionError as exc:
        final, status, error = exc.state, "failed", str(exc)
    finally:
        if metrics is not None:
            metrics.add_gauge("triage_runs_in_flight", -1)

    summary = tracer.summary()
    if store is not None:
        store.finish_run(run_id, attempt, status, final, summary, tracer.payload(), error)
    result = {"run_id": run_id, "status": status, "error": error, "config": cfg.name, "incident": incident,
              "final": final, "summary": summary, "tracer": tracer, "checkpoints": checkpoints}
    if metrics is not None:
        metrics.record_run(result)
    event(log, logging.INFO if status in ("completed", "needs_review") else logging.WARNING, "run_finished",
          run_id=run_id, incident=incident["id"], status=status, attempt=attempt, error=error,
          llm_calls=summary["llm_calls"], tokens=summary["total_tokens"], cost_usd=round(summary["cost_usd"], 6),
          latency_s=round(summary["latency_s"], 3), injection_lines_removed=summary["injection_lines_removed"])
    return result


def resume_incident(run_id: str, store, llm=None, *, settings: Settings | None = None, metrics=None) -> dict:
    """Continue a failed / timed-out / over-budget run from its last checkpoint (fresh budget and deadline)."""
    rec = store.get_run(run_id)
    if rec is None:
        raise KeyError(f"unknown run {run_id}")
    if rec["status"] not in RESUMABLE:
        raise ValueError(f"run {run_id} has status '{rec['status']}' and cannot be resumed")
    cp = store.last_checkpoint(run_id)
    state = cp["state"] if cp else initial_state(rec["incident"])
    attempt = store.begin_attempt(run_id)
    return run_incident(rec["incident"], rec["config"], llm, settings=settings, store=store, metrics=metrics,
                        run_id=run_id, resume_state=state, attempt=attempt)


def report_dict(result: dict) -> dict:
    f = result["final"]
    return {"category": f.get("category"), "signal": f.get("log_findings"), "location": f.get("code_findings"),
            "proposal": f.get("proposal"), "review": f.get("review"), "revisions": f.get("revisions", 0)}


def format_report(result: dict) -> str:
    f, s = result["final"], result["summary"]
    status = f"Status     : {result['status']}" + (f" ({result['error']})" if result.get("error") else "") + "\n" if "status" in result else ""
    return (
        status +
        f"Incident   : {f['incident']['id']}  [{f.get('category', '?')}]\n"
        f"Signal     : {f.get('log_findings', 'n/a')}\n"
        f"Location   : {f.get('code_findings', 'n/a')}\n"
        f"Review     : {f.get('review', 'n/a')} after {f.get('revisions', 0)} revision(s)\n"
        f"Proposal   :\n  " + f.get("proposal", "n/a").replace("\n", "\n  ") + "\n\n"
        f"Metrics    : {s['llm_calls']} LLM calls, {s['tool_calls']} tool calls ({s['tool_cache_hits']} cached), "
        f"{s['tokens_in']} in / {s['tokens_out']} out tokens, cost ${s['cost_usd']:.5f}, "
        f"{s['tokens_per_sec']:.0f} tok/s, avg TTFT {s['avg_ttft_s']:.2f}s, latency {s['latency_s']:.2f}s"
    )
