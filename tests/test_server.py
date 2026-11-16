"""End-to-end tests against a real HTTP server on an ephemeral port."""
import json
import threading
import time

from conftest import WrapLLM

BODY = {"incident": {"id": "INC-101", "description": "Checkout returns HTTP 500 when printing a shipping label. NullPointerException in the order service."}}


def test_health_endpoints_need_no_auth(server):
    client, _ = server()
    assert client("GET", "/healthz", token=None)[:2] == (200, {"status": "ok"})
    code, body, _ = client("GET", "/readyz", token=None)
    assert code == 200 and body["status"] == "ready"


def test_auth_is_required_everywhere_else(server):
    client, _ = server()
    for method, path in [("POST", "/v1/triage"), ("GET", "/v1/runs"), ("GET", "/metrics"), ("GET", "/v1/runs/" + "a" * 32)]:
        code, _, headers = client(method, path, BODY if method == "POST" else None, token=None)
        assert code == 401 and headers["WWW-Authenticate"] == "Bearer", path
    assert client("GET", "/v1/runs", token="wrong")[0] == 401


def test_triage_then_fetch_run_trace_and_list(server):
    client, _ = server()
    code, body, headers = client("POST", "/v1/triage", BODY)
    assert code == 200 and body["status"] == "completed" and headers["X-Request-Id"]
    assert body["report"]["category"] == "npe" and "OrderService.java" in body["report"]["location"]
    assert body["metrics"]["llm_calls"] > 0 and body["metrics"]["cost_usd"] > 0

    run_id = body["run_id"]
    code, rec, _ = client("GET", f"/v1/runs/{run_id}")
    assert code == 200 and rec["status"] == "completed" and rec["state"]["review"] == "approve"
    code, trace, _ = client("GET", f"/v1/runs/{run_id}/trace")
    assert code == 200 and any(s["kind"] == "llm" for s in trace["spans"])
    code, listing, _ = client("GET", "/v1/runs?limit=5&status=completed")
    assert code == 200 and run_id in [r["id"] for r in listing["runs"]]


def test_input_validation_and_limits(server):
    client, _ = server(max_body_bytes=400)
    inc = lambda **kw: {"incident": {"description": "NullPointerException", **kw}}
    assert client("POST", "/v1/triage", raw=b"{not json")[0] == 400
    assert client("POST", "/v1/triage", raw=b"[1,2]")[0] == 400
    assert client("POST", "/v1/triage", {"incident": {"description": ""}})[0] == 400
    assert client("POST", "/v1/triage", inc(id="../etc/passwd"))[0] == 400
    assert client("POST", "/v1/triage", {**inc(), "config": "turbo"})[0] == 400
    assert client("POST", "/v1/triage", BODY, headers={"Content-Type": "text/plain"})[0] == 415
    code, body, _ = client("POST", "/v1/triage", raw=json.dumps(inc(description="x" * 600)).encode())
    assert code == 413 and "error" in body


def test_unknown_routes_and_runs_return_404_without_internals(server):
    client, _ = server()
    for method, path in [("GET", "/nope"), ("GET", "/v1/runs/" + "0" * 32), ("GET", "/v1/runs/not-a-run-id"),
                         ("GET", "/v1/runs/../../etc/passwd"), ("POST", "/v1/runs/" + "0" * 32 + "/resume"), ("GET", "/v1/triage")]:
        code, body, _ = client(method, path)
        assert code == 404 and "Traceback" not in json.dumps(body), (method, path, code)
    assert client("GET", "/v1/runs?limit=abc")[0] == 400


def test_llm_failure_is_a_200_with_failed_status_and_can_be_resumed_over_http(server):
    client, _ = server(WrapLLM(fail_at=9))
    code, body, _ = client("POST", "/v1/triage", BODY)
    assert code == 200 and body["status"] == "failed" and "injected" in body["error"]
    run_id = body["run_id"]
    code, resumed, _ = client("POST", f"/v1/runs/{run_id}/resume")
    assert code == 200 and resumed["status"] == "completed" and resumed["run_id"] == run_id
    assert client("POST", f"/v1/runs/{run_id}/resume")[0] == 409  # completed runs are not resumable


