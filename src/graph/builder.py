"""Build and compile the LangGraph agent pipeline."""

from __future__ import annotations

from langgraph.graph import END, START, StateGraph

from src.agents.planner import create_planner_node
from src.agents.verification_chain import (
    create_verification_chain_nodes,
    get_verification_chain_specs,
    route_after_verification_fix,
    route_after_verification_stage_result,
)
from src.core.llm import LLMProvider
from src.core.state import PipelineState
from src.graph.conditions import (
    check_validation,
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
        validate_code -> [verify_contract | fix_validation_code | save_code(skip)]
        fix_validation_code -> validate_code (loop)
        verify_contract -> ... -> verify_robustness -> save_code
        verifier_fail -> fix_verification_issue -> [same_verifier | next_verifier]
        save_code -> explain_solution -> prepare_response
        answer_question -> END
        prepare_response -> END
    """
    nodes = create_nodes(llm)
    nodes["plan_request"] = create_planner_node(llm)
    nodes.update(create_verification_chain_nodes(llm))
    verification_specs = get_verification_chain_specs()

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
            "verify": "verify_contract",
            "fix_validation": "fix_validation_code",
            "save": "save_code",
        },
    )

    verification_route_targets = {
        "fix_verification_issue": "fix_verification_issue",
        "complete": "save_code",
    }
    verification_route_targets.update(
        {spec["node_name"]: spec["node_name"] for spec in verification_specs}
    )
    for spec in verification_specs:
        graph.add_conditional_edges(
            spec["node_name"],
            route_after_verification_stage_result,
            verification_route_targets,
        )

    fix_route_targets = {"complete": "save_code"}
    fix_route_targets.update({spec["node_name"]: spec["node_name"] for spec in verification_specs})
    graph.add_conditional_edges(
        "fix_verification_issue",
        route_after_verification_fix,
        fix_route_targets,
    )

    graph.add_edge("fix_validation_code", "validate_code")
    graph.add_edge("save_code", "explain_solution")
    graph.add_edge("explain_solution", "prepare_response")

    graph.add_edge("answer_question", END)
    graph.add_edge("prepare_response", END)

    return graph.compile()
