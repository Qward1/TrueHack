"""Integration tests for the standalone verifier/fixer chain compatibility."""

import asyncio
import unittest
from typing import Any

from src.agents.verification_chain import (
    create_verification_chain_nodes,
    get_verification_chain_specs,
    get_verification_chain_state_defaults,
    get_verification_stage_spec,
    route_after_verification_fix,
    route_after_verification_stage_result,
)


class AgentAwareStubLLM:
    """LLM stub that can return different JSON payloads per agent."""

    def __init__(self, responses_by_agent: dict[str, object] | None = None) -> None:
        self._responses_by_agent: dict[str, list[object]] = {}
        for agent_name, response in (responses_by_agent or {}).items():
            if isinstance(response, list):
                self._responses_by_agent[agent_name] = list(response)
            else:
                self._responses_by_agent[agent_name] = [response]
        self.call_count_by_agent: dict[str, int] = {}
        self.last_prompt_by_agent: dict[str, str] = {}

    async def generate_json(
        self,
        prompt: str,
        system: str = "",
        temperature: float = 0.0,
        *,
        agent_name: str = "",
    ) -> object:
        self.last_prompt_by_agent[agent_name] = prompt
        count = self.call_count_by_agent.get(agent_name, 0)
        self.call_count_by_agent[agent_name] = count + 1
        responses = self._responses_by_agent.get(agent_name, [{}])
        index = min(count, len(responses) - 1)
        return responses[index]


def _base_state() -> dict[str, Any]:
    state = {
        "user_input": "Return recall time.",
        "generated_code": "return wf.vars.time",
        "current_code": "",
        "validation_passed": True,
        "verification_passed": False,
        "fix_verification_iterations": 0,
        "failure_stage": "",
        "verification": {},
        "compiled_request": {
            "verification_prompt": "Return wf.initVariables.recallTime.",
            "selected_primary_path": "wf.initVariables.recallTime",
            "expected_workflow_paths": ["wf.initVariables.recallTime"],
            "has_parseable_context": True,
            "parsed_context": {
                "wf": {
                    "initVariables": {"recallTime": "2026-04-13T10:20:30"},
                    "vars": {"time": "wrong"},
                }
            },
            "planner_result": {"expected_result_action": "return"},
        },
        "diagnostics": {
            "result_preview": "",
            "workflow_state": {
                "wf": {
                    "initVariables": {"recallTime": "2026-04-13T10:20:30"},
                    "vars": {"time": "wrong"},
                }
            },
        },
    }
    state.update(get_verification_chain_state_defaults())
    return state


