from typing import Any, Dict, List
from typing_extensions import TypedDict


STATUS_NEW_TASK = "NEW_TASK"
STATUS_PARSED = "PARSED"
STATUS_PLANNED = "PLANNED"
STATUS_CODE_GENERATED = "CODE_GENERATED"
STATUS_EXECUTED = "EXECUTED"
STATUS_TESTED = "TESTED"
STATUS_REPAIR_NEEDED = "REPAIR_NEEDED"
STATUS_FINALIZED = "FINALIZED"
STATUS_FAILED = "FAILED"


class AgentState(TypedDict, total=False):
    user_prompt: str
    parsed_spec: Dict[str, Any]
    parsing_notes: List[str]
    implementation_plan: Dict[str, Any]
    validation_plan: Dict[str, Any]
    test_plan_outline: Dict[str, Any]
    planning_notes: List[str]
    current_code: str
    code_unit_plan: List[Dict[str, Any]]
    code_units: List[Dict[str, Any]]
    code_unit_map: List[Dict[str, Any]]
    code_versions: List[Dict[str, Any]]
    generation_notes: List[str]
    execution_result: Dict[str, Any]
    lint_result: Dict[str, Any]
    test_result: Dict[str, Any]
    repair_history: List[Dict[str, Any]]
    repair_notes: List[str]
    final_artifact: Dict[str, Any]
    final_notes: List[str]
    status: str

    execution_ok: bool
    tests_passed: bool

    execution_attempts: int
    repair_attempts: int
    test_attempts: int
    max_attempts: int

DEFAULT_MAX_ATTEMPTS = 20


def ensure_state_defaults(state: AgentState) -> Dict[str, Any]:
    return {
        "user_prompt": state.get("user_prompt", ""),
        "parsed_spec": dict(state.get("parsed_spec", {})),
        "parsing_notes": list(state.get("parsing_notes", [])),
        "implementation_plan": dict(state.get("implementation_plan", {})),
        "validation_plan": dict(state.get("validation_plan", {})),
        "test_plan_outline": dict(state.get("test_plan_outline", {})),
        "planning_notes": list(state.get("planning_notes", [])),
        "current_code": state.get("current_code", ""),
        "code_unit_plan": list(state.get("code_unit_plan", [])),
        "code_units": list(state.get("code_units", [])),
        "code_unit_map": list(state.get("code_unit_map", [])),
        "code_versions": list(state.get("code_versions", [])),
        "generation_notes": list(state.get("generation_notes", [])),
        "execution_result": dict(state.get("execution_result", {})),
        "lint_result": dict(state.get("lint_result", {})),
        "test_result": dict(state.get("test_result", {})),
        "repair_history": list(state.get("repair_history", [])),
        "repair_notes": list(state.get("repair_notes", [])),
        "final_artifact": dict(state.get("final_artifact", {})),
        "final_notes": list(state.get("final_notes", [])),
        "execution_ok": state.get("execution_ok", False),
        "tests_passed": state.get("tests_passed", False),
        "execution_attempts": state.get("execution_attempts", 0),
        "repair_attempts": state.get("repair_attempts", 0),
        "test_attempts": state.get("test_attempts", 0),
        "max_attempts": state.get("max_attempts", DEFAULT_MAX_ATTEMPTS),
        "status": state.get("status", STATUS_NEW_TASK),
    }


def build_failure_result(state: AgentState, message: str) -> Dict[str, Any]:
    defaults = ensure_state_defaults(state)
    final_artifact = dict(defaults["final_artifact"])
    final_artifact["failure_reason"] = message
    return {
        **defaults,
        "execution_ok": False,
        "tests_passed": False,
        "status": STATUS_FAILED,
        "final_artifact": final_artifact,
    }
