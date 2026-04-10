"""Build and compile the LangGraph agent pipeline."""

from __future__ import annotations

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
        START -> resolve_target
        resolve_target -> route_intent
        route_intent -> [generate_code | refine_code | answer_question]
        generate_code -> validate_code
        refine_code -> validate_code
        validate_code -> [verify_requirements | fix_code | prepare_response(force)]
        verify_requirements -> [save_code | fix_code | prepare_response(force)]
        fix_code -> validate_code (loop)
        save_code -> explain_solution -> prepare_response
        answer_question -> END
        prepare_response -> END
    """
    nodes = create_nodes(llm)

    graph = StateGraph(PipelineState)
    for name, fn in nodes.items():
        graph.add_node(name, fn)

    graph.add_edge(START, "resolve_target")
    graph.add_edge("resolve_target", "route_intent")

    graph.add_conditional_edges(
        "route_intent",
        route_by_intent,
        {
            "generate": "generate_code",
            "refine": "refine_code",
            "answer": "answer_question",
        },
    )

    graph.add_edge("generate_code", "validate_code")
    graph.add_edge("refine_code", "validate_code")

    graph.add_conditional_edges(
        "validate_code",
        check_validation,
        {
            "verify": "verify_requirements",
            "fix": "fix_code",
            "force_respond": "prepare_response",
        },
    )

    graph.add_conditional_edges(
        "verify_requirements",
        check_verification,
        {
            "save": "save_code",
            "fix": "fix_code",
            "force_respond": "prepare_response",
        },
    )

    graph.add_edge("fix_code", "validate_code")
    graph.add_edge("save_code", "explain_solution")
    graph.add_edge("explain_solution", "prepare_response")

    graph.add_edge("answer_question", END)
    graph.add_edge("prepare_response", END)

    return graph.compile()
