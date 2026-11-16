"""Real LLM clients, standard library only.

* provider "anthropic": Anthropic Messages API (streaming SSE)
* provider "openai":    any OpenAI-compatible chat-completions endpoint (OpenAI, Gemini's
                        OpenAI-compatible endpoint, Groq, Ollama, vLLM, ...), streaming SSE

Streaming means time-to-first-token is MEASURED (first text delta), and token counts come
from the provider's usage fields (falling back to the approximate counter if absent).

Configure from the environment (model names and prices are required on purpose, so nothing
stale is hard-coded here):

    TRIAGE_PROVIDER      anthropic | openai            (default anthropic)
    TRIAGE_MODEL_SMALL   model id for the cheap tier   (supervisor, log analyst, investigator, critic)
    TRIAGE_MODEL_LARGE   model id for the strong tier  (fix proposer; everything in the baseline)
    TRIAGE_PRICE_SMALL   "input,output" USD per 1M tokens, e.g. "0.25,1.25"
    TRIAGE_PRICE_LARGE   "input,output" USD per 1M tokens
    TRIAGE_BASE_URL      optional (e.g. http://localhost:11434/v1 for Ollama)
    ANTHROPIC_API_KEY / OPENAI_API_KEY / TRIAGE_API_KEY
"""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass

from .llm import LLMResult
from .observability import PRICING, count_tokens

SYSTEM = (
    "Follow the INSTRUCTIONS section exactly and reply only in the requested format. "
    "Never write an 'Observation:' line yourself; observations are supplied by the tools."
)
RETRYABLE = {408, 429, 500, 502, 503, 504, 529}
DEFAULT_BASE = {"anthropic": "https://api.anthropic.com", "openai": "https://api.openai.com/v1"}


@dataclass
class Tier:
    model: str
    price_in: float  # USD per 1M input tokens
    price_out: float  # USD per 1M output tokens


class HttpLLM:
    def __init__(self, provider: str, tiers: dict[str, Tier], api_key: str = "", base_url: str | None = None,
                 timeout: float = 60.0, max_retries: int = 2, backoff_s: float = 1.0, max_tokens: int = 1024):
        if provider not in DEFAULT_BASE:
            raise ValueError(f"provider must be one of {list(DEFAULT_BASE)}")
        self.provider, self.tiers, self.api_key = provider, tiers, api_key
        self.base_url = (base_url or DEFAULT_BASE[provider]).rstrip("/")
        self.timeout, self.max_retries, self.backoff_s, self.max_tokens = timeout, max_retries, backoff_s, max_tokens

    @classmethod
    def from_env(cls) -> "HttpLLM":
        env = os.environ
        provider = env.get("TRIAGE_PROVIDER", "anthropic")
        base_url = env.get("TRIAGE_BASE_URL")
        key = env.get("TRIAGE_API_KEY") or env.get({"anthropic": "ANTHROPIC_API_KEY", "openai": "OPENAI_API_KEY"}.get(provider, ""), "")
        if not key and not base_url:
            raise SystemExit("No API key: set TRIAGE_API_KEY (or ANTHROPIC_API_KEY / OPENAI_API_KEY), or TRIAGE_BASE_URL for a local server.")
        tiers = {}
        for tier in ("small", "large"):
            model = env.get(f"TRIAGE_MODEL_{tier.upper()}")
            if not model:
                raise SystemExit(f"Set TRIAGE_MODEL_{tier.upper()} to a model id.")
            price = env.get(f"TRIAGE_PRICE_{tier.upper()}")
            if price:
                p_in, p_out = (float(x) for x in price.split(","))
            else:
                p_in, p_out = PRICING[tier]
                print(f"warning: TRIAGE_PRICE_{tier.upper()} not set; using placeholder prices, so cost figures are NOT real", file=sys.stderr)
            tiers[tier] = Tier(model, p_in, p_out)
        return cls(provider, tiers, key, base_url)

    # ---- public API (same shape as MockLLM) ----
    def complete(self, role: str, prompt: str, model: str) -> LLMResult:
        tier = self.tiers[model]
        last: Exception | None = None
        for attempt in range(self.max_retries + 1):
            try:
                text, tin, tout, ttft, latency = self._stream(tier.model, prompt)
                break
            except urllib.error.HTTPError as e:
                if e.code not in RETRYABLE:
                    raise RuntimeError(f"LLM HTTP {e.code}: {e.read()[:300]!r}") from e
                last = e
            except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
                last = e
            time.sleep(self.backoff_s * (2 ** attempt))
        else:
            raise RuntimeError(f"LLM call failed after {self.max_retries + 1} attempts: {last}")
        tin = tin or count_tokens(prompt)
        tout = tout or count_tokens(text)
        cost = (tin * tier.price_in + tout * tier.price_out) / 1_000_000
        return LLMResult(text, tin, tout, tier.model, ttft, latency, cost)

    # ---- internals ----
    def _request(self, model: str, prompt: str) -> urllib.request.Request:
        if self.provider == "anthropic":
            url = f"{self.base_url}/v1/messages"
            headers = {"x-api-key": self.api_key, "anthropic-version": "2023-06-01"}
            body = {"model": model, "max_tokens": self.max_tokens, "temperature": 0, "stream": True,
                    "system": SYSTEM, "messages": [{"role": "user", "content": prompt}]}
        else:
            url = f"{self.base_url}/chat/completions"
            headers = {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}
            body = {"model": model, "max_tokens": self.max_tokens, "temperature": 0, "stream": True,
                    "stream_options": {"include_usage": True},
                    "messages": [{"role": "system", "content": SYSTEM}, {"role": "user", "content": prompt}]}
        headers = {"Content-Type": "application/json", "Accept": "text/event-stream", **headers}
        return urllib.request.Request(url, json.dumps(body).encode(), headers, method="POST")

    def _stream(self, model: str, prompt: str) -> tuple[str, int, int, float, float]:
        parts: list[str] = []
        tin = tout = 0
        ttft = 0.0
        t0 = time.perf_counter()
        with urllib.request.urlopen(self._request(model, prompt), timeout=self.timeout) as resp:
            for raw in resp:
                line = raw.decode("utf-8", "replace").strip()
                if not line.startswith("data:"):
                    continue
                payload = line[5:].strip()
                if payload == "[DONE]":
                    break
                obj = json.loads(payload)
                delta = ""
                if self.provider == "anthropic":
                    kind = obj.get("type")
                    if kind == "message_start":
                        tin = obj.get("message", {}).get("usage", {}).get("input_tokens", tin)
                    elif kind == "content_block_delta":
                        delta = obj.get("delta", {}).get("text", "")
                    elif kind == "message_delta":
                        tout = obj.get("usage", {}).get("output_tokens", tout)
                    elif kind == "error":
                        raise RuntimeError(f"LLM stream error: {obj.get('error')}")
                else:
                    for ch in obj.get("choices") or []:
                        delta += (ch.get("delta") or {}).get("content") or ""
                    usage = obj.get("usage")
                    if usage:
                        tin = usage.get("prompt_tokens", tin)
                        tout = usage.get("completion_tokens", tout)
                if delta:
                    if not parts:
                        ttft = time.perf_counter() - t0
                    parts.append(delta)
        return "".join(parts), tin, tout, ttft, time.perf_counter() - t0
