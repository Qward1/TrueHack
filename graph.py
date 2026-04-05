from langgraph.graph import StateGraph, END
from registry import AgentRegistry
from routers import (
    route_after_execute,
    route_after_generate,
    route_after_repair,
    route_after_test,
)
from state import AgentState, STATUS_FAILED

def make_node(agent):
    def node(state):
        return agent.run(state)
    return node


def failed_node(state: AgentState):
    return {"status": STATUS_FAILED, "final_artifact": state.get("final_artifact", {})}


def build_graph(registry: AgentRegistry):
    builder = StateGraph(AgentState)

    builder.add_node("parse_task", make_node(registry.parse_task))
    builder.add_node("plan_task", make_node(registry.plan_task))
    builder.add_node("generate_code", make_node(registry.generate_code))
    builder.add_node("execute_code", make_node(registry.execute_code))
    builder.add_node("test_code", make_node(registry.test_code))
    builder.add_node("repair_code", make_node(registry.repair_code))
    builder.add_node("finalize_artifact", make_node(registry.finalize_artifact))
    builder.add_node("failed", failed_node)

    builder.set_entry_point("parse_task")

    builder.add_edge("parse_task", "plan_task")
    builder.add_edge("plan_task", "generate_code")
    builder.add_conditional_edges("generate_code", route_after_generate, {
        "to_execute": "execute_code",
        "to_failed": "failed",
    })

    builder.add_conditional_edges("execute_code", route_after_execute, {
        "to_test": "test_code",
        "to_repair": "repair_code",
        "to_failed": "failed",
    })

    builder.add_conditional_edges("test_code", route_after_test, {
        "to_finalize": "finalize_artifact",
        "to_repair": "repair_code",
        "to_failed": "failed",
    })

    builder.add_conditional_edges("repair_code", route_after_repair, {
        "to_execute": "execute_code",
        "to_failed": "failed",
    })
    builder.add_edge("finalize_artifact", END)
    builder.add_edge("failed", END)

    return builder.compile()
