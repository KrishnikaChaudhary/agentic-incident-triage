import json
import operator
import re
from pathlib import Path
from typing import Annotated, TypedDict

import pytest

from triage.config import CONFIGS
from triage.graph import END, StateGraph
from triage.observability import PRICING, Tracer, cost_usd
from triage.pipeline import run_incident
from triage.tools import SAMPLE

INCIDENTS = json.loads((SAMPLE / "incidents.json").read_text())


# ---- graph runtime ----
class S(TypedDict, total=False):
    n: int
    log: Annotated[list, operator.add]


def test_graph_routing_reducers_and_loop_guard():
    g = StateGraph(S)
    g.add_node("a", lambda s: {"n": s.get("n", 0) + 1, "log": ["a"]})
    g.add_node("b", lambda s: {"log": ["b"]})
    g.set_entry_point("a")
    g.add_conditional_edges("a", lambda s: "again" if s["n"] < 3 else "done", {"again": "b", "done": END})
    g.add_edge("b", "a")
    cps: list = []
    out = g.compile(checkpointer=cps).invoke({"log": []})
    assert out["log"] == ["a", "b", "a", "b", "a"] and out["n"] == 3
    assert len(cps) == 5 and cps[0]["node"] == "a"

    loop = StateGraph(S)
    loop.add_node("x", lambda s: {})
    loop.set_entry_point("x")
    loop.add_edge("x", "x")
    with pytest.raises(RecursionError):
        loop.compile(recursion_limit=5).invoke({})


# ---- observability ----
def test_cost_math_and_span_nesting():
    assert cost_usd("large", 1_000_000, 1_000_000) == pytest.approx(sum(PRICING["large"]))
    t = Tracer("x")
    with t.span("outer", "agent") as o:
        with t.span("inner", "llm", agent="a") as i:
            i.tokens_in, i.tokens_out, i.latency_s, i.cost_usd = 100, 50, 0.5, 0.01
    assert i.parent_id == o.span_id
    s = t.summary()
    assert s["tokens_per_sec"] == pytest.approx(100.0) and s["total_tokens"] == 150


# ---- pipeline ----
@pytest.mark.parametrize("inc", INCIDENTS, ids=lambda i: i["id"])
@pytest.mark.parametrize("cfg", list(CONFIGS))
def test_every_incident_resolved_in_every_config(inc, cfg):
    r = run_incident(inc, cfg)["final"]
    assert r["review"] == "approve"
    assert inc["expected_file"] in r["code_findings"]
    assert "Test:" in r["proposal"]


def test_reflection_loop_fires_only_when_fix_lacks_test():
    odd = run_incident(next(i for i in INCIDENTS if i["id"] == "INC-101"))["final"]
    even = run_incident(next(i for i in INCIDENTS if i["id"] == "INC-102"))["final"]
    assert odd["revisions"] == 1 and even["revisions"] == 0


def test_optimized_is_cheaper_and_caches_tools():
    inc = INCIDENTS[0]
    base = run_incident(inc, "baseline")["summary"]
    opt = run_incident(inc, "optimized")["summary"]
    assert opt["cost_usd"] < base["cost_usd"] * 0.5
    assert opt["tokens_in"] < base["tokens_in"]
    assert opt["tool_cache_hits"] > 0 and base["tool_cache_hits"] == 0


def test_model_routing_uses_strong_model_only_for_fix():
    spans = run_incident(INCIDENTS[0], "optimized")["tracer"].spans
    big = {s.attrs["agent"] for s in spans if s.kind == "llm" and s.attrs["model"] == "large"}
    assert big == {"fix_proposer"}


def test_every_llm_call_belongs_to_one_trace_tree():
    t = run_incident(INCIDENTS[2])["tracer"]
    ids = {s.span_id for s in t.spans}
    assert all(s.parent_id in ids for s in t.spans if s.parent_id)
    assert sum(1 for s in t.spans if s.parent_id is None) == 1


def test_sample_logs_match_source_lines():
    """Stack frames in the generated logs must point at real lines of the sample app."""
    log = (SAMPLE / "logs" / "app.log").read_text()
    for cls, ln in re.findall(r"\((\w+\.java):(\d+)\)", log):
        matches = list((SAMPLE / "app").rglob(cls))
        if matches:  # controllers/jobs in deeper frames are intentionally absent
            assert len(matches[0].read_text().splitlines()) >= int(ln)
