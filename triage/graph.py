"""Minimal StateGraph runtime with the same contract as LangGraph:

    nodes are functions  state -> partial-update dict
    edges / conditional edges decide the next node
    Annotated reducers merge updates (e.g. operator.add for lists)
    a checkpointer (anything with .append(dict)) records the state after every step

Production behaviours: node exceptions are wrapped in GraphRunError carrying the last good state
(so a failed run still has a partial result), a wall-clock deadline is enforced between steps,
and a run can be resumed at any node with `start_at`.

langgraph_port.py wires the same node functions into the real LangGraph.
"""
from __future__ import annotations

import copy
import time
from typing import Callable

from .errors import RunTimeout
from .state import reducers_from

END = "__end__"


class GraphRunError(Exception):
    """A node raised (or the deadline passed). `.state` is the last consistent state, `.cause` the original error."""

    def __init__(self, node: str, state: dict, cause: BaseException):
        super().__init__(f"node '{node}' failed: {type(cause).__name__}: {cause}")
        self.node, self.state, self.cause = node, state, cause


class GraphRecursionError(RecursionError):
    def __init__(self, message: str, state: dict):
        super().__init__(message)
        self.state = state


class CompiledGraph:
    def __init__(self, g: "StateGraph", checkpointer, recursion_limit: int):
        assert g.entry, "set_entry_point first"
        self.g = g
        self.entry: str = g.entry
        self.checkpointer = checkpointer
        self.recursion_limit = recursion_limit

    def invoke(self, state: dict, start_at: str | None = None, deadline: float | None = None) -> dict:
        """Run to completion. `deadline` is a time.monotonic() value; `start_at` resumes at a node."""
        state = dict(state)
        node: str = start_at or self.entry
        steps = 0
        while node != END:
            if steps >= self.recursion_limit:
                raise GraphRecursionError(f"graph exceeded {self.recursion_limit} steps (stuck in a loop?)", state)
            if deadline is not None and time.monotonic() > deadline:
                raise GraphRunError(node, state, RunTimeout("run deadline exceeded"))
            try:
                update = self.g.nodes[node](state) or {}
            except Exception as exc:  # noqa: BLE001 - re-raised with context
                raise GraphRunError(node, state, exc) from exc
            for key, val in update.items():
                reducer = self.g.reducers.get(key)
                state[key] = reducer(state.get(key, []), val) if reducer else val
            if self.checkpointer is not None:
                self.checkpointer.append({"step": steps, "node": node, "state": copy.deepcopy(state)})
            node = self._next(node, state)
            steps += 1
        return state

    def _next(self, node: str, state: dict) -> str:
        if node in self.g.conditional:
            router, mapping = self.g.conditional[node]
            key = router(state)
            return mapping.get(key, key) if mapping else key
        return self.g.edges.get(node, END)


class StateGraph:
    def __init__(self, schema):
        self.reducers = reducers_from(schema)
        self.nodes: dict[str, Callable] = {}
        self.edges: dict[str, str] = {}
        self.conditional: dict[str, tuple] = {}
        self.entry: str | None = None

    def add_node(self, name: str, fn: Callable) -> None:
        self.nodes[name] = fn

    def set_entry_point(self, name: str) -> None:
        self.entry = name

    def add_edge(self, src: str, dst: str) -> None:
        self.edges[src] = dst

    def add_conditional_edges(self, src: str, router: Callable, mapping: dict | None = None) -> None:
        self.conditional[src] = (router, mapping)

    def compile(self, checkpointer=None, recursion_limit: int = 25) -> CompiledGraph:
        assert self.entry, "set_entry_point first"
        return CompiledGraph(self, checkpointer, recursion_limit)
