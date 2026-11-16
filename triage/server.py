"""HTTP service (standard library only).

    GET  /healthz                 liveness (no auth)
    GET  /readyz                  readiness: database reachable (no auth)
    GET  /metrics                 Prometheus text format
    POST /v1/triage               {"incident": {"id"?, "description"}, "config"?} -> run result
    GET  /v1/runs[?limit=&status=]   recent runs
    GET  /v1/runs/{id}            one run (final or partial state)
    GET  /v1/runs/{id}/trace      full span trace (latest attempt)
    POST /v1/runs/{id}/resume     continue a failed / timeout / budget_exceeded run from its checkpoint

Security: bearer token (constant-time compare) on everything except health probes; refuses to listen
on a non-loopback address without a token unless TRIAGE_ALLOW_INSECURE=1; JSON content-type, body-size
and input validation; concurrency cap (429 when saturated); socket timeouts; no stack traces in responses.
Run:  python -m triage.server   (configure with TRIAGE_* environment variables, see settings.py)
"""
from __future__ import annotations

import argparse
import hmac
import json
import logging
import re
import signal
import threading
import time
import uuid
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from .config import CONFIGS
from .llm import MockLLM
from .logging_setup import configure, event
from .metrics import Metrics
from .pipeline import RESUMABLE, report_dict, resume_incident, run_incident, validate_incident
from .settings import Settings
from .store import Store

log = logging.getLogger("triage.server")
RUN_ID_RE = re.compile(r"^[0-9a-f]{32}$")
SUMMARY_KEYS = ("llm_calls", "tool_calls", "tool_cache_hits", "tokens_in", "tokens_out", "cost_usd",
                "tokens_per_sec", "avg_ttft_s", "latency_s", "injection_lines_removed")


class App:
    def __init__(self, settings: Settings, store: Store | None = None, llm=None, metrics: Metrics | None = None):
        self.settings = settings
        self.store = store or Store(settings.db_path)
        self.metrics = metrics or Metrics()
        self.llm = llm if llm is not None else self._make_llm()
        self.slots = threading.BoundedSemaphore(settings.max_concurrent)

    def _make_llm(self):
        if self.settings.llm == "env":
            from .providers import HttpLLM
            return HttpLLM.from_env()
        return MockLLM()


class Handler(BaseHTTPRequestHandler):
    app: App  # bound by make_server
    server_version = "triage"
    sys_version = ""
    timeout = 30  # socket timeout: protects against slow / stalled clients

    def log_message(self, *args) -> None:  # replaced by structured http_request events
        pass

    # ---- plumbing ----
    def _send(self, code: int, body: bytes, ctype: str = "application/json", headers: dict | None = None) -> None:
        self._code = code
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Request-Id", self._request_id)
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, code: int, obj, headers: dict | None = None) -> None:
        self._send(code, json.dumps(obj, default=str).encode(), headers=headers)

    def _error(self, code: int, message: str, headers: dict | None = None) -> None:
        self._json(code, {"error": message, "request_id": self._request_id}, headers)

    def _authorized(self) -> bool:
        token = self.app.settings.token
        if not token:
            return True
        supplied = self.headers.get("Authorization", "")
        return hmac.compare_digest(supplied.encode(), f"Bearer {token}".encode())

    def _read_json(self) -> dict:
        ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        if ctype != "application/json":
            raise _HttpError(415, "Content-Type must be application/json")
        try:
            length = int(self.headers.get("Content-Length", ""))
        except ValueError:
            raise _HttpError(411, "Content-Length required") from None
        if length < 0 or length > self.app.settings.max_body_bytes:
            raise _HttpError(413, f"body larger than {self.app.settings.max_body_bytes} bytes")
        try:
            obj = json.loads(self.rfile.read(length) or b"null")
        except (ValueError, UnicodeDecodeError):
            raise _HttpError(400, "body is not valid JSON") from None
        if not isinstance(obj, dict):
            raise _HttpError(400, "body must be a JSON object")
        return obj

    def _acquire(self) -> None:
        if not self.app.slots.acquire(blocking=False):
            raise _HttpError(429, "server is at its concurrency limit; retry shortly", {"Retry-After": "1"})

    # ---- dispatch ----
    def do_GET(self) -> None:
        self._dispatch("GET")

    def do_POST(self) -> None:
        self._dispatch("POST")

    def _dispatch(self, method: str) -> None:
        t0 = time.perf_counter()
        self._request_id = uuid.uuid4().hex[:12]
        self._code = 500
        route = "unmatched"
        try:
            route = self._route(method, urlparse(self.path))
        except _HttpError as e:
            self._error(e.code, e.message, e.headers)
        except Exception:  # noqa: BLE001 - never leak internals to the client
            log.exception("unhandled error", extra={"fields": {"request_id": self._request_id}})
            self._error(500, "internal error")
        finally:
            if route == "unmatched":  # errors raised before a route returned: still label known routes
                route = _label(urlparse(self.path).path)
            self.app.metrics.inc("triage_http_requests_total", method=method, route=route, code=self._code)
            event(log, logging.INFO, "http_request", request_id=self._request_id, method=method, route=route,
                  code=self._code, ms=round((time.perf_counter() - t0) * 1000, 1))

    def _route(self, method: str, url) -> str:
        path = url.path.rstrip("/") or "/"
        app, parts = self.app, path.strip("/").split("/")

        if path == "/healthz" and method == "GET":
            self._json(200, {"status": "ok"})
            return "/healthz"
        if path == "/readyz" and method == "GET":
            try:
                app.store.ping()
                self._json(200, {"status": "ready", "llm": app.settings.llm})
            except Exception:  # noqa: BLE001
                self._error(503, "database not reachable")
            return "/readyz"

        if not self._authorized():
            raise _HttpError(401, "missing or invalid bearer token", {"WWW-Authenticate": "Bearer"})

        if path == "/metrics" and method == "GET":
            self._send(200, app.metrics.render().encode(), "text/plain; version=0.0.4; charset=utf-8")
            return "/metrics"
        if path == "/v1/triage" and method == "POST":
            self._triage()
            return "/v1/triage"
        if path == "/v1/runs" and method == "GET":
            q = parse_qs(url.query)
            try:
                limit = int(q.get("limit", ["20"])[0])
            except ValueError:
                raise _HttpError(400, "limit must be an integer") from None
            self._json(200, {"runs": app.store.list_runs(limit, q.get("status", [None])[0])})
            return "/v1/runs"
        if len(parts) >= 3 and parts[:2] == ["v1", "runs"]:
            run_id = parts[2]
            if not RUN_ID_RE.match(run_id):
                raise _HttpError(404, "run not found")
            if len(parts) == 3 and method == "GET":
                rec = app.store.get_run(run_id)
                if rec is None:
                    raise _HttpError(404, "run not found")
                self._json(200, rec)
                return "/v1/runs/{id}"
            if len(parts) == 4 and parts[3] == "trace" and method == "GET":
                trace = app.store.get_trace(run_id)
                if trace is None:
                    raise _HttpError(404, "trace not found")
                self._json(200, trace)
                return "/v1/runs/{id}/trace"
            if len(parts) == 4 and parts[3] == "resume" and method == "POST":
                self._resume(run_id)
                return "/v1/runs/{id}/resume"
        raise _HttpError(404, "not found")

    # ---- handlers ----
    def _result_payload(self, result: dict) -> dict:
        return {"run_id": result["run_id"], "status": result["status"], "error": result["error"],
                "config": result["config"], "report": report_dict(result),
                "metrics": {k: result["summary"][k] for k in SUMMARY_KEYS}}

    def _triage(self) -> None:
        body = self._read_json()
        try:
            incident = validate_incident(body.get("incident"))
        except ValueError as e:
            raise _HttpError(400, str(e)) from None
        cfg = body.get("config", self.app.settings.default_config)
        if cfg not in CONFIGS:
            raise _HttpError(400, f"config must be one of {sorted(CONFIGS)}")
        self._acquire()
        try:
            result = run_incident(incident, cfg, self.app.llm, settings=self.app.settings,
                                  store=self.app.store, metrics=self.app.metrics)
        finally:
            self.app.slots.release()
        self._json(200, self._result_payload(result))

    def _resume(self, run_id: str) -> None:
        rec = self.app.store.get_run(run_id)
        if rec is None:
            raise _HttpError(404, "run not found")
        if rec["status"] not in RESUMABLE:
            raise _HttpError(409, f"run status is '{rec['status']}'; only {sorted(RESUMABLE)} can be resumed")
        self._acquire()
        try:
            result = resume_incident(run_id, self.app.store, self.app.llm, settings=self.app.settings,
                                     metrics=self.app.metrics)
        except ValueError as e:  # lost a race with another resume
            raise _HttpError(409, str(e)) from None
        finally:
            self.app.slots.release()
        self._json(200, self._result_payload(result))


