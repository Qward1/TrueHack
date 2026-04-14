"""Unit tests for the standalone UniversalVerificationFixer agent module."""

import asyncio
import unittest

from src.agents.universal_verification_fixer import (
    UniversalVerificationFixerAgent,
    _SYSTEM_PROMPT,
    _build_universal_verification_fixer_prompt,
    _normalize_universal_verification_fixer_result,
    build_universal_verification_fixer_input_from_state,
    create_universal_verification_fixer_node,
)
from src.agents.verification_chain import create_verification_chain_nodes


class StubLLM:
    """Minimal LLM stub that returns canned JSON responses."""

    def __init__(self, responses: object = None):
        if isinstance(responses, list):
            self._responses = list(responses)
        else:
            self._responses = [responses if responses is not None else {}]
        self.call_count = 0
        self.last_prompt = ""
        self.last_system = ""
        self.last_agent_name = ""

    async def generate_json(
        self,
        prompt: str,
        system: str = "",
        temperature: float = 0.0,
        *,
        agent_name: str = "",
    ) -> object:
        self.call_count += 1
        self.last_prompt = prompt
        self.last_system = system
        self.last_agent_name = agent_name
        index = min(self.call_count - 1, len(self._responses) - 1)
        return self._responses[index]


class TestNormalizeUniversalVerificationFixerResult(unittest.TestCase):
    def test_changed_code_is_wrapped_and_marked_changed(self) -> None:
        result = _normalize_universal_verification_fixer_result(
            {
                "fixed": True,
                "changed": True,
                "applied_error_family": "contract",
                "applied_error_code": "wrong_read_path",
                "applied_strategy": "replace_wrong_path",
                "preserved_constraints": ["Keep conversion logic."],
                "remaining_risks": [],
                "fixed_lua_body": "return wf.initVariables.recallTime",
            },
            original_code="return wf.vars.time",
            verifier_result={
                "verifier_name": "ContractVerifier",
                "passed": False,
                "error_family": "contract",
                "error_code": "wrong_read_path",
                "severity": "high",
                "summary": "Wrong workflow path.",
                "field_path": "wf.vars.time",
                "evidence": [],
                "expected": {},
                "actual": {},
                "fixer_brief": {
                    "goal": "Use the correct path.",
                    "must_change": ["Replace wf.vars.time."],
                    "must_preserve": ["Keep conversion logic."],
                    "forbidden_fixes": [],
                    "suggested_patch": "Read wf.initVariables.recallTime.",
                    "patch_scope": "local",
                },
                "confidence": 0.9,
            },
        )
        self.assertTrue(result["fixed"])
        self.assertTrue(result["changed"])
        self.assertTrue(result["fixed_lua_code"].startswith("lua{"))
        self.assertTrue(result["fixed_lua_code"].endswith("}lua"))

    def test_unchanged_code_for_failed_verifier_forces_changed_false(self) -> None:
        result = _normalize_universal_verification_fixer_result(
            {
                "fixed": True,
                "changed": True,
                "fixed_lua_code": "lua{return wf.vars.time}lua",
            },
            original_code="return wf.vars.time",
            verifier_result={
                "verifier_name": "ContractVerifier",
                "passed": False,
                "error_family": "contract",
                "error_code": "wrong_read_path",
                "severity": "high",
                "summary": "Wrong workflow path.",
                "field_path": "wf.vars.time",
                "evidence": [],
                "expected": {},
                "actual": {},
                "fixer_brief": {
                    "goal": "Use the correct path.",
                    "must_change": ["Replace wf.vars.time."],
                    "must_preserve": [],
                    "forbidden_fixes": [],
                    "suggested_patch": "",
                    "patch_scope": "local",
                },
                "confidence": 0.9,
            },
        )
        self.assertFalse(result["fixed"])
        self.assertFalse(result["changed"])


