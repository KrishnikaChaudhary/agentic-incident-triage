"""Safety rails around the model and its tools.

* Budget            : hard per-run caps on LLM calls, tokens and cost
* sanitize          : strips instruction-like lines from untrusted tool output (prompt injection via logs/code)
* redact_secrets    : keeps credentials out of error messages and logs

Defence in depth: these are heuristics on top of the structural defences (read-only tools, an
allow-list of tool names, validated arguments, a sandboxed source root). See docs/SECURITY.md.
"""
from __future__ import annotations

import re

from .errors import BudgetExceeded


class Budget:
    """Per-run limits. before_call() stops a runaway loop early; after_call() charges the real usage."""

    def __init__(self, max_cost_usd: float, max_llm_calls: int, max_total_tokens: int):
        self.max_cost_usd, self.max_llm_calls, self.max_total_tokens = max_cost_usd, max_llm_calls, max_total_tokens
        self.calls = 0
        self.cost_usd = 0.0
        self.tokens = 0

    def before_call(self) -> None:
        if self.calls >= self.max_llm_calls:
            raise BudgetExceeded("llm_calls", self.max_llm_calls, self.calls)

    def after_call(self, result) -> None:
        self.calls += 1
        self.cost_usd += result.cost_usd
        self.tokens += result.tokens_in + result.tokens_out
        if self.cost_usd > self.max_cost_usd:
            raise BudgetExceeded("cost_usd", self.max_cost_usd, round(self.cost_usd, 6))
        if self.tokens > self.max_total_tokens:
            raise BudgetExceeded("total_tokens", self.max_total_tokens, self.tokens)


_INJECTION = [re.compile(p, re.I) for p in (
    r"ignore\s+(all\s+|any\s+|the\s+)?(previous|prior|above|earlier)\s+(instructions?|prompts?|rules?)",
    r"disregard\s+(all\s+|any\s+|the\s+)?(previous|prior|above|earlier)",
    r"\byou\s+are\s+now\b",
    r"\bsystem\s+prompt\b",
    r"\b(reveal|print|show|leak)\s+(your|the)\s+(instructions?|prompt|api[\s_-]?key|secrets?)\b",
    r"\bnew\s+instructions?\s*:",
    r"<\s*/?\s*(system|assistant|tool_output)\s*>",
)]
REMOVED = "[line removed: instruction-like content in untrusted data]"


def sanitize(text: str) -> tuple[str, int]:
    """Replace lines that look like instructions aimed at the model. Returns (clean_text, lines_removed).
    Removing a line is acceptable: legitimate logs/code essentially never contain these phrases."""
    out, removed = [], 0
    for line in text.splitlines():
        if any(p.search(line) for p in _INJECTION):
            out.append(REMOVED)
            removed += 1
        else:
            out.append(line)
    return "\n".join(out), removed


_SECRETS = [
    (re.compile(r"sk-[A-Za-z0-9_\-]{10,}"), "sk-[REDACTED]"),
    (re.compile(r"(Bearer\s+)[A-Za-z0-9._\-]{8,}", re.I), r"\1[REDACTED]"),
    (re.compile(r"((?:x-api-key|api[_-]?key|token)[\"'\s:=]+)[A-Za-z0-9._\-]{8,}", re.I), r"\1[REDACTED]"),
]


def redact_secrets(text: str) -> str:
    for pat, repl in _SECRETS:
        text = pat.sub(repl, text)
    return text
