"""Tiny thread-safe metrics registry that renders the Prometheus text exposition format
(counters, gauges, histograms). No dependencies; scrape GET /metrics."""
from __future__ import annotations

import threading

LATENCY_BUCKETS = (0.5, 1, 2.5, 5, 10, 30, 60, 120)
COST_BUCKETS = (0.001, 0.005, 0.01, 0.05, 0.1, 0.25, 0.5, 1)

HELP = {
    "triage_runs_total": "Triage runs finished, by terminal status.",
    "triage_llm_calls_total": "LLM calls, by agent and model tier.",
    "triage_tokens_total": "Tokens used, by direction and model tier.",
    "triage_cost_usd_total": "LLM spend in USD, by model tier.",
    "triage_tool_calls_total": "Tool calls, by tool and outcome (ok, cache_hit, error).",
    "triage_injection_lines_removed_total": "Instruction-like lines stripped from untrusted tool output.",
    "triage_run_latency_seconds": "Per-run model+tool latency in seconds (simulated when the mock LLM is used).",
    "triage_run_cost_usd": "Per-run LLM cost in USD.",
    "triage_runs_in_flight": "Runs currently executing.",
    "triage_http_requests_total": "HTTP requests, by method, route and status code.",
}


def _key(labels: dict) -> tuple:
    return tuple(sorted((k, str(v)) for k, v in labels.items()))


def _fmt_labels(key: tuple, extra: tuple = ()) -> str:
    items = [*key, *extra]
    if not items:
        return ""
    esc = lambda v: v.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
    return "{" + ",".join(f'{k}="{esc(v)}"' for k, v in items) + "}"


class Metrics:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._counters: dict[str, dict[tuple, float]] = {}
        self._gauges: dict[str, dict[tuple, float]] = {}
        self._hists: dict[str, dict[tuple, dict]] = {}
        self._buckets: dict[str, tuple] = {}

    def inc(self, name: str, value: float = 1.0, **labels) -> None:
        with self._lock:
            series = self._counters.setdefault(name, {})
            k = _key(labels)
            series[k] = series.get(k, 0.0) + value

    def add_gauge(self, name: str, delta: float, **labels) -> None:
        with self._lock:
            series = self._gauges.setdefault(name, {})
            k = _key(labels)
            series[k] = series.get(k, 0.0) + delta

    def observe(self, name: str, value: float, buckets: tuple, **labels) -> None:
        with self._lock:
            self._buckets[name] = buckets
            h = self._hists.setdefault(name, {}).setdefault(
                _key(labels), {"counts": [0] * (len(buckets) + 1), "sum": 0.0, "count": 0})
            for i, bound in enumerate(buckets):
                if value <= bound:
                    h["counts"][i] += 1
                    break
            else:
                h["counts"][-1] += 1
            h["sum"] += value
            h["count"] += 1

    def record_run(self, result: dict) -> None:
        """Fold a finished run (as returned by pipeline.run_incident) into the registry."""
        self.inc("triage_runs_total", status=result["status"], config=result["config"])
        for sp in result["tracer"].spans:
            if sp.kind == "llm":
                model = sp.attrs.get("model", "?")
                self.inc("triage_llm_calls_total", agent=sp.attrs.get("agent", "?"), model=model)
                self.inc("triage_tokens_total", sp.tokens_in, direction="input", model=model)
                self.inc("triage_tokens_total", sp.tokens_out, direction="output", model=model)
                self.inc("triage_cost_usd_total", sp.cost_usd, model=model)
            elif sp.kind == "tool":
                outcome = "error" if sp.status == "error" else ("cache_hit" if sp.attrs.get("cache_hit") else "ok")
                self.inc("triage_tool_calls_total", tool=sp.attrs.get("tool", "?"), outcome=outcome)
                if sp.attrs.get("injection_lines_removed"):
                    self.inc("triage_injection_lines_removed_total", sp.attrs["injection_lines_removed"])
        self.observe("triage_run_latency_seconds", result["summary"]["latency_s"], LATENCY_BUCKETS)
        self.observe("triage_run_cost_usd", result["summary"]["cost_usd"], COST_BUCKETS)

    def render(self) -> str:
        lines: list[str] = []
        with self._lock:
            for name in sorted(self._counters):
                lines += [f"# HELP {name} {HELP.get(name, name)}", f"# TYPE {name} counter"]
                lines += [f"{name}{_fmt_labels(k)} {float(v)!r}" for k, v in sorted(self._counters[name].items())]
            for name in sorted(self._gauges):
                lines += [f"# HELP {name} {HELP.get(name, name)}", f"# TYPE {name} gauge"]
                lines += [f"{name}{_fmt_labels(k)} {float(v)!r}" for k, v in sorted(self._gauges[name].items())]
            for name in sorted(self._hists):
                lines += [f"# HELP {name} {HELP.get(name, name)}", f"# TYPE {name} histogram"]
                bounds = self._buckets[name]
                for k, h in sorted(self._hists[name].items()):
                    cum = 0
                    for bound, c in zip(bounds, h["counts"]):
                        cum += c
                        lines.append(f"{name}_bucket{_fmt_labels(k, (('le', repr(float(bound))),))} {cum}")
                    lines.append(f"{name}_bucket{_fmt_labels(k, (('le', '+Inf'),))} {h['count']}")
                    lines.append(f"{name}_sum{_fmt_labels(k)} {h['sum']!r}")
                    lines.append(f"{name}_count{_fmt_labels(k)} {h['count']}")
        return "\n".join(lines) + "\n"
