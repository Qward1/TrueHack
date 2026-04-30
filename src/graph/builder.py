"""Build and compile the LangGraph agent pipeline."""

from __future__ import annotations

from langgraph.graph import END, START, StateGraph

from src.agents.planner import create_planner_node
from src.core.llm import LLMProvider
from src.core.state import PipelineState
from src.graph.conditions import (
    check_validation,
    check_verification,
    route_after_planning,
    route_after_preparation,
    route_by_intent,
    route_from_start,
)
from src.graph.nodes import create_nodes


def build_graph(llm: LLMProvider):
    """Construct and compile the full pipeline graph.

    Flow:
        START -> resolve_target
        resolve_target -> route_intent
        route_intent -> [prepare_generation_context | answer_question]
        prepare_generation_context -> [generate_code | refine_code | prepare_response(clarify)]
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
    nodes["plan_request"] = create_planner_node(llm)

    graph = StateGraph(PipelineState)
    for name, fn in nodes.items():
        graph.add_node(name, fn)

    graph.add_conditional_edges(
        START,
        route_from_start,
        {
            "normal": "resolve_target",
            "planner_followup": "plan_request",
        },
    )
    graph.add_edge("resolve_target", "route_intent")

    graph.add_conditional_edges(
        "route_intent",
        route_by_intent,
        {
            "prepare": "plan_request",
            "answer": "answer_question",
        },
    )

    graph.add_conditional_edges(
        "plan_request",
        route_after_planning,
        {
            "continue": "prepare_generation_context",
            "clarify": "prepare_response",
        },
    )

    graph.add_conditional_edges(
        "prepare_generation_context",
        route_after_preparation,
        {
            "generate": "generate_code",
            "refine": "refine_code",
            "clarify": "prepare_response",
        },
    )

    graph.add_edge("generate_code", "validate_code")
    graph.add_edge("refine_code", "validate_code")

    graph.add_conditional_edges(
        "validate_code",
        check_validation,
        {
            "verify": "verify_requirements",
            "fix_validation": "fix_validation_code",
        },
    )

    graph.add_conditional_edges(
        "verify_requirements",
        check_verification,
        {
            "save": "save_code",
            "fix_verification": "fix_verification_code",
        },
    )

    graph.add_edge("fix_validation_code", "validate_code")
    graph.add_edge("fix_verification_code", "verify_requirements")
    graph.add_edge("save_code", "explain_solution")
    graph.add_edge("explain_solution", "prepare_response")

    graph.add_edge("answer_question", END)
    graph.add_edge("prepare_response", END)

    return graph.compile()
