import threading

import pytest

from triage.metrics import COST_BUCKETS, Metrics
from triage.pipeline import run_incident, validate_incident
from triage.server import main
from triage.settings import Settings
from triage.store import Store


# ---- store ----
def test_store_round_trip_and_filters(tmp_path, incidents):
    s = Store(tmp_path / "s.db")
    assert s.ping()
    ok = run_incident(incidents["INC-101"], "optimized", store=s)
    bad = run_incident(incidents["INC-102"], "optimized", settings=Settings(max_llm_calls=3), store=s)
    assert {r["id"] for r in s.list_runs()} == {ok["run_id"], bad["run_id"]}
    assert [r["id"] for r in s.list_runs(status="budget_exceeded")] == [bad["run_id"]]
    rec = s.get_run(ok["run_id"])
    assert rec["incident"]["id"] == "INC-101" and rec["state"]["review"] == "approve" and rec["tokens_in"] > 0
    assert s.get_trace(ok["run_id"])["spans"] and s.get_run("nope") is None


def test_store_is_safe_against_sql_injection_in_filters(tmp_path):
    s = Store(tmp_path / "s.db")
    assert s.list_runs(status="x'; DROP TABLE runs; --") == []
    assert s.list_runs() == [] and s.ping()  # table still exists


# ---- metrics ----
def test_prometheus_text_format_and_histogram_semantics():
    m = Metrics()
    m.inc("triage_runs_total", status="completed", config="optimized")
    m.inc("triage_runs_total", status="completed", config="optimized")
    m.add_gauge("triage_runs_in_flight", 1)
    for v in (0.002, 0.02, 0.02, 5.0):
        m.observe("triage_run_cost_usd", v, COST_BUCKETS)
    text = m.render()
    assert 'triage_runs_total{config="optimized",status="completed"} 2.0' in text
    assert "# TYPE triage_run_cost_usd histogram" in text and "# TYPE triage_runs_in_flight gauge" in text
    assert 'triage_run_cost_usd_bucket{le="0.005"} 1' in text  # cumulative
    assert 'triage_run_cost_usd_bucket{le="0.05"} 3' in text
    assert 'triage_run_cost_usd_bucket{le="+Inf"} 4' in text and "triage_run_cost_usd_count 4" in text


def test_label_values_are_escaped():
    m = Metrics()
    m.inc("x_total", path='a"b\\c\nd')
    assert 'x_total{path="a\\"b\\\\c\\nd"} 1.0' in m.render()


def test_metrics_are_thread_safe():
    m = Metrics()

    def work():
        for _ in range(500):
            m.inc("c_total")
            m.observe("h", 0.1, (1.0,))

    threads = [threading.Thread(target=work) for _ in range(8)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert "c_total 4000.0" in m.render() and "h_count 4000" in m.render()


# ---- input validation ----
@pytest.mark.parametrize("bad", [None, [], "x", {}, {"description": ""}, {"description": " "}, {"description": 5},
                                 {"description": "x" * 4001}, {"description": "ok", "id": "../x"}, {"description": "ok", "id": "a" * 41}])
def test_validate_incident_rejects_bad_input(bad):
    with pytest.raises(ValueError):
        validate_incident(bad)


def test_validate_incident_accepts_and_generates_ids():
    out = validate_incident({"description": " hello ", "id": "INC-7"})
    assert out == {"id": "INC-7", "description": "hello"}
    assert validate_incident({"description": "x"})["id"].startswith("REQ-")


# ---- settings ----
def test_settings_from_env_and_validation():
    s = Settings.from_env({"TRIAGE_PORT": "9000", "TRIAGE_MAX_COST_USD": "1.5", "TRIAGE_LLM": "env", "TRIAGE_ALLOW_INSECURE": "true"})
    assert (s.port, s.max_cost_usd, s.llm, s.allow_insecure) == (9000, 1.5, "env", True)
    for env in ({"TRIAGE_PORT": "abc"}, {"TRIAGE_PORT": "70000"}, {"TRIAGE_LLM": "gpt"}, {"TRIAGE_MAX_COST_USD": "0"},
                {"TRIAGE_DEFAULT_CONFIG": "nope"}, {"TRIAGE_MAX_CONCURRENT": "0"}):
        with pytest.raises(ValueError):
            Settings.from_env(env)


def test_server_refuses_public_bind_without_a_token(monkeypatch, tmp_path):
    for k in ("TRIAGE_SERVER_TOKEN", "TRIAGE_ALLOW_INSECURE"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("TRIAGE_DB", str(tmp_path / "x.db"))
    with pytest.raises(SystemExit, match="Refusing to listen"):
        main(["--host", "0.0.0.0", "--port", "18999"])