def test_budget_exceeded_is_reported_not_raised(server):
    client, _ = server(max_llm_calls=4)
    code, body, _ = client("POST", "/v1/triage", BODY)
    assert code == 200 and body["status"] == "budget_exceeded"


def test_sql_injection_in_query_string_is_harmless(server):
    client, _ = server()
    client("POST", "/v1/triage", BODY)
    code, body, _ = client("GET", "/v1/runs?status=x%27%3B%20DROP%20TABLE%20runs%3B%20--")
    assert code == 200 and body["runs"] == []
    assert len(client("GET", "/v1/runs")[1]["runs"]) == 1


def test_metrics_endpoint(server):
    client, _ = server()
    client("POST", "/v1/triage", BODY)
    client("GET", "/v1/runs/not-a-run-id")
    code, text, headers = client("GET", "/metrics")
    assert code == 200 and headers["Content-Type"].startswith("text/plain")
    assert 'triage_runs_total{config="optimized",status="completed"} 1.0' in text
    assert 'triage_http_requests_total{code="404",method="GET",route="/v1/runs/{id}"} 1.0' in text  # low-cardinality label
    assert "triage_run_cost_usd_bucket" in text


def test_concurrency_cap_returns_429_with_retry_after(server):
    client, _ = server(WrapLLM(delay=0.04), max_concurrent=1)
    first = {}
    t = threading.Thread(target=lambda: first.update(r=client("POST", "/v1/triage", BODY)))
    t.start()
    deadline = time.time() + 5
    while "triage_runs_in_flight 1.0" not in client("GET", "/metrics")[1] and time.time() < deadline:
        time.sleep(0.01)
    code, body, headers = client("POST", "/v1/triage", BODY)
    assert code == 429 and headers["Retry-After"] == "1"
    t.join()
    assert first["r"][0] == 200 and first["r"][1]["status"] == "completed"
    assert client("POST", "/v1/triage", BODY)[0] == 200  # slot released afterwards


def test_parallel_requests_are_isolated(server):
    client, _ = server(max_concurrent=8)
    results = []
    lock = threading.Lock()

    def go(i):
        r = client("POST", "/v1/triage", {"incident": {"id": f"INC-{101 + i}", "description": "NullPointerException checkout"}})
        with lock:
            results.append(r)

    threads = [threading.Thread(target=go, args=(i,)) for i in range(8)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert [r[0] for r in results] == [200] * 8
    assert len({r[1]["run_id"] for r in results}) == 8
    assert 'triage_runs_total{config="optimized",status="completed"} 8.0' in client("GET", "/metrics")[1]


def test_shutdown_drains_in_flight_runs(tmp_path):
    import urllib.request

    from triage.server import App, make_server
    from triage.settings import Settings

    app = App(Settings(db_path=str(tmp_path / "d.db")), llm=WrapLLM(delay=0.04))
    srv = make_server(app, "127.0.0.1", 0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    out = {}

    def call():
        req = urllib.request.Request(f"http://127.0.0.1:{srv.server_port}/v1/triage", json.dumps(BODY).encode(),
                                     {"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=30) as r:
            out["code"], out["body"] = r.status, json.loads(r.read())

    t = threading.Thread(target=call)
    t.start()
    deadline = time.time() + 5
    while "triage_runs_in_flight 1.0" not in app.metrics.render() and time.time() < deadline:
        time.sleep(0.01)
    srv.shutdown()  # stop accepting new connections (what SIGTERM does)
    t0 = time.time()
    srv.server_close()  # must wait for the in-flight run
    waited = time.time() - t0
    t.join()
    assert out["code"] == 200 and out["body"]["status"] == "completed"
    assert waited > 0.1, "server_close returned before the in-flight run finished"


def test_readiness_fails_when_database_is_down(server):
    client, app = server()
    app.store.ping = lambda: (_ for _ in ()).throw(RuntimeError("db down"))  # type: ignore[method-assign]
    code, body, _ = client("GET", "/readyz", token=None)
    assert code == 503 and "db down" not in json.dumps(body)
