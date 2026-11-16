"""Tools the specialist agents call: log search, code grep, file read.

Design:
* Tools are READ-ONLY and allow-listed by name; arguments are validated before they touch anything.
* Data comes from pluggable sources (LogSource / CodeSource). The bundled FileLogSource and
  DirCodeSource serve the sample data; implement the same two small interfaces to point the agents
  at Loki / Elasticsearch / a Git checkout.
* Everything a tool returns is UNTRUSTED data: instruction-like lines are stripped (guardrails.sanitize),
  output is capped, and it is wrapped in <tool_output> tags that the agent prompts say never to obey.
* Optimizations (config.py): per-request memoization, incident-scoped log search, windowed reads.
"""
from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Protocol

from .config import Config
from .errors import ToolError
from .guardrails import sanitize
from .logging_setup import event
from .observability import Tracer

log = logging.getLogger("triage.tools")

SAMPLE = Path(__file__).resolve().parents[1] / "sample_data"
SIM_TOOL_LATENCY_S = 0.08
TOOL_NAMES = ("search_logs", "grep_code", "read_file")
_TERM_RE = re.compile(r"^[^\r\n]{1,100}$")
_READ_RE = re.compile(r"^[\w./-]{1,200}:\d{1,6}$")


class LogSource(Protocol):
    def search(self, term: str, incident_id: str | None, limit: int) -> list[str]:
        """Return up to `limit` log entries (multi-line blocks) containing `term`; scoped to the incident if given."""


class CodeSource(Protocol):
    def resolve(self, path: str) -> str | None:
        """Map a (possibly partial) path to a canonical relative path inside the sandbox, or None."""

    def grep(self, pattern: str, limit: int) -> list[str]: ...

    def read(self, path: str, line: int, window: int | None) -> str:
        """Numbered source around `line` (whole file if window is None). Raises ToolError if not found."""


class FileLogSource:
    def __init__(self, path: Path | str):
        self.path = Path(path)

    def search(self, term: str, incident_id: str | None, limit: int) -> list[str]:
        blocks: list[list[str]] = []
        cur: list[str] = []
        for line in self.path.read_text().splitlines():
            if line[:1].isspace() and cur:  # continuation (stack frame)
                cur.append(line)
            else:
                cur = [line]
                blocks.append(cur)
        hits: list[str] = []
        for b in blocks:
            text = "\n".join(b)
            if term.lower() not in text.lower():
                continue
            if incident_id and f"[{incident_id}]" not in b[0]:
                continue
            hits.append(text)
            if len(hits) >= limit:
                break
        return hits


class DirCodeSource:
    """Serves source files from one directory tree. Paths can never escape `root` (symlinks included)."""

    def __init__(self, root: Path | str, suffixes: tuple[str, ...] = (".java",)):
        self.root = Path(root).resolve()
        self._files: dict[str, Path] = {}
        for p in sorted(self.root.rglob("*")):
            if p.suffix in suffixes and p.is_file() and p.resolve().is_relative_to(self.root):
                self._files[p.relative_to(self.root).as_posix()] = p

    def resolve(self, path: str) -> str | None:
        for rel in self._files:
            if rel == path or rel.endswith("/" + path):
                return rel
        return None

    def grep(self, pattern: str, limit: int) -> list[str]:
        out: list[str] = []
        for rel, p in self._files.items():
            for i, line in enumerate(p.read_text().splitlines(), 1):
                if pattern in line:
                    out.append(f"{rel}:{i}: {line.strip()}")
                    if len(out) >= limit:
                        return out
        return out

    def read(self, path: str, line: int, window: int | None) -> str:
        rel = self.resolve(path)
        if rel is None:
            raise ToolError(f"file not found: {path}")
        lines = self._files[rel].read_text().splitlines()
        lo, hi = (1, len(lines)) if window is None else (max(1, line - window), min(len(lines), line + window))
        body = "\n".join(f"{'>>' if n == line else '  '} {n}: {lines[n - 1]}" for n in range(lo, hi + 1))
        return f"FILE: {rel} LINE: {line}\n{body}"


class ToolBox:
    def __init__(self, tracer: Tracer, cfg: Config, incident_id: str, root: Path = SAMPLE,
                 logs: LogSource | None = None, code: CodeSource | None = None):
        self.tracer, self.cfg, self.incident_id = tracer, cfg, incident_id
        self.logs: LogSource = logs or FileLogSource(root / "logs" / "app.log")
        self.code: CodeSource = code or DirCodeSource(root / "app")
        self._cache: dict[tuple, str] = {}

    def call(self, name: str, arg: str, agent: str) -> str:
        with self.tracer.span(f"tool:{name}", "tool", agent=agent, tool=name, arg=arg) as sp:
            try:
                self._validate(name, arg)
                key = (name, self._normalize(name, arg))
                if self.cfg.cache_tools and key in self._cache:
                    sp.attrs["cache_hit"] = True
                    return self._cache[key]
                sp.latency_s = SIM_TOOL_LATENCY_S
                raw = self._run(name, arg)
                clean, removed = sanitize(raw)
                if removed:
                    sp.attrs["injection_lines_removed"] = removed
                    event(log, logging.WARNING, "injection_suspected", tool=name, agent=agent,
                          incident=self.incident_id, lines_removed=removed)
                out = f"<tool_output>\n{clean[: self.cfg.max_obs_chars]}\n</tool_output>"
                if self.cfg.cache_tools:
                    self._cache[key] = out
                return out
            except ToolError as exc:
                sp.status, sp.error = "error", str(exc)
                event(log, logging.INFO, "tool_rejected", tool=name, agent=agent, reason=str(exc))
                return f"error: {exc}"

    # ---- internals ----
    @staticmethod
    def _validate(name: str, arg: str) -> None:
        if name not in TOOL_NAMES:
            raise ToolError(f"unknown tool '{name}'; allowed: {', '.join(TOOL_NAMES)}")
        if name == "read_file":
            if not _READ_RE.match(arg) or ".." in arg.split(":")[0].split("/"):
                raise ToolError("read_file expects <relative/path.java>:<line> with no '..' segments")
        elif not _TERM_RE.match(arg):
            raise ToolError(f"{name} expects a single-line argument of 1-100 characters")

    def _normalize(self, name: str, arg: str) -> str:
        if name == "read_file":
            path, _, line = arg.rpartition(":")
            hit = self.code.resolve(path)
            return f"{hit}:{line}" if hit else arg
        return arg

    def _run(self, name: str, arg: str) -> str:
        if name == "search_logs":
            scope = self.incident_id if self.cfg.scope_tools else None
            hits = self.logs.search(arg, scope, self.cfg.max_log_matches)
            return "\n".join(hits) if hits else f"no log lines match '{arg}'"
        if name == "grep_code":
            hits = self.code.grep(arg, 5)
            return "\n".join(hits) if hits else f"no code matches '{arg}'"
        path, _, line_s = arg.rpartition(":")
        return self.code.read(path, int(line_s), self.cfg.read_window)
