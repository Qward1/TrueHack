"""Registry and orchestration helpers for the modular verification chain."""

from __future__ import annotations

from typing import Any, Awaitable, Callable, TypedDict, cast

from src.agents.contract_verifier import create_contract_verifier_node
from src.agents.robustness_verifier import create_robustness_verifier_node
from src.agents.runtime_state_verifier import create_runtime_state_verifier_node
from src.agents.semantic_logic_verifier import create_semantic_logic_verifier_node
from src.agents.shape_type_verifier import create_shape_type_verifier_node
from src.agents.universal_verification_fixer import create_universal_verification_fixer_node
from src.core.llm import LLMProvider


class VerificationStageSpec(TypedDict):
    index: int
    node_name: str
    verifier_name: str
    result_state_key: str
    failure_stage: str
    default_max_fix_iterations: int


_STAGE_SPECS: tuple[VerificationStageSpec, ...] = (
    {
        "index": 0,
        "node_name": "verify_contract",
        "verifier_name": "ContractVerifier",
        "result_state_key": "contract_verifier_result",
        "failure_stage": "contract_verification",
        "default_max_fix_iterations": 1,
    },
    {
        "index": 1,
        "node_name": "verify_shape_type",
        "verifier_name": "ShapeTypeVerifier",
        "result_state_key": "shape_type_verifier_result",
        "failure_stage": "shape_type_verification",
        "default_max_fix_iterations": 1,
    },
    {
        "index": 2,
        "node_name": "verify_semantic_logic",
        "verifier_name": "SemanticLogicVerifier",
        "result_state_key": "semantic_logic_verifier_result",
        "failure_stage": "semantic_logic_verification",
        "default_max_fix_iterations": 1,
    },
    {
        "index": 3,
        "node_name": "verify_runtime_state",
        "verifier_name": "RuntimeStateVerifier",
        "result_state_key": "runtime_state_verifier_result",
        "failure_stage": "runtime_state_verification",
        "default_max_fix_iterations": 1,
    },
    {
        "index": 4,
        "node_name": "verify_robustness",
        "verifier_name": "RobustnessVerifier",
        "result_state_key": "robustness_verifier_result",
        "failure_stage": "robustness_verification",
        "default_max_fix_iterations": 1,
    },
)

_VERIFIER_NODE_FACTORIES: dict[str, Callable[[LLMProvider], Callable[..., Awaitable[dict[str, Any]]]]] = {
    "verify_contract": create_contract_verifier_node,
    "verify_shape_type": create_shape_type_verifier_node,
    "verify_semantic_logic": create_semantic_logic_verifier_node,
    "verify_runtime_state": create_runtime_state_verifier_node,
    "verify_robustness": create_robustness_verifier_node,
}


def _copy_stage_spec(spec: VerificationStageSpec) -> VerificationStageSpec:
    return dict(spec)


def _normalize_string(value: object) -> str:
    return str(value or "").strip()


def _normalize_positive_int(value: object, *, default: int) -> int:
    try:
        numeric = int(value)
    except (TypeError, ValueError):
        return default
    return numeric if numeric > 0 else default


def _normalize_history_entries(value: object) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    entries: list[dict[str, Any]] = []
    for item in value:
        if isinstance(item, dict):
            entries.append(dict(item))
    return entries


def _normalize_stage_results(value: object) -> dict[str, dict[str, Any]]:
    if not isinstance(value, dict):
        return {}
    normalized: dict[str, dict[str, Any]] = {}
    for key, item in value.items():
        verifier_name = _normalize_string(key)
        if verifier_name and isinstance(item, dict):
            normalized[verifier_name] = dict(item)
    return normalized


def _normalize_stage_fix_counts(value: object) -> dict[str, int]:
    if not isinstance(value, dict):
        return {}
    normalized: dict[str, int] = {}
    for key, item in value.items():
        verifier_name = _normalize_string(key)
        if verifier_name:
            normalized[verifier_name] = _normalize_positive_int(item, default=0) if item else 0
    return normalized