class TestUniversalVerificationFixerPrompt(unittest.TestCase):
    def test_prompt_contains_verifier_constraints_whitelists_and_focused_evidence(self) -> None:
        prompt = _build_universal_verification_fixer_prompt(
            {
                "task": "Convert recall time.",
                "code": "return wf.vars.time",
                "workflow_context": {"wf": {"initVariables": {"recallTime": "2026-04-13T10:20:30"}}},
                "before_state": {"wf": {"initVariables": {"recallTime": "2026-04-13T10:20:30"}}},
                "after_state": {"wf": {"vars": {"time": "1681374030"}}},
                "runtime_result": "1681374030",
                "verifier_result": {
                    "verifier_name": "ContractVerifier",
                    "passed": False,
                    "error_family": "contract",
                    "error_code": "wrong_read_path",
                    "severity": "high",
                    "summary": "Wrong workflow path.",
                    "field_path": "wf.vars.time",
                    "fixer_brief": {
                        "goal": "Use the correct path.",
                        "must_change": ["Replace wf.vars.time with wf.initVariables.recallTime."],
                        "must_preserve": ["Keep unix conversion logic."],
                        "forbidden_fixes": ["Do not return wf.vars unchanged."],
                        "suggested_patch": "Read the correct path and keep the rest.",
                        "patch_scope": "local",
                    },
                },
                "previous_fix_attempts": [{"strategy": "unchanged", "changed": False}],
            }
        )
        self.assertIn("Verifier diagnosis:", prompt)
        self.assertIn("must_change", prompt)
        self.assertIn("must_preserve", prompt)
        self.assertIn("forbidden_fixes", prompt)
        self.assertIn("allowed_workflow_paths", prompt)
        self.assertIn("available_code_variables", prompt)
        self.assertIn("after_state value at wf.vars.time", prompt)
        self.assertIn("previous_fix_attempts", prompt)
        self.assertIn("fixed_lua_body", prompt)
        self.assertNotIn("Workflow context:\n", prompt)


class TestUniversalVerificationFixerAgent(unittest.TestCase):
    def test_passed_verifier_skips_llm_and_keeps_code(self) -> None:
        llm = StubLLM({"fixed": False})
        agent = UniversalVerificationFixerAgent(llm)
        result = asyncio.run(
            agent.fix(
                {
                    "task": "Return users.",
                    "code": "return wf.vars.users",
                    "verifier_result": {
                        "verifier_name": "ContractVerifier",
                        "passed": True,
                        "summary": "Contract check passed.",
                    },
                }
            )
        )
        self.assertTrue(result["fixed"])
        self.assertFalse(result["changed"])
        self.assertEqual(llm.call_count, 0)
        self.assertIn("return wf.vars.users", result["fixed_lua_code"])

    def test_fix_calls_llm_and_marks_changed_true(self) -> None:
        llm = StubLLM(
            {
                "fixed": True,
                "changed": True,
                "applied_error_family": "contract",
                "applied_error_code": "wrong_read_path",
                "applied_strategy": "replace_wrong_path",
                "preserved_constraints": ["Keep unix conversion logic."],
                "remaining_risks": [],
                "fixed_lua_body": "return wf.initVariables.recallTime",
            }
        )
        agent = UniversalVerificationFixerAgent(llm)
        result = asyncio.run(
            agent.fix(
                {
                    "task": "Return recall time.",
                    "code": "return wf.vars.time",
                    "verifier_result": {
                        "verifier_name": "ContractVerifier",
                        "passed": False,
                        "error_family": "contract",
                        "error_code": "wrong_read_path",
                        "severity": "high",
                        "summary": "Wrong workflow path.",
                        "field_path": "wf.vars.time",
                        "fixer_brief": {
                            "goal": "Use the correct path.",
                            "must_change": ["Replace wf.vars.time with wf.initVariables.recallTime."],
                            "must_preserve": ["Keep unix conversion logic."],
                            "forbidden_fixes": ["Do not rewrite the whole script."],
                            "suggested_patch": "Read the correct path and keep the rest.",
                            "patch_scope": "local",
                        },
                    },
                }
            )
        )
        self.assertTrue(result["fixed"])
        self.assertTrue(result["changed"])
        self.assertEqual(result["applied_error_code"], "wrong_read_path")
        self.assertEqual(llm.call_count, 1)
        self.assertEqual(llm.last_system, _SYSTEM_PROMPT)
        self.assertEqual(llm.last_agent_name, "UniversalVerificationFixer")

    def test_retry_when_first_fix_is_unchanged(self) -> None:
        llm = StubLLM(
            [
                {
                    "fixed": True,
                    "changed": True,
                    "applied_error_family": "contract",
                    "applied_error_code": "wrong_read_path",
                    "applied_strategy": "first_try_same",
                    "fixed_lua_body": "return wf.vars.time",
                },
                {
                    "fixed": True,
                    "changed": True,
                    "applied_error_family": "contract",
                    "applied_error_code": "wrong_read_path",
                    "applied_strategy": "second_try_patch",
                    "fixed_lua_body": "return wf.initVariables.recallTime",
                },
            ]
        )
        agent = UniversalVerificationFixerAgent(llm)
        result = asyncio.run(
            agent.fix(
                {
                    "task": "Return recall time.",
                    "code": "return wf.vars.time",
                    "verifier_result": {
                        "verifier_name": "ContractVerifier",
                        "passed": False,
                        "error_family": "contract",
                        "error_code": "wrong_read_path",
                        "summary": "Wrong workflow path.",
                        "fixer_brief": {
                            "must_change": ["Replace wf.vars.time."],
                            "patch_scope": "local",
                        },
                    },
                }
            )
        )
        self.assertTrue(result["changed"])
        self.assertEqual(result["applied_strategy"], "second_try_patch")
        self.assertEqual(llm.call_count, 2)

    def test_failed_verifier_returns_changed_false_when_no_real_patch(self) -> None:
        llm = StubLLM(
            [
                {
                    "fixed": True,
                    "changed": True,
                    "applied_strategy": "first_try_same",
                    "fixed_lua_body": "return wf.vars.time",
                },
                {
                    "fixed": True,
                    "changed": True,
                    "applied_strategy": "still_same",
                    "fixed_lua_body": "return wf.vars.time",
                },
            ]
        )
        agent = UniversalVerificationFixerAgent(llm)
        result = asyncio.run(
            agent.fix(
                {
                    "task": "Return recall time.",
                    "code": "return wf.vars.time",
                    "verifier_result": {
                        "verifier_name": "ContractVerifier",
                        "passed": False,
                        "error_family": "contract",
                        "error_code": "wrong_read_path",
                        "summary": "Wrong workflow path.",
                        "fixer_brief": {
                            "must_change": ["Replace wf.vars.time."],
                            "patch_scope": "local",
                        },
                    },
                }
            )
        )
        self.assertFalse(result["fixed"])
        self.assertFalse(result["changed"])
        self.assertEqual(llm.call_count, 2)