class TestVerificationChainIntegration(unittest.TestCase):
    def test_verifier_fail_then_fixer_patch_then_next_verifier_uses_new_code(self) -> None:
        llm = AgentAwareStubLLM(
            {
                "ContractVerifier": {
                    "passed": False,
                    "error_family": "workflow_path",
                    "error_code": "wrong_read_path",
                    "severity": "high",
                    "summary": "Reads wf.vars.time instead of wf.initVariables.recallTime.",
                    "field_path": "wf.vars.time",
                    "evidence": ["Code reads wf.vars.time."],
                    "expected": {"read_path": "wf.initVariables.recallTime"},
                    "actual": {"read_path": "wf.vars.time"},
                    "fixer_brief": {
                        "goal": "Use the correct workflow path.",
                        "must_change": ["Replace wf.vars.time with wf.initVariables.recallTime."],
                        "must_preserve": ["Keep the existing return logic."],
                        "forbidden_fixes": ["Do not rewrite the whole script."],
                        "suggested_patch": "Return wf.initVariables.recallTime.",
                        "patch_scope": "local",
                    },
                    "confidence": 0.92,
                },
                "UniversalVerificationFixer": {
                    "fixed": True,
                    "changed": True,
                    "applied_error_family": "workflow_path",
                    "applied_error_code": "wrong_read_path",
                    "applied_strategy": "replace_wrong_path",
                    "preserved_constraints": ["Keep the existing return logic."],
                    "remaining_risks": [],
                    "fixed_lua_code": "lua{return wf.initVariables.recallTime}lua",
                },
                "ShapeTypeVerifier": {
                    "passed": True,
                    "summary": "Shape/type check passed.",
                    "severity": "low",
                    "fixer_brief": {"patch_scope": "none"},
                    "confidence": 0.97,
                },
            }
        )
        nodes = create_verification_chain_nodes(llm)
        state0 = _base_state()

        verifier_output = asyncio.run(nodes["verify_contract"](state0))
        self.assertFalse(verifier_output["verification_passed"])
        self.assertEqual(verifier_output["verification"]["verifier_name"], "ContractVerifier")
        self.assertEqual(verifier_output["verification_chain_current_verifier"], "ContractVerifier")
        self.assertEqual(verifier_output["verification_chain_next_verifier"], "ShapeTypeVerifier")
        self.assertEqual(verifier_output["previous_fix_attempts"], [])
        self.assertEqual(route_after_verification_stage_result({**state0, **verifier_output}), "fix_verification_issue")

        state1 = {**state0, **verifier_output}
        fixer_output = asyncio.run(nodes["fix_verification_issue"](state1))
        self.assertEqual(fixer_output["generated_code"], "return wf.initVariables.recallTime")
        self.assertFalse(fixer_output["verification_passed"])
        self.assertEqual(fixer_output["failure_stage"], "contract_verification")
        self.assertEqual(fixer_output["previous_fix_attempts"][-1]["strategy"], "replace_wrong_path")
        self.assertEqual(fixer_output["verification_chain_stage_fix_counts"]["ContractVerifier"], 1)
        self.assertEqual(fixer_output["verification_chain_last_transition"], "fixer_changed_code")

        state2 = {**state1, **fixer_output}
        self.assertEqual(route_after_verification_fix(state2), "verify_shape_type")

        next_verifier_output = asyncio.run(nodes["verify_shape_type"](state2))
        self.assertTrue(next_verifier_output["verification_passed"])
        self.assertEqual(next_verifier_output["verification_chain_current_verifier"], "ShapeTypeVerifier")
        self.assertEqual(next_verifier_output["previous_fix_attempts"], [])
        self.assertIn("wf.initVariables.recallTime", llm.last_prompt_by_agent["ShapeTypeVerifier"])
        self.assertEqual(route_after_verification_stage_result({**state2, **next_verifier_output}), "verify_semantic_logic")

    def test_noop_fixer_keeps_failure_context_and_attempt_history(self) -> None:
        llm = AgentAwareStubLLM(
            {
                "ContractVerifier": {
                    "passed": False,
                    "summary": "Wrong workflow path.",
                    "error_family": "workflow_path",
                    "error_code": "wrong_read_path",
                    "fixer_brief": {
                        "must_change": ["Replace wf.vars.time with wf.initVariables.recallTime."],
                        "patch_scope": "local",
                    },
                },
                "UniversalVerificationFixer": [
                    {
                        "fixed": True,
                        "changed": True,
                        "applied_strategy": "first_try_same",
                        "fixed_lua_code": "lua{return wf.vars.time}lua",
                    },
                    {
                        "fixed": True,
                        "changed": True,
                        "applied_strategy": "still_same",
                        "fixed_lua_code": "lua{return wf.vars.time}lua",
                    },
                ],
            }
        )
        nodes = create_verification_chain_nodes(llm)
        state0 = _base_state()
        verifier_output = asyncio.run(nodes["verify_contract"](state0))
        state1 = {**state0, **verifier_output}
        fixer_output = asyncio.run(nodes["fix_verification_issue"](state1))

        self.assertEqual(fixer_output["generated_code"], "return wf.vars.time")
        self.assertFalse(fixer_output["universal_verification_fixer_result"]["changed"])
        self.assertFalse(fixer_output["verification_passed"])
        self.assertEqual(fixer_output["failure_stage"], "contract_verification")
        self.assertEqual(fixer_output["verification_chain_stage_fix_counts"]["ContractVerifier"], 1)
        self.assertEqual(fixer_output["previous_fix_attempts"][-1]["changed"], False)
        self.assertEqual(route_after_verification_fix({**state1, **fixer_output}), "verify_shape_type")

    def test_route_after_fix_repeats_same_stage_when_stage_limit_allows_it(self) -> None:
        spec = get_verification_stage_spec("ContractVerifier")
        self.assertIsNotNone(spec)
        route = route_after_verification_fix(
            {
                "verification_chain_current_verifier": "ContractVerifier",
                "verification_chain_current_node": "verify_contract",
                "verification_chain_stage_fix_counts": {"ContractVerifier": 1},
                "verification_chain_stage_fix_limits": {"ContractVerifier": 2},
                "universal_verification_fixer_result": {"changed": True},
            }
        )
        self.assertEqual(route, "verify_contract")

        route_after_limit = route_after_verification_fix(
            {
                "verification_chain_current_verifier": "ContractVerifier",
                "verification_chain_current_node": "verify_contract",
                "verification_chain_stage_fix_counts": {"ContractVerifier": 2},
                "verification_chain_stage_fix_limits": {"ContractVerifier": 2},
                "universal_verification_fixer_result": {"changed": True},
            }
        )
        self.assertEqual(route_after_limit, "verify_shape_type")

    def test_registry_defaults_match_active_runtime_chain(self) -> None:
        specs = get_verification_chain_specs()
        self.assertEqual([spec["verifier_name"] for spec in specs], [
            "ContractVerifier",
            "ShapeTypeVerifier",
            "SemanticLogicVerifier",
            "RuntimeStateVerifier",
            "RobustnessVerifier",
        ])
        defaults = get_verification_chain_state_defaults()
        self.assertEqual(defaults["verification_chain_current_index"], -1)
        self.assertEqual(defaults["verification_chain_history"], [])
        self.assertEqual(defaults["verification_chain_stage_fix_limits"]["ContractVerifier"], 1)


if __name__ == "__main__":
    unittest.main()