def _normalize_previous_fix_attempts(value: object) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    attempts: list[dict[str, Any]] = []
    for item in value:
        if isinstance(item, dict):
            attempts.append(dict(item))
    return attempts


def get_verification_chain_specs() -> list[VerificationStageSpec]:
    return [_copy_stage_spec(spec) for spec in _STAGE_SPECS]


def get_verification_chain_stage_names() -> list[str]:
    return [spec["verifier_name"] for spec in _STAGE_SPECS]


def get_verification_chain_state_defaults() -> dict[str, Any]:
    return {
        "previous_fix_attempts": [],
        "verification_chain_current_verifier": "",
        "verification_chain_current_node": "",
        "verification_chain_current_index": -1,
        "verification_chain_current_stage_passed": False,
        "verification_chain_current_failure_stage": "",
        "verification_chain_next_verifier": "",
        "verification_chain_next_node": "",
        "verification_chain_last_transition": "",
        "verification_chain_stage_fix_counts": {},
        "verification_chain_stage_fix_limits": {
            spec["verifier_name"]: spec["default_max_fix_iterations"] for spec in _STAGE_SPECS
        },
        "verification_chain_stage_results": {},
        "verification_chain_history": [],
    }


def get_verification_stage_spec(identifier: object) -> VerificationStageSpec | None:
    needle = _normalize_string(identifier)
    if not needle:
        return None
    for spec in _STAGE_SPECS:
        if needle in {
            spec["node_name"],
            spec["verifier_name"],
            spec["result_state_key"],
            spec["failure_stage"],
        }:
            return _copy_stage_spec(spec)
    return None


def get_current_verification_stage_spec(state: dict[str, Any]) -> VerificationStageSpec | None:
    candidates = (
        state.get("verification_chain_current_verifier"),
        state.get("verification_chain_current_node"),
        state.get("failure_stage"),
        state.get("verification", {}).get("verifier_name") if isinstance(state.get("verification"), dict) else "",
    )
    for candidate in candidates:
        spec = get_verification_stage_spec(candidate)
        if spec is not None:
            return spec
    return None


def get_next_verification_stage_spec(identifier: object) -> VerificationStageSpec | None:
    spec = get_verification_stage_spec(identifier)
    if spec is None:
        return None
    next_index = spec["index"] + 1
    if next_index >= len(_STAGE_SPECS):
        return None
    return _copy_stage_spec(_STAGE_SPECS[next_index])


def get_verification_stage_fix_limits(state: dict[str, Any]) -> dict[str, int]:
    defaults = {
        spec["verifier_name"]: spec["default_max_fix_iterations"] for spec in _STAGE_SPECS
    }
    raw = state.get("verification_chain_stage_fix_limits")
    if not isinstance(raw, dict):
        return defaults
    for key, value in raw.items():
        spec = get_verification_stage_spec(key)
        if spec is None:
            continue
        defaults[spec["verifier_name"]] = _normalize_positive_int(
            value,
            default=spec["default_max_fix_iterations"],
        )
    return defaults


def get_verification_stage_fix_count(state: dict[str, Any], verifier_name: object) -> int:
    spec = get_verification_stage_spec(verifier_name)
    if spec is None:
        return 0
    counts = _normalize_stage_fix_counts(state.get("verification_chain_stage_fix_counts"))
    return counts.get(spec["verifier_name"], 0)


def _get_current_stage_passed(state: dict[str, Any], current_spec: VerificationStageSpec) -> bool:
    current_stage_passed = state.get("verification_chain_current_stage_passed")
    if isinstance(current_stage_passed, bool):
        return current_stage_passed

    stage_results = _normalize_stage_results(state.get("verification_chain_stage_results"))
    current_result = stage_results.get(current_spec["verifier_name"])
    if isinstance(current_result, dict):
        return bool(current_result.get("passed", False))

    verification = state.get("verification")
    if isinstance(verification, dict):
        verifier_name = _normalize_string(verification.get("verifier_name"))
        if verifier_name == current_spec["verifier_name"]:
            return bool(verification.get("passed", False))

    return bool(state.get("verification_passed", False))


