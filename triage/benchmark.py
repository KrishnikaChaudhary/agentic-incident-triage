"""Benchmark every config over every sample incident and write results/benchmark.{md,json}.

Usage: python -m triage.benchmark [--llm env] [--limit N] [--configs baseline optimized]
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from .config import CONFIGS
from .pipeline import run_incident
from .tools import SAMPLE

RESULTS = Path(__file__).resolve().parents[1] / "results"


def aggregate(runs: list[dict], incidents: list[dict]) -> dict:
    n = len(runs)
    avg = lambda key: sum(r["summary"][key] for r in runs) / n
    solved = sum(
        1 for r, inc in zip(runs, incidents)
        if r["final"].get("review") == "approve" and inc["expected_file"] in r["final"].get("code_findings", "")
    )
    return {
        "tokens_in": avg("tokens_in"), "tokens_out": avg("tokens_out"), "cost_usd": avg("cost_usd"),
        "tokens_per_sec": avg("tokens_per_sec"), "avg_ttft_s": avg("avg_ttft_s"), "latency_s": avg("latency_s"),
        "llm_calls": avg("llm_calls"), "cache_hits": avg("tool_cache_hits"), "resolved": solved / n,
        "avg_revisions": sum(r["final"].get("revisions", 0) for r in runs) / n,
    }


def main() -> None:
    ap = argparse.ArgumentParser(description="Benchmark all configs over the sample incidents")
    ap.add_argument("--llm", default="mock", choices=["mock", "env"],
                    help="env = real model via TRIAGE_* env vars; writes results/benchmark_real.* so the mock results are kept")
    ap.add_argument("--limit", type=int, help="only the first N incidents (cheap smoke run against a real model)")
    ap.add_argument("--configs", nargs="+", choices=list(CONFIGS), default=list(CONFIGS))
    args = ap.parse_args()

    llm = None
    if args.llm == "env":
        from .providers import HttpLLM
        llm = HttpLLM.from_env()
    suffix = "" if args.llm == "mock" else "_real"
    incidents = json.loads((SAMPLE / "incidents.json").read_text())[: args.limit]
    configs = {k: CONFIGS[k] for k in dict.fromkeys(["baseline", *args.configs])}  # baseline is always the reference
    table = {}
    for name, cfg in configs.items():
        runs = [run_incident(inc, cfg, llm=llm) for inc in incidents]
        table[name] = aggregate(runs, incidents)
        if name in ("baseline", "optimized"):
            for r in runs:
                r["tracer"].export(RESULTS / f"traces{suffix}" / name / f"{r['incident']['id']}.json")

    base = table["baseline"]
    note = ("MockLLM: latency/TTFT simulated, prices are placeholders" if llm is None
            else "real model: latency/TTFT measured, tokens from provider usage, prices from TRIAGE_PRICE_*")
    lines = [
        f"Averages over {len(incidents)} incidents ({note}).\n",
        "| config | tokens in | tokens out | cost/request | vs baseline | tok/s | TTFT (s) | latency (s) | LLM calls | cache hits | resolved |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for name, m in table.items():
        delta = (m["cost_usd"] / base["cost_usd"] - 1) * 100
        lines.append(
            f"| {name} | {m['tokens_in']:.0f} | {m['tokens_out']:.0f} | ${m['cost_usd']:.5f} | {delta:+.0f}% | "
            f"{m['tokens_per_sec']:.0f} | {m['avg_ttft_s']:.2f} | {m['latency_s']:.2f} | {m['llm_calls']:.1f} | "
            f"{m['cache_hits']:.1f} | {m['resolved'] * 100:.0f}% |"
        )
    md = "\n".join(lines)
    RESULTS.mkdir(exist_ok=True)
    (RESULTS / f"benchmark{suffix}.md").write_text(md + "\n")
    (RESULTS / f"benchmark{suffix}.json").write_text(json.dumps(table, indent=2))
    print(md)


if __name__ == "__main__":
    main()