def _label(raw_path: str) -> str:
    """Low-cardinality route label for metrics (never the raw path: run ids would explode cardinality)."""
    p = raw_path.rstrip("/") or "/"
    if p in ("/healthz", "/readyz", "/metrics", "/v1/triage", "/v1/runs"):
        return p
    parts = p.strip("/").split("/")
    if len(parts) >= 3 and parts[:2] == ["v1", "runs"]:
        return "/v1/runs/{id}" + (f"/{parts[3]}" if len(parts) == 4 and parts[3] in ("trace", "resume") else "")
    return "unmatched"


class _HttpError(Exception):
    def __init__(self, code: int, message: str, headers: dict | None = None):
        super().__init__(message)
        self.code, self.message, self.headers = code, message, headers


def make_server(app: App, host: str | None = None, port: int | None = None) -> ThreadingHTTPServer:
    handler = type("BoundHandler", (Handler,), {"app": app})
    srv = ThreadingHTTPServer((host or app.settings.host, app.settings.port if port is None else port), handler)
    srv.daemon_threads = False  # server_close() then waits for in-flight requests (graceful drain)
    return srv


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description="Incident triage HTTP service (configure via TRIAGE_* env vars)")
    ap.add_argument("--host")
    ap.add_argument("--port", type=int)
    args = ap.parse_args(argv)
    settings = Settings.from_env()
    settings = replace(settings, **{k: v for k, v in (("host", args.host), ("port", args.port)) if v is not None})
    configure(settings.log_level)
    if not settings.is_loopback and not settings.token and not settings.allow_insecure:
        raise SystemExit("Refusing to listen on a non-loopback address without TRIAGE_SERVER_TOKEN "
                         "(set a token, or TRIAGE_ALLOW_INSECURE=1 to override).")
    app = App(settings)
    if settings.llm == "mock":
        event(log, logging.WARNING, "mock_llm_active", note="responses come from the deterministic MockLLM; set TRIAGE_LLM=env for a real model")
    srv = make_server(app)

    def stop(signum, _frame):
        event(log, logging.INFO, "shutdown_requested", signal=signum)
        threading.Thread(target=srv.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    event(log, logging.INFO, "server_started", host=settings.host, port=srv.server_port, llm=settings.llm,
          auth="token" if settings.token else "none", db=settings.db_path)
    try:
        srv.serve_forever()
    finally:
        srv.server_close()  # waits for in-flight requests
        event(log, logging.INFO, "server_stopped")


if __name__ == "__main__":
    main()
