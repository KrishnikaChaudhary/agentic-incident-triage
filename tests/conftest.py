import json
import threading
import time
import urllib.error
import urllib.request

import pytest

from triage.llm import MockLLM
from triage.server import App, make_server
from triage.settings import Settings
from triage.tools import SAMPLE

INCIDENTS = {i["id"]: i for i in json.loads((SAMPLE / "incidents.json").read_text())}


class WrapLLM:
    """MockLLM with fault injection: delay, a failure on the Nth call, or output rewriting."""

    def __init__(self, transform=None, delay: float = 0.0, fail_at: int | None = None, fail_exc: Exception | None = None):
        self.transform, self.delay, self.fail_at, self.fail_exc = transform, delay, fail_at, fail_exc
        self.calls = 0
        self._lock = threading.Lock()
        self._inner = MockLLM()

    def complete(self, role, prompt, model):
        with self._lock:
            self.calls += 1
            n = self.calls
        if self.fail_at == n:
            raise self.fail_exc or RuntimeError("injected LLM failure")
        if self.delay:
            time.sleep(self.delay)
        res = self._inner.complete(role, prompt, model)
        if self.transform:
            res.text = self.transform(role, res.text)
        return res


@pytest.fixture
def incidents():
    return INCIDENTS


@pytest.fixture
def server(tmp_path):
    """Start a real HTTP server on an ephemeral port; yields (client, app)."""
    started = []

    def start(llm=None, **overrides):
        settings = Settings(db_path=str(tmp_path / "t.db"), token="test-token", **overrides)
        app = App(settings, llm=llm or MockLLM())
        srv = make_server(app, "127.0.0.1", 0)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        started.append(srv)
        base = f"http://127.0.0.1:{srv.server_port}"

        def client(method, path, body=None, token="test-token", headers=None, raw=None):
            hdrs = {"Content-Type": "application/json", **(headers or {})}
            if token:
                hdrs["Authorization"] = f"Bearer {token}"
            data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
            req = urllib.request.Request(base + path, data=data, headers=hdrs, method=method)
            try:
                with urllib.request.urlopen(req, timeout=20) as r:
                    code, text, h = r.status, r.read().decode(), r.headers
            except urllib.error.HTTPError as e:
                code, text, h = e.code, e.read().decode(), e.headers
            try:
                return code, json.loads(text), h
            except ValueError:
                return code, text, h

        return client, app

    yield start
    for srv in started:
        srv.shutdown()
        srv.server_close()
