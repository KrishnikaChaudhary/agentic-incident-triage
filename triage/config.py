"""Run configurations. Each flag is one optimization so we can ablate them."""
from __future__ import annotations

from dataclasses import dataclass, replace


@dataclass(frozen=True)
class Config:
    name: str = "baseline"
    compact_state: bool = False  # hand off summaries, not full ReAct transcripts
    route_models: bool = False  # cheap model for routing/extraction/review, strong model for the fix
    cache_tools: bool = False  # memoize identical tool calls within a request
    scope_tools: bool = False  # scope log search to the incident, window file reads, cap observations

    def model_for(self, agent: str) -> str:
        if not self.route_models:
            return "large"
        return "large" if agent == "fix_proposer" else "small"

    # tool observation limits
    @property
    def max_obs_chars(self) -> int:
        return 600 if self.scope_tools else 4000

    @property
    def max_log_matches(self) -> int:
        return 3 if self.scope_tools else 8

    @property
    def read_window(self) -> int | None:
        return 3 if self.scope_tools else None  # None = whole file


BASELINE = Config("baseline")
CONFIGS = {
    "baseline": BASELINE,
    "compact_state": replace(BASELINE, name="compact_state", compact_state=True),
    "model_routing": replace(BASELINE, name="model_routing", route_models=True),
    "tool_scoping_cache": replace(BASELINE, name="tool_scoping_cache", scope_tools=True, cache_tools=True),
    "optimized": Config("optimized", compact_state=True, route_models=True, cache_tools=True, scope_tools=True),
}