def route_after_verification_stage_result(state: dict[str, Any]) -> str:
    current_spec = get_current_verification_stage_spec(state)
    if current_spec is None:
        return "complete"
    if not _get_current_stage_passed(state, current_spec):
        return "fix_verification_issue"
    next_spec = get_next_verification_stage_spec(current_spec["verifier_name"])
    return next_spec["node_name"] if next_spec is not None else "complete"


def route_after_verification_fix(state: dict[str, Any]) -> str:
    current_spec = get_current_verification_stage_spec(state)
    if current_spec is None:
        return "complete"
    fixer_result = state.get("universal_verification_fixer_result")
    changed = bool(fixer_result.get("changed", False)) if isinstance(fixer_result, dict) else False
    current_count = get_verification_stage_fix_count(state, current_spec["verifier_name"])
    current_limit = get_verification_stage_fix_limits(state).get(
        current_spec["verifier_name"],
        current_spec["default_max_fix_iterations"],
    )
    if changed and current_count < current_limit:
        return current_spec["node_name"]
    next_spec = get_next_verification_stage_spec(current_spec["verifier_name"])
    return next_spec["node_name"] if next_spec is not None else "complete"


def _build_history_entry(
    *,
    entry_type: str,
    spec: VerificationStageSpec,
    verification: dict[str, Any],
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    entry = {
        "entry_type": entry_type,
        "verifier_name": spec["verifier_name"],
        "node_name": spec["node_name"],
        "failure_stage": spec["failure_stage"],
        "passed": bool(verification.get("passed", False)),
        "error_code": _normalize_string(verification.get("error_code")),
        "field_path": _normalize_string(verification.get("field_path")),
        "summary": _normalize_string(verification.get("summary")),
    }
    if extra:
        entry.update(extra)
    return entry


def _coerce_verifier_output(
    output: dict[str, Any],
    *,
    spec: VerificationStageSpec,
) -> tuple[dict[str, Any], dict[str, Any], bool]:
    coerced_output = dict(output)
    result = coerced_output.get(spec["result_state_key"])
    if not isinstance(result, dict):
        result = {}
    verification = _build_aggregate_verification(result, spec=spec)
    passed = bool(verification.get("passed", False))
    result = dict(result)
    result["verifier_name"] = spec["verifier_name"]
    coerced_output["verification"] = verification
    coerced_output["verification_passed"] = passed
    coerced_output["failure_stage"] = "" if passed else spec["failure_stage"]
    return coerced_output, cast(dict[str, Any], result), passed


def _build_aggregate_verification(
    result: dict[str, Any],
    *,
    spec: VerificationStageSpec,
) -> dict[str, Any]:
    passed = bool(result.get("passed", False))
    verification = {
        "passed": passed,
        "verifier_name": spec["verifier_name"],
        "summary": _normalize_string(result.get("summary")) or ("Verification passed." if passed else "Verification failed."),
        "error_family": result.get("error_family"),
        "error_code": result.get("error_code"),
        "severity": _normalize_string(result.get("severity")) or ("low" if passed else "high"),
        "field_path": result.get("field_path"),
        "evidence": list(result.get("evidence", [])) if isinstance(result.get("evidence"), list) else [],
        "expected": dict(result.get("expected", {})) if isinstance(result.get("expected"), dict) else {},
        "actual": dict(result.get("actual", {})) if isinstance(result.get("actual"), dict) else {},
        "fixer_brief": dict(result.get("fixer_brief", {})) if isinstance(result.get("fixer_brief"), dict) else {},
        "confidence": result.get("confidence", 0.0),
        "warnings": list(result.get("warnings", [])) if isinstance(result.get("warnings"), list) else [],
        "error": bool(result.get("error", False)),
        "missing_requirements": list(result.get("missing_requirements", []))
        if isinstance(result.get("missing_requirements"), list)
        else [],
    }
    if not verification["missing_requirements"] and not passed:
        missing = verification["fixer_brief"].get("must_change") if isinstance(verification["fixer_brief"], dict) else []
        if isinstance(missing, list) and missing:
            verification["missing_requirements"] = [str(item).strip() for item in missing if str(item).strip()]
        else:
            verification["missing_requirements"] = [verification["summary"]]
    elif passed:
        verification["missing_requirements"] = []
    return verification


def _find_first_blocking_stage(
    stage_results: dict[str, dict[str, Any]],
) -> tuple[VerificationStageSpec | None, dict[str, Any]]:
    for stage_spec in _STAGE_SPECS:
        stage_result = stage_results.get(stage_spec["verifier_name"])
        if (
            isinstance(stage_result, dict)
            and not bool(stage_result.get("passed", False))
            and not bool(stage_result.get("resolved_by_fixer", False))
        ):
            return _copy_stage_spec(stage_spec), dict(stage_result)
    return None, {}


def _wrap_verifier_node(
    node_factory: Callable[[LLMProvider], Callable[..., Awaitable[dict[str, Any]]]],
    *,
    llm: LLMProvider,
    spec: VerificationStageSpec,
) -> Callable[..., Awaitable[dict[str, Any]]]:
    node = node_factory(llm)

    async def wrapped(state: dict[str, Any]) -> dict[str, Any]:
        output = await node(state)
        coerced_output, result, passed = _coerce_verifier_output(output, spec=spec)
        next_spec = get_next_verification_stage_spec(spec["verifier_name"])
        stage_results = _normalize_stage_results(state.get("verification_chain_stage_results"))
        stage_results[spec["verifier_name"]] = dict(result)
        history = _normalize_history_entries(state.get("verification_chain_history"))
        history.append(
            _build_history_entry(
                entry_type="verifier",
                spec=spec,
                verification=coerced_output["verification"],
                extra={"confidence": result.get("confidence", 0.0)},
            )
        )
        prior_stage = _normalize_string(state.get("verification_chain_current_verifier"))
        if passed or prior_stage != spec["verifier_name"]:
            previous_fix_attempts: list[dict[str, Any]] = []
        else:
            previous_fix_attempts = _normalize_previous_fix_attempts(state.get("previous_fix_attempts"))

        blocking_spec, blocking_result = _find_first_blocking_stage(stage_results)
        aggregate_verification = dict(coerced_output["verification"])
        aggregate_passed = blocking_spec is None
        if blocking_spec is not None and blocking_spec["verifier_name"] != spec["verifier_name"]:
            aggregate_verification = _build_aggregate_verification(blocking_result, spec=blocking_spec)
        blocking_failure_stage = blocking_spec["failure_stage"] if blocking_spec is not None else ""

        coerced_output.update(
            {
                "verification": aggregate_verification,
                "verification_passed": aggregate_passed,
                "previous_fix_attempts": previous_fix_attempts,
                "verification_chain_current_verifier": spec["verifier_name"],
                "verification_chain_current_node": spec["node_name"],
                "verification_chain_current_index": spec["index"],
                "verification_chain_current_stage_passed": passed,
                "verification_chain_current_failure_stage": blocking_failure_stage,
                "verification_chain_next_verifier": next_spec["verifier_name"] if next_spec is not None else "",
                "verification_chain_next_node": next_spec["node_name"] if next_spec is not None else "",
                "verification_chain_last_transition": "verifier_pass" if passed else "verifier_fail",
                "verification_chain_stage_fix_counts": _normalize_stage_fix_counts(
                    state.get("verification_chain_stage_fix_counts")
                ),
                "verification_chain_stage_fix_limits": get_verification_stage_fix_limits(state),
                "verification_chain_stage_results": stage_results,
                "verification_chain_history": history,
                "failure_stage": blocking_failure_stage,
            }
        )
        return coerced_output

    return wrapped


def create_verification_chain_nodes(llm: LLMProvider) -> dict[str, Any]:
    nodes: dict[str, Any] = {}
    for spec in _STAGE_SPECS:
        node_factory = _VERIFIER_NODE_FACTORIES[spec["node_name"]]
        nodes[spec["node_name"]] = _wrap_verifier_node(node_factory, llm=llm, spec=spec)
    nodes["fix_verification_issue"] = create_universal_verification_fixer_node(llm)
    return nodes
