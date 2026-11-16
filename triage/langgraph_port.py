"""The same agents on real LangGraph.

UNTESTED IN THE BUILD SANDBOX: langgraph could not be installed there (network off).
Run it on your machine after `pip install langgraph`; if the API has moved, only the
~15 lines below need adjusting because every node is a plain `state -> update` function.

Usage: python -m triage.langgraph_port INC-101
"""
from __future__ import annotations

import json
import sys

from .agents import Ctx, make_nodes, route
from .config import CONFIGS
from .llm import MockLLM
from .observability import Tracer
from .pipeline import WORKERS, format_report
from .state import IncidentState
from .tools import SAMPLE, ToolBox


def build_langgraph(ctx: Ctx):
    from langgraph.graph import END, StateGraph  # pip install langgraph

    g = StateGraph(IncidentState)  # Annotated[list, operator.add] on `transcript` is picked up natively
    for name, fn in make_nodes(ctx).items():
        g.add_node(name, fn)
    g.set_entry_point("supervisor")
    g.add_conditional_edges("supervisor", route, {**{w: w for w in WORKERS}, "FINISH": END})
    for w in WORKERS:
        g.add_edge(w, "supervisor")
    return g.compile()  # add a checkpointer here (e.g. MemorySaver) for persistence / time travel


def main() -> None:
    inc_id = sys.argv[1] if len(sys.argv) > 1 else "INC-101"
    inc = next(i for i in json.loads((SAMPLE / "incidents.json").read_text()) if i["id"] == inc_id)
    cfg = CONFIGS["optimized"]
    tracer = Tracer(inc_id)
    ctx = Ctx(MockLLM(), tracer, ToolBox(tracer, cfg, inc_id), cfg)  # swap MockLLM for a real client
    final = build_langgraph(ctx).invoke({"incident": inc, "transcript": [], "hops": 0, "revisions": 0, "review": "none"})
    print(format_report({"final": final, "summary": tracer.summary()}))


if __name__ == "__main__":
    main()
