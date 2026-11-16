"""Runtime settings, read from environment variables (12-factor). Validated at startup so a bad
value fails fast with a clear message instead of misbehaving later."""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Mapping

from .config import CONFIGS


@dataclass(frozen=True)
class Settings:
    # server
    host: str = "127.0.0.1"
    port: int = 8080
    token: str | None = None  # bearer token; required unless bound to loopback or allow_insecure
    allow_insecure: bool = False
    max_concurrent: int = 4
    max_body_bytes: int = 65_536
    # persistence
    db_path: str = "triage.db"
    # per-run guardrails
    max_cost_usd: float = 0.50
    max_llm_calls: int = 40
    max_total_tokens: int = 60_000
    run_timeout_s: float = 120.0
    # behaviour
    llm: str = "mock"  # mock | env (real model via TRIAGE_* variables, see providers.py)
    default_config: str = "optimized"
    log_level: str = "INFO"

    def __post_init__(self) -> None:
        if not (0 < self.port < 65536):
            raise ValueError(f"port out of range: {self.port}")
        for name in ("max_concurrent", "max_body_bytes", "max_llm_calls", "max_total_tokens"):
            if getattr(self, name) < 1:
                raise ValueError(f"{name} must be >= 1")
        if self.max_cost_usd <= 0 or self.run_timeout_s <= 0:
            raise ValueError("max_cost_usd and run_timeout_s must be > 0")
        if self.llm not in ("mock", "env"):
            raise ValueError("llm must be 'mock' or 'env'")
        if self.default_config not in CONFIGS:
            raise ValueError(f"default_config must be one of {list(CONFIGS)}")

    @property
    def is_loopback(self) -> bool:
        return self.host in ("127.0.0.1", "localhost", "::1")

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "Settings":
        e = os.environ if env is None else env

        def get(key, cast, default):
            raw = e.get(f"TRIAGE_{key}")
            if raw is None or raw == "":
                return default
            try:
                return cast(raw)
            except ValueError as exc:
                raise ValueError(f"TRIAGE_{key}={raw!r} is invalid: {exc}") from exc

        d = cls()
        return cls(
            host=get("HOST", str, d.host), port=get("PORT", int, d.port),
            token=get("SERVER_TOKEN", str, d.token),
            allow_insecure=get("ALLOW_INSECURE", lambda v: v.lower() in ("1", "true", "yes"), d.allow_insecure),
            max_concurrent=get("MAX_CONCURRENT", int, d.max_concurrent),
            max_body_bytes=get("MAX_BODY_BYTES", int, d.max_body_bytes),
            db_path=get("DB", str, d.db_path),
            max_cost_usd=get("MAX_COST_USD", float, d.max_cost_usd),
            max_llm_calls=get("MAX_LLM_CALLS", int, d.max_llm_calls),
            max_total_tokens=get("MAX_TOTAL_TOKENS", int, d.max_total_tokens),
            run_timeout_s=get("RUN_TIMEOUT_S", float, d.run_timeout_s),
            llm=get("LLM", str, d.llm), default_config=get("DEFAULT_CONFIG", str, d.default_config),
            log_level=get("LOG_LEVEL", lambda v: v.upper(), d.log_level),
        )
