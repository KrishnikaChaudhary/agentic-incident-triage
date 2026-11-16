"""Shared state schema.

The Annotated reducers are the single source of truth for both the built-in graph
runtime (graph.py) and the LangGraph port (langgraph_port.py).
"""
from __future__ import annotations

import operator
from typing import Annotated, TypedDict, get_args, get_origin, get_type_hints


class IncidentState(TypedDict, total=False):
    incident: dict
    category: str
    next: str
    hops: int
    log_findings: str
    code_findings: str
    proposal: str
    review: str  # none | revise | approve
    feedback: str
    revisions: int
    transcript: Annotated[list, operator.add]  # raw ReAct scratchpads, appended by each worker


def reducers_from(schema) -> dict:
    """Extract {key: reducer} from Annotated[...] fields."""
    out = {}
    for key, hint in get_type_hints(schema, include_extras=True).items():
        if get_origin(hint) is Annotated:
            out[key] = get_args(hint)[1]
    return out
