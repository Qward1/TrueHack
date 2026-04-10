"""Build and compile the LangGraph agent pipeline."""

from __future__ import annotations

from typing import Any

from langgraph.graph import END, START, StateGraph

from src.core.llm import LLMProvider
from src.core.state import PipelineState
from src.graph.conditions import (
    check_validation,
    check_verification,
    route_by_intent,
)
from src.graph.nodes import create_nodes


def build_graph(llm: LLMProvider):
    """Construct and compile the full pipeline graph.

    Flow:
        START → route_intent
        route_intent → [generate_code | refine_code | answer_question]
        generate_code → validate_code
        refine_code → validate_code
        validate_code → [verify_requirements | fix_code | prepare_response(force)]
        verify_requirements → [prepare_response | fix_code]
        fix_code → validate_code (loop)
        answer_question → END
        prepare_response → END
    """
    nodes = create_nodes(llm)

    g = StateGraph(PipelineState)

    # Register all nodes
    for name, fn in nodes.items():
        g.add_node(name, fn)

    # ── Edges ────────────────────────────────────────────────────────

    # START → route
    g.add_edge(START, "route_intent")

    # route → generate / refine / answer
    g.add_conditional_edges(
        "route_intent",
        route_by_intent,
        {
            "generate": "generate_code",
            "refine": "refine_code",
            "answer": "answer_question",
        },
    )

    # generate / refine → validate
    g.add_edge("generate_code", "validate_code")
    g.add_edge("refine_code", "validate_code")

    # validate → verify / fix / force_respond
    g.add_conditional_edges(
        "validate_code",
        check_validation,
        {
            "verify": "verify_requirements",
            "fix": "fix_code",
            "force_respond": "prepare_response",
        },
    )

    # verify → respond / fix
    g.add_conditional_edges(
        "verify_requirements",
        check_verification,
        {
            "respond": "prepare_response",
            "fix": "fix_code",
        },
    )

    # fix → back to validate (loop)
    g.add_edge("fix_code", "validate_code")

    # terminals
    g.add_edge("answer_question", END)
    g.add_edge("prepare_response", END)

    return g.compile()
