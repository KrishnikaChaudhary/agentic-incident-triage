"""Failure modes: every one must end in a recorded status with a partial result, never an exception."""
import pytest

from conftest import WrapLLM
from triage.metrics import Metrics
from triage.pipeline import resume_incident, run_incident
from triage.settings import Settings
from triage.store import Store


def test_llm_error_becomes_failed_run_with_partial_state_and_redacted_error(incidents):
    llm = WrapLLM(fail_at=6, fail_exc=RuntimeError("401 invalid key sk-abcdefghijklmnop"))
    r = run_incident(incidents["INC-101"], "optimized", llm)
    assert r["status"] == "failed"
    assert "sk-abcdefghijklmnop" not in r["error"] and "[REDACTED]" in r["error"]
    assert r["final"].get("log_findings")  # work done before the failure is preserved
    assert r["summary"]["span_errors"] >= 1  # the failing LLM span is marked as an error in the trace
    assert [s for s in r["tracer"].spans if s.status == "error" and s.kind == "llm"]


def test_call_budget_stops_the_run(incidents):
    r = run_incident(incidents["INC-101"], "optimized", settings=Settings(max_llm_calls=5))
    assert r["status"] == "budget_exceeded" and "llm_calls" in r["error"]
    assert r["summary"]["llm_calls"] == 5


def test_cost_budget_stops_the_run(incidents):
    r = run_incident(incidents["INC-101"], "baseline", settings=Settings(max_cost_usd=0.001))
    assert r["status"] == "budget_exceeded" and "cost_usd" in r["error"]
    assert r["summary"]["llm_calls"] < 17  # stopped early instead of running to completion


def test_deadline_stops_the_run(incidents):
    r = run_incident(incidents["INC-101"], "optimized", WrapLLM(delay=0.03), settings=Settings(run_timeout_s=0.05))
    assert r["status"] == "timeout" and "deadline" in r["error"]


def test_looping_supervisor_is_stopped_by_the_step_limit(incidents):
    llm = WrapLLM(transform=lambda role, text: "CATEGORY: npe\nNEXT: log_analyst" if role == "supervisor" else text)
    r = run_incident(incidents["INC-101"], "optimized", llm)
    assert r["status"] == "failed" and "exceeded" in r["error"]


def test_critic_never_approving_ends_as_needs_review_after_two_revisions(incidents):
    llm = WrapLLM(transform=lambda role, text: text.replace("VERDICT=APPROVE", "VERDICT=REVISE") if role == "critic" else text)
    r = run_incident(incidents["INC-102"], "optimized", llm)
    assert r["status"] == "needs_review"
    assert r["final"]["revisions"] == 2 and r["final"]["review"] == "revise"


def test_unknown_tool_from_the_model_is_survivable(incidents):
    llm = WrapLLM(transform=lambda role, text: text.replace("search_logs[", "drop_tables[") if role == "log_analyst" and "Action:" in text else text)
    r = run_incident(incidents["INC-101"], "optimized", llm)
    assert r["status"] in ("completed", "needs_review", "failed")  # no exception escapes
    assert any(s.status == "error" and s.kind == "tool" for s in r["tracer"].spans)


# ---- persistence + resume ----
@pytest.fixture
def store(tmp_path):
    return Store(tmp_path / "runs.db")


def test_failed_run_resumes_from_its_checkpoint_not_from_scratch(incidents, store):
    fresh = run_incident(incidents["INC-101"], "optimized")
    first = run_incident(incidents["INC-101"], "optimized", WrapLLM(fail_at=9), store=store)
    assert first["status"] == "failed"
    rec = store.get_run(first["run_id"])
    assert rec["status"] == "failed" and rec["attempts"] == 1 and store.last_checkpoint(first["run_id"])

    second = resume_incident(first["run_id"], store, WrapLLM())
    assert second["status"] == "completed" and second["final"]["review"] == "approve"
    assert second["summary"]["llm_calls"] < fresh["summary"]["llm_calls"]  # picked up where it stopped
    rec = store.get_run(first["run_id"])
    assert rec["status"] == "completed" and rec["attempts"] == 2
    assert rec["cost_usd"] == pytest.approx(first["summary"]["cost_usd"] + second["summary"]["cost_usd"])
    assert store.get_trace(first["run_id"], 1) and store.get_trace(first["run_id"], 2)


def test_over_budget_run_can_be_resumed_with_a_fresh_budget(incidents, store):
    r = run_incident(incidents["INC-104"], "optimized", settings=Settings(max_llm_calls=6), store=store)
    assert r["status"] == "budget_exceeded"
    done = resume_incident(r["run_id"], store, settings=Settings())
    assert done["status"] == "completed"


def test_resume_rejects_unknown_and_non_resumable_runs(incidents, store):
    with pytest.raises(KeyError):
        resume_incident("0" * 32, store)
    ok = run_incident(incidents["INC-101"], "optimized", store=store)
    with pytest.raises(ValueError, match="cannot be resumed"):
        resume_incident(ok["run_id"], store)


def test_metrics_reflect_run_outcomes(incidents):
    m = Metrics()
    run_incident(incidents["INC-101"], "optimized", metrics=m)
    run_incident(incidents["INC-101"], "optimized", WrapLLM(fail_at=4), metrics=m)
    text = m.render()
    assert 'triage_runs_total{config="optimized",status="completed"} 1.0' in text
    assert 'triage_runs_total{config="optimized",status="failed"} 1.0' in text
    assert "triage_runs_in_flight" in text and "triage_runs_in_flight 0.0" in text