class TestStateBridgeAndNode(unittest.TestCase):
    def test_build_input_from_state_uses_current_pipeline_fields(self) -> None:
        payload = build_universal_verification_fixer_input_from_state(
            {
                "generated_code": "return wf.vars.time",
                "compiled_request": {
                    "verification_prompt": "Convert recall time.",
                    "workflow_path_inventory": [{"path": "wf.initVariables.recallTime"}],
                    "parsed_context": {"wf": {"initVariables": {"recallTime": "2026-04-13T10:20:30"}}},
                    "has_parseable_context": True,
                },
                "diagnostics": {
                    "result_value": "1681374030",
                    "workflow_state": {"vars": {"time": "1681374030"}, "initVariables": []},
                },
                "verification": {
                    "verifier_name": "ContractVerifier",
                    "passed": False,
                    "error_code": "wrong_read_path",
                    "summary": "Wrong workflow path.",
                    "fixer_brief": {"must_change": ["Use wf.initVariables.recallTime."]},
                },
                "previous_fix_attempts": [{"strategy": "unchanged", "changed": False}],
            }
        )
        self.assertEqual(payload["task"], "Convert recall time.")
        self.assertEqual(payload["runtime_result"], "1681374030")
        self.assertEqual(payload["verifier_result"]["verifier_name"], "ContractVerifier")
        self.assertEqual(payload["previous_fix_attempts"], [{"strategy": "unchanged", "changed": False}])
        self.assertIn("wf.initVariables.recallTime", payload["allowed_workflow_paths"])
        self.assertIn("wf.vars.time", payload["allowed_workflow_paths"])
        self.assertEqual(payload["available_code_variables"], [])
        self.assertIn("wf", payload["after_state"])
        self.assertTrue(payload["available_runtime_evidence"]["workflow_context"])

    def test_node_bridges_result_to_generated_code_and_attempts(self) -> None:
        llm = StubLLM(
            {
                "fixed": True,
                "changed": True,
                "applied_error_family": "contract",
                "applied_error_code": "wrong_read_path",
                "applied_strategy": "replace_wrong_path",
                "preserved_constraints": ["Keep unix conversion logic."],
                "remaining_risks": [],
                "fixed_lua_body": "return wf.initVariables.recallTime",
            }
        )
        node = create_universal_verification_fixer_node(llm)
        result = asyncio.run(
            node(
                {
                    "generated_code": "return wf.vars.time",
                    "fix_verification_iterations": 0,
                    "validation_passed": True,
                    "failure_stage": "contract_verification",
                    "compiled_request": {
                        "verification_prompt": "Convert recall time.",
                    },
                    "verification_chain_current_verifier": "ContractVerifier",
                    "verification_chain_current_node": "verify_contract",
                    "verification_chain_current_index": 0,
                    "verification_chain_current_failure_stage": "contract_verification",
                    "verification_chain_next_verifier": "ShapeTypeVerifier",
                    "verification_chain_next_node": "verify_shape_type",
                    "verification_chain_stage_fix_limits": {"ContractVerifier": 1},
                    "verification": {
                        "verifier_name": "ContractVerifier",
                        "passed": False,
                        "error_family": "contract",
                        "error_code": "wrong_read_path",
                        "summary": "Wrong workflow path.",
                        "fixer_brief": {
                            "must_change": ["Use wf.initVariables.recallTime."],
                            "patch_scope": "local",
                        },
                    },
                }
            )
        )
        self.assertEqual(result["generated_code"], "return wf.initVariables.recallTime")
        self.assertEqual(result["fix_verification_iterations"], 1)
        self.assertFalse(result["verification_passed"])
        self.assertEqual(result["failure_stage"], "contract_verification")
        self.assertEqual(result["previous_fix_attempts"][-1]["strategy"], "replace_wrong_path")
        self.assertEqual(result["verification_chain_stage_fix_counts"]["ContractVerifier"], 1)
        self.assertEqual(result["verification_chain_last_transition"], "fixer_changed_code")

    def test_node_keeps_passed_verification_state_on_noop(self) -> None:
        llm = StubLLM({})
        node = create_universal_verification_fixer_node(llm)
        result = asyncio.run(
            node(
                {
                    "generated_code": "return wf.vars.users",
                    "fix_verification_iterations": 3,
                    "verification": {
                        "verifier_name": "RobustnessVerifier",
                        "passed": True,
                        "summary": "Robustness check passed.",
                    },
                }
            )
        )
        self.assertEqual(result["generated_code"], "return wf.vars.users")
        self.assertEqual(result["fix_verification_iterations"], 3)
        self.assertTrue(result["verification_passed"])
        self.assertFalse(result["universal_verification_fixer_result"]["changed"])
        self.assertEqual(result["failure_stage"], "")

    def test_node_handles_partial_verifier_result_without_crashing(self) -> None:
        llm = StubLLM(
            {
                "fixed": True,
                "changed": True,
                "applied_strategy": "minimal_patch_from_verifier_brief",
                "fixed_lua_body": "return wf.vars.users or {}",
            }
        )
        node = create_universal_verification_fixer_node(llm)
        result = asyncio.run(
            node(
                {
                    "generated_code": "return wf.vars.users",
                    "verification": {
                        "verifier_name": "RobustnessVerifier",
                        "passed": False,
                        "summary": "Missing nil guard.",
                    },
                }
            )
        )
        self.assertTrue(result["universal_verification_fixer_result"]["changed"])
        self.assertEqual(result["previous_fix_attempts"][-1]["verifier_name"], "RobustnessVerifier")
        self.assertEqual(result["verification_chain_stage_fix_counts"]["RobustnessVerifier"], 1)

    def test_normalizer_accepts_body_only_response(self) -> None:
        result = _normalize_universal_verification_fixer_result(
            {
                "fixed": True,
                "changed": True,
                "fixed_lua_body": "return wf.initVariables.recallTime",
            },
            original_code="return wf.vars.time",
            verifier_result={
                "verifier_name": "ContractVerifier",
                "passed": False,
                "error_family": "contract",
                "error_code": "wrong_read_path",
                "severity": "high",
                "summary": "Wrong workflow path.",
                "field_path": "wf.vars.time",
                "evidence": [],
                "expected": {},
                "actual": {},
                "fixer_brief": {
                    "goal": "Use the correct path.",
                    "must_change": ["Replace wf.vars.time."],
                    "must_preserve": [],
                    "forbidden_fixes": [],
                    "suggested_patch": "",
                    "patch_scope": "local",
                },
                "confidence": 0.9,
            },
        )
        self.assertTrue(result["changed"])
        self.assertIn("wf.initVariables.recallTime", result["fixed_lua_code"])


class TestVerificationChainRegistry(unittest.TestCase):
    def test_registry_builds_shared_fix_node(self) -> None:
        nodes = create_verification_chain_nodes(StubLLM())
        self.assertIn("fix_verification_issue", nodes)
        self.assertTrue(callable(nodes["fix_verification_issue"]))


if __name__ == "__main__":
    unittest.main()
