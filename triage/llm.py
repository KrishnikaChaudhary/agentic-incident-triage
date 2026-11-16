"""LLM interface plus a deterministic MockLLM so the pipeline runs offline.

Any real client only needs one method:

    complete(role: str, prompt: str, model: "small" | "large") -> LLMResult

MockLLM is rule-based: it reads the labelled prompt sections (### INCIDENT, ### SCRATCHPAD, ...)
and emits ReAct-formatted text, so the agent loop, parsing, state handoff, token accounting
and cost maths are all exercised for real. Latency/TTFT are SIMULATED from per-model speeds.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Protocol

from .observability import cost_usd, count_tokens

# (time-to-first-token seconds, output tokens/sec) -- simulated
SIM_SPEED = {"small": (0.15, 180.0), "large": (0.45, 70.0)}


@dataclass
class LLMResult:
    text: str
    tokens_in: int
    tokens_out: int
    model: str
    ttft_s: float
    latency_s: float
    cost_usd: float


def section(prompt: str, name: str) -> str:
    m = re.search(rf"### {name}\n(.*?)(?=\n### |\Z)", prompt, re.S)
    return m.group(1).strip() if m else ""


CATEGORIES = [
    ("npe", "NullPointerException"),
    ("timeout", "SocketTimeoutException"),
    ("pool", "Hikari"),
    ("json", "UnrecognizedPropertyException"),
    ("oom", "OutOfMemoryError"),
]
SEARCH_HINT = {"pool": "setMaximumPoolSize"}

PATCHES = {
    "npe": "Add an explicit null check before the dereference and throw CustomerNotFoundException (mapped to HTTP 404) instead of failing with a 500.",
    "timeout": "Configure connect and per-request timeouts on the HTTP client (2s / 5s) and wrap the charge call in a bounded retry with exponential backoff and an idempotency key.",
    "pool": "Raise the Hikari maximum pool size from 5 to a value sized for peak concurrency, enable leak detection, and make sure connections are closed with try-with-resources.",
    "json": "Disable FAIL_ON_UNKNOWN_PROPERTIES on the ObjectMapper (or add the missing taxCode field to Invoice) so newer partner payloads still deserialize.",
    "oom": "Stream the export in pages of 5000 rows and write each page to the output as it arrives, instead of loading findAll() into one in-memory list.",
}
TESTS = {
    "npe": "unit test calling shippingLabel with an unknown customer id and asserting CustomerNotFoundException.",
    "timeout": "integration test against a stub server that stalls and asserting the call fails fast and retries once.",
    "pool": "load test with 50 concurrent requests asserting no connection-timeout errors.",
    "json": "unit test parsing a payload with an extra taxCode field and asserting it succeeds.",
    "oom": "test exporting 2 million rows under a 256MB heap and asserting it completes.",
}


def classify(text: str) -> str:
    low = text.lower()
    for cat, kw in CATEGORIES:
        if kw.lower() in low:
            return cat
    return "unknown"


def _signature(text: str) -> str:
    m = re.search(r"\b[A-Z]\w*(?:Exception|Error)\b", text)
    if m:
        return m.group(0)
    return "Hikari" if "hikari" in text.lower() else "ERROR"


def _kv(text: str, key: str, default: str = "none") -> str:
    m = re.search(rf"{key}=([^;]+)", text)
    return m.group(1).strip() if m else default


def p_supervisor(prompt: str) -> str:
    cat = classify(section(prompt, "INCIDENT"))
    f = dict(kv.split("=") for kv in section(prompt, "STATE").split())
    if f["logs"] == "no":
        nxt = "log_analyst"
    elif f["code"] == "no":
        nxt = "code_investigator"
    elif f["fix"] == "no":
        nxt = "fix_proposer"
    elif f["review"] == "none":
        nxt = "critic"
    elif f["review"] == "revise" and int(f["revisions"]) < 2:
        nxt = "fix_proposer"
    else:
        nxt = "FINISH"
    return f"CATEGORY: {cat}\nNEXT: {nxt}"


def p_log_analyst(prompt: str) -> str:
    pad = section(prompt, "SCRATCHPAD")
    if "Observation:" not in pad:
        term = _signature(section(prompt, "INCIDENT"))
        return f"Thought: Search the logs for the failure signature.\nAction: search_logs[{term}]"
    obs = pad.rsplit("Observation:", 1)[-1]
    head = re.search(r"\[INC-\d+\]\s*(.+)", obs)
    sig = head.group(1).strip()[:90] if head else "unknown"
    fr = re.search(r"\((\w+\.java):(\d+)\)", obs)
    file, line = (fr.group(1), fr.group(2)) if fr else ("none", "0")
    return f"Thought: The logs identify the failing signal.\nFinal: signal={sig}; file={file}; line={line}"


def p_code_investigator(prompt: str) -> str:
    pad = section(prompt, "SCRATCHPAD")
    lf = section(prompt, "LOG_FINDINGS")
    n_obs = pad.count("Observation:")
    file, line = _kv(lf, "file"), _kv(lf, "line", "0")
    if n_obs == 0:
        if file != "none":
            return f"Thought: The stack frame names the file; read it.\nAction: read_file[{file}:{line}]"
        hint = SEARCH_HINT.get(section(prompt, "CATEGORY"), "TODO")
        return f"Thought: No stack frame; grep for the relevant setting.\nAction: grep_code[{hint}]"
    last = pad.rsplit("Observation:", 1)[-1]
    if "FILE:" not in last:  # last observation was a grep
        m = re.search(r"([\w/]+\.java):(\d+):", last)
        if not m:
            return "Thought: Nothing found.\nFinal: file=none; line=0; snippet=none"
        return f"Thought: Read the matching file.\nAction: read_file[{m.group(1)}:{m.group(2)}]"
    loc = re.search(r"FILE: (\S+) LINE: (\d+)", last)
    if not loc:  # e.g. the read_file call was rejected or the file was missing
        return "Thought: Could not read the file.\nFinal: file=none; line=0; snippet=none"
    snip = re.search(r">>\s*\d+:\s*(.*)", last)
    return f"Thought: I have the code context.\nFinal: file={loc.group(1)}; line={loc.group(2)}; snippet={snip.group(1).strip() if snip else 'n/a'}"


def p_fix_proposer(prompt: str) -> str:
    cat = section(prompt, "CATEGORY")
    code = section(prompt, "CODE_FINDINGS")
    feedback = section(prompt, "FEEDBACK")
    inc_id = re.search(r"INC-(\d+)", section(prompt, "INCIDENT"))
    # Deterministic stand-in for model quality: even incident ids get a complete answer first time,
    # odd ones omit the regression test until the critic asks for it (exercises the reflection loop).
    complete = feedback not in ("", "none") or (inc_id is not None and int(inc_id.group(1)) % 2 == 0)
    out = f"FIX for {_kv(code, 'file')}:{_kv(code, 'line')}\n{PATCHES.get(cat, 'Investigate manually.')}"
    if complete:
        out += f"\nTest: {TESTS.get(cat, 'add a regression test.')}"
    return out


def p_critic(prompt: str) -> str:
    pad = section(prompt, "SCRATCHPAD")
    code = section(prompt, "CODE_FINDINGS")
    if "Observation:" not in pad:
        return f"Thought: Verify the cited code really exists.\nAction: read_file[{_kv(code, 'file')}:{_kv(code, 'line', '1')}]"
    if "Test:" in section(prompt, "PROPOSAL"):
        return "Thought: Fix cites real code and includes a regression test.\nFinal: VERDICT=APPROVE; FEEDBACK=none"
    return "Thought: Fix is plausible but untested.\nFinal: VERDICT=REVISE; FEEDBACK=Add a regression test that reproduces the failure."


POLICIES = {
    "supervisor": p_supervisor,
    "log_analyst": p_log_analyst,
    "code_investigator": p_code_investigator,
    "fix_proposer": p_fix_proposer,
    "critic": p_critic,
}


class LLMClient(Protocol):
    """What the pipeline needs from a model client (MockLLM and providers.HttpLLM both satisfy it)."""

    def complete(self, role: str, prompt: str, model: str) -> LLMResult: ...


class MockLLM:
    def complete(self, role: str, prompt: str, model: str) -> LLMResult:
        text = POLICIES[role](prompt)
        tin, tout = count_tokens(prompt), count_tokens(text)
        ttft, tps = SIM_SPEED[model]
        return LLMResult(text, tin, tout, model, ttft, ttft + tout / tps, cost_usd(model, tin, tout))
