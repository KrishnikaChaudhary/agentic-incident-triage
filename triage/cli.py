"""Usage: python -m triage.cli INC-101 [--config optimized|baseline|...] [--llm mock|env] [--trace out.json] [--db triage.db]

Exit code: 0 = completed, 1 = anything else (needs_review, failed, budget_exceeded, timeout).
"""
from __future__ import annotations

import argparse
import json

from .config import CONFIGS
from .logging_setup import configure
from .pipeline import format_report, run_incident
from .settings import Settings
from .tools import SAMPLE


def main() -> None:
    ap = argparse.ArgumentParser(description="Run the incident triage agents on a sample incident")
    ap.add_argument("incident", help="incident id from sample_data/incidents.json, e.g. INC-101")
    ap.add_argument("--config", default="optimized", choices=list(CONFIGS))
    ap.add_argument("--trace", help="write the full span trace to this JSON file")
    ap.add_argument("--db", help="persist the run (checkpoints + trace) to this SQLite file")
    ap.add_argument("--llm", default="mock", choices=["mock", "env"],
                    help="mock = offline deterministic model; env = real model configured via TRIAGE_* env vars (see providers.py)")
    args = ap.parse_args()

    settings = Settings.from_env()
    configure(settings.log_level)
    incidents = {i["id"]: i for i in json.loads((SAMPLE / "incidents.json").read_text())}
    if args.incident not in incidents:
        raise SystemExit(f"unknown incident {args.incident}; choose from {', '.join(incidents)}")
    llm = None
    if args.llm == "env":
        from .providers import HttpLLM
        llm = HttpLLM.from_env()
    store = None
    if args.db:
        from .store import Store
        store = Store(args.db)
    result = run_incident(incidents[args.incident], args.config, llm=llm, settings=settings, store=store)
    print(format_report(result))
    print(f"\nrun_id     : {result['run_id']}")
    if args.trace:
        result["tracer"].export(args.trace)
        print(f"trace written to {args.trace}")
    raise SystemExit(0 if result["status"] == "completed" else 1)


if __name__ == "__main__":
    main()
