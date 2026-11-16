"""Exercises the REAL client code path (HTTP, SSE streaming, retries, usage parsing) against a local
fake server that speaks both the Anthropic and the OpenAI-compatible wire formats. The fake server's
replies come from the MockLLM policies, so the full pipeline can be run through HttpLLM."""
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from triage.agents import ROLE_INSTRUCTIONS, build_prompt
from triage.llm import POLICIES
from triage.observability import PRICING, count_tokens
from triage.pipeline import run_incident
from triage.providers import HttpLLM, Tier
from triage.tools import SAMPLE

INCIDENTS = {i["id"]: i for i in json.loads((SAMPLE / "incidents.json").read_text())}
TIERS = {t: Tier(f"fake-{t}", *PRICING[t]) for t in ("small", "large")}


class FakeAPI:
    def __init__(self, fail_first: int = 0, transform=None):
        self.fail_first, self.transform, self.requests = fail_first, transform, 0
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                outer.requests += 1
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                if outer.requests <= outer.fail_first:
                    self.send_response(503)
                    self.end_headers()
                    self.wfile.write(b"overloaded")
                    return
                anthropic = self.path.endswith("/v1/messages")
                prompt = body["messages"][-1]["content"]
                role = next(r for r, ins in ROLE_INSTRUCTIONS.items() if ins in prompt)
                text = POLICIES[role](prompt)
                if outer.transform:
                    text = outer.transform(role, text)
                tin, tout = count_tokens(prompt), count_tokens(text)
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()

                def send(obj, event=None):
                    data = obj if isinstance(obj, str) else json.dumps(obj)
                    self.wfile.write((f"event: {event}\n" if event else "").encode() + f"data: {data}\n\n".encode())
                    self.wfile.flush()

                chunks = [text[i:i + 25] for i in range(0, len(text), 25)]
                if anthropic:
                    send({"type": "message_start", "message": {"usage": {"input_tokens": tin}}}, "message_start")
                    time.sleep(0.02)
                    for c in chunks:
                        send({"type": "content_block_delta", "delta": {"type": "text_delta", "text": c}}, "content_block_delta")
                    send({"type": "message_delta", "usage": {"output_tokens": tout}}, "message_delta")
                else:
                    time.sleep(0.02)
                    for c in chunks:
                        send({"choices": [{"delta": {"content": c}}]})
                    send({"choices": [], "usage": {"prompt_tokens": tin, "completion_tokens": tout}})
                    send("[DONE]")

        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def llm(self, provider: str, **kw) -> HttpLLM:
        return HttpLLM(provider, TIERS, "test-key", self.url, backoff_s=0, **kw)

    def close(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def api_factory():
    made = []

    def make(**kw):
        made.append(FakeAPI(**kw))
        return made[-1]

    yield make
    for a in made:
        a.close()


PROVIDERS = ["anthropic", "openai"]
SUP_PROMPT = build_prompt("supervisor", {"INCIDENT": "INC-101: NullPointerException", "STATE": "logs=no code=no fix=no review=none revisions=0"})


@pytest.mark.parametrize("provider", PROVIDERS)
def test_streaming_text_usage_ttft_and_cost(api_factory, provider):
    res = api_factory().llm(provider).complete("supervisor", SUP_PROMPT, "small")
    assert "NEXT: log_analyst" in res.text
    assert res.tokens_in == count_tokens(SUP_PROMPT) and res.tokens_out == count_tokens(res.text)
    assert 0 < res.ttft_s <= res.latency_s
    assert res.cost_usd == pytest.approx((res.tokens_in * PRICING["small"][0] + res.tokens_out * PRICING["small"][1]) / 1e6)
    assert res.model == "fake-small"


def test_retries_transient_errors_then_gives_up(api_factory):
    ok = api_factory(fail_first=2)
    assert "NEXT" in ok.llm("anthropic", max_retries=2).complete("supervisor", SUP_PROMPT, "small").text
    assert ok.requests == 3
    bad = api_factory(fail_first=3)
    with pytest.raises(RuntimeError, match="failed after 3 attempts"):
        bad.llm("anthropic", max_retries=2).complete("supervisor", SUP_PROMPT, "small")


@pytest.mark.parametrize("provider", PROVIDERS)
def test_full_pipeline_through_real_client_matches_mock(api_factory, provider):
    inc = INCIDENTS["INC-101"]
    via_http = run_incident(inc, "optimized", llm=api_factory().llm(provider))
    via_mock = run_incident(inc, "optimized")
    assert via_http["final"]["proposal"] == via_mock["final"]["proposal"]
    assert via_http["final"]["review"] == "approve" and via_http["final"]["revisions"] == 1
    h, m = via_http["summary"], via_mock["summary"]
    assert (h["tokens_in"], h["tokens_out"], h["llm_calls"]) == (m["tokens_in"], m["tokens_out"], m["llm_calls"])
    assert h["cost_usd"] == pytest.approx(m["cost_usd"])
    assert h["avg_ttft_s"] > 0  # measured, not simulated


def test_hallucinated_observation_is_discarded(api_factory):
    def transform(role, text):
        if role == "log_analyst" and text.startswith("Thought") and "Action:" in text:
            return text + "\nObservation: totally made up\nFinal: signal=FAKE; file=Fake.java; line=1"
        return text

    out = run_incident(INCIDENTS["INC-101"], "optimized", llm=api_factory(transform=transform).llm("anthropic"))["final"]
    assert "FAKE" not in out["log_findings"] and "file=OrderService.java" in out["log_findings"]
    assert out["review"] == "approve"


@pytest.mark.parametrize("supervisor_reply", ["Sure! Let's look at the logs first.", "CATEGORY: npe\nNEXT: FINISH", "NEXT: delete_everything"])
def test_bad_supervisor_output_is_repaired(api_factory, supervisor_reply):
    def transform(role, text):
        return supervisor_reply if role == "supervisor" else text

    out = run_incident(INCIDENTS["INC-102"], "optimized", llm=api_factory(transform=transform).llm("openai"))["final"]
    assert out["review"] == "approve" and "PaymentClient.java" in out["code_findings"]


def test_from_env_requires_models_and_warns_without_prices(monkeypatch, capsys):
    for k in ("TRIAGE_PROVIDER", "TRIAGE_BASE_URL", "TRIAGE_MODEL_SMALL", "TRIAGE_MODEL_LARGE", "TRIAGE_PRICE_SMALL", "TRIAGE_PRICE_LARGE", "TRIAGE_API_KEY", "OPENAI_API_KEY"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    with pytest.raises(SystemExit, match="TRIAGE_MODEL_SMALL"):
        HttpLLM.from_env()
    monkeypatch.setenv("TRIAGE_MODEL_SMALL", "s")
    monkeypatch.setenv("TRIAGE_MODEL_LARGE", "l")
    monkeypatch.setenv("TRIAGE_PRICE_LARGE", "3,15")
    llm = HttpLLM.from_env()
    assert llm.tiers["large"].price_out == 15.0 and llm.tiers["small"].model == "s"
    assert "TRIAGE_PRICE_SMALL not set" in capsys.readouterr().err
