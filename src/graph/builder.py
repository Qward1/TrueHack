"""Build and compile the LangGraph agent graph."""

from __future__ import annotations

from functools import partial
from typing import Any

from langgraph.graph import END, START, StateGraph

from src.core.config import Settings
from src.core.state import AgentState
from src.graph.conditions import (
    check_plan_result,
    check_validation,
    has_more_tasks,
    route_by_intent,
)
from src.graph.nodes import create_nodes


def build_graph(agents: dict[str, Any], settings: Settings):
    """Construct and compile the full agent graph.

    Parameters
    ----------
    agents:
        Dict with keys ``router``, ``planner``, ``coder``, ``validator``, ``qa``.
    settings:
        Application settings (used for ``max_fix_iterations``).

    Returns
    -------
    CompiledGraph
    """
    nodes = create_nodes(agents)
    max_fix = settings.max_fix_iterations

    g = StateGraph(AgentState)

    # ── Register nodes ────────────────────────────────────────────────
    for name, fn in nodes.items():
        g.add_node(name, fn)

    # ── Edges ─────────────────────────────────────────────────────────
    # START → route_intent
    g.add_edge(START, "route_intent")

    # route_intent → [route_by_intent] → plan / refine / answer
    g.add_conditional_edges(
        "route_intent",
        route_by_intent,
        {
            "plan": "plan_task",
            "refine": "refine_code",
            "answer": "answer_question",
        },
    )

    # plan_task → [check_plan_result] → respond (END) / generate
    g.add_conditional_edges(
        "plan_task",
        check_plan_result,
        {
            "respond": END,
            "generate": "generate_code",
        },
    )

    # generate_code → [has_more_tasks] → generate (loop) / validate
    g.add_conditional_edges(
        "generate_code",
        has_more_tasks,
        {
            "generate": "generate_code",
            "validate": "validate_code",
        },
    )

    # validate_code → [check_validation] → assemble / fix / force_assemble
    _check = partial(check_validation, max_fix_iterations=max_fix)
    g.add_conditional_edges(
        "validate_code",
        _check,
        {
            "assemble": "assemble_code",
            "fix": "fix_code",
            "force_assemble": "assemble_code",
        },
    )

    # fix_code → back to validate_code (loop)
    g.add_edge("fix_code", "validate_code")

    # assemble_code → prepare_response → END
    g.add_edge("assemble_code", "prepare_response")
    g.add_edge("prepare_response", END)

    # refine_code → validate_code (same validation flow)
    g.add_edge("refine_code", "validate_code")

    # answer_question → END
    g.add_edge("answer_question", END)

    return g.compile()
