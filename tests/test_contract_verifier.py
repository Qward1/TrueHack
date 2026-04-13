"""Unit tests for the standalone ContractVerifier agent module."""

import asyncio
import unittest

from src.agents.contract_verifier import (
    ContractVerifierAgent,
    _SYSTEM_PROMPT,
    _build_contract_verifier_prompt,
    _normalize_contract_verifier_result,
    build_contract_verifier_input_from_state,
    create_contract_verifier_node,
    to_aggregate_verification_result,
)


class StubLLM:
    """Minimal LLM stub that returns canned JSON responses."""

    def __init__(self, response: object = None):
        self._response = response if response is not None else {}
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
        return self._response


class TestNormalizeContractVerifierResult(unittest.TestCase):
    def test_passed_result_gets_safe_defaults(self) -> None:
        result = _normalize_contract_verifier_result({"passed": True, "summary": "OK"})
        self.assertTrue(result["passed"])
        self.assertEqual(result["verifier_name"], "ContractVerifier")
        self.assertIsNone(result["error_family"])
        self.assertIsNone(result["error_code"])
        self.assertEqual(result["severity"], "low")
        self.assertEqual(result["fixer_brief"]["patch_scope"], "none")

    def test_failed_result_defaults_to_high_severity(self) -> None:
        result = _normalize_contract_verifier_result(
            {
                "passed": False,
                "summary": "Wrong path.",
                "fixer_brief": {"must_change": ["Use wf.initVariables.recallTime."]},
            }
        )
        self.assertFalse(result["passed"])
        self.assertEqual(result["severity"], "high")
        self.assertEqual(result["fixer_brief"]["patch_scope"], "local")
        self.assertEqual(result["fixer_brief"]["must_change"], ["Use wf.initVariables.recallTime."])


class TestContractVerifierPrompt(unittest.TestCase):
    def test_prompt_contains_runtime_and_after_state(self) -> None:
        prompt = _build_contract_verifier_prompt(
            {
                "task": "Use wf.initVariables.recallTime and return unix time.",
                "code": "return wf.vars.time",
                "expected_workflow_paths": ["wf.initVariables.recallTime"],
                "expected_result_action": "return",
                "expected_return_path": "wf.initVariables.recallTime",
                "expected_top_level_type": "scalar",
                "runtime_result": "2026-04-13T10:20:30",
                "after_state": {"wf": {"vars": {"time": "2026-04-13T10:20:30"}}},
            }
        )
        self.assertIn("runtime_result", prompt)
        self.assertIn("after_state", prompt)
        self.assertIn("wf.initVariables.recallTime", prompt)
        self.assertIn('"verifier_name": "ContractVerifier"', prompt)


class TestContractVerifierAgent(unittest.TestCase):
    def test_evidence_first_pass_skips_llm_when_runtime_matches_expected_return_path(self) -> None:
        llm = StubLLM(response={"passed": False, "summary": "Should never be used."})
        agent = ContractVerifierAgent(llm)
        result = asyncio.run(
            agent.verify(
                {
                    "task": "Return wf.initVariables.recallTime.",
                    "code": "return wf.initVariables.recallTime",
                    "expected_result_action": "return",
                    "expected_return_path": "wf.initVariables.recallTime",
                    "expected_top_level_type": "scalar",
                    "before_state": {"wf": {"initVariables": {"recallTime": "2026-04-13T10:20:30"}}},
                    "runtime_result": "2026-04-13T10:20:30",
                }
            )
        )
        self.assertTrue(result["passed"])
        self.assertEqual(llm.call_count, 0)
        self.assertIn("runtime evidence", result["summary"].lower())

    def test_evidence_first_fail_skips_llm_when_wrong_path_was_updated(self) -> None:
        llm = StubLLM(response={"passed": True, "summary": "Should never be used."})
        agent = ContractVerifierAgent(llm)
        result = asyncio.run(
            agent.verify(
                {
                    "task": "Save the count to wf.vars.count.",
                    "code": "wf.vars.debug = 3\nreturn wf.vars.debug",
                    "expected_result_action": "save_to_wf_vars",
                    "expected_update_path": "wf.vars.count",
                    "before_state": {"wf": {"vars": {"count": 1, "debug": 0}}},
                    "after_state": {"wf": {"vars": {"count": 1, "debug": 3}}},
                }
            )
        )
        self.assertFalse(result["passed"])
        self.assertEqual(result["error_code"], "wrong_update_path")
        self.assertEqual(result["field_path"], "wf.vars.debug")
        self.assertEqual(llm.call_count, 0)

    def test_evidence_first_fail_skips_llm_when_runtime_shape_is_wrong(self) -> None:
        llm = StubLLM(response={"passed": True, "summary": "Should never be used."})
        agent = ContractVerifierAgent(llm)
        result = asyncio.run(
            agent.verify(
                {
                    "task": "Return a scalar count.",
                    "code": "return {1, 2, 3}",
                    "expected_result_action": "return",
                    "expected_top_level_type": "scalar",
                    "runtime_result": [1, 2, 3],
                }
            )
        )
        self.assertFalse(result["passed"])
        self.assertEqual(result["error_code"], "wrong_top_level_type")
        self.assertEqual(llm.call_count, 0)

    def test_forbidden_io_short_circuits_without_llm(self) -> None:
        llm = StubLLM(
            response={
                "passed": True,
                "summary": "Should never be used.",
            }
        )
        agent = ContractVerifierAgent(llm)
        result = asyncio.run(
            agent.verify(
                {
                    "task": "Return wf.vars.value",
                    "code": "local value = wf.vars.value\nprint(value)\nreturn value",
                }
            )
        )
        self.assertFalse(result["passed"])
        self.assertEqual(result["error_code"], "forbidden_io")
        self.assertEqual(llm.call_count, 0)
        self.assertIn("print", result["summary"])

    def test_demo_data_short_circuits_without_llm(self) -> None:
        llm = StubLLM(response={"passed": True, "summary": "Should never be used."})
        agent = ContractVerifierAgent(llm)
        result = asyncio.run(
            agent.verify(
                {
                    "task": "Return the last email from workflow data.",
                    "code": 'local emails = {"a@example.com", "b@example.com"}\nreturn emails[#emails]',
                }
            )
        )
        self.assertFalse(result["passed"])
        self.assertEqual(result["error_code"], "demo_data")
        self.assertEqual(llm.call_count, 0)

    def test_verify_calls_llm_and_normalizes_structured_output(self) -> None:
        llm = StubLLM(
            response={
                "verifier_name": "SomethingElse",
                "passed": False,
                "error_family": "workflow_path",
                "error_code": "wrong_read_path",
                "severity": "high",
                "summary": "Reads wf.vars.time instead of wf.initVariables.recallTime.",
                "field_path": "wf.vars.time",
                "evidence": [
                    "Code reads wf.vars.time.",
                    "Task expects wf.initVariables.recallTime.",
                ],
                "expected": {"read_path": "wf.initVariables.recallTime"},
                "actual": {"read_path": "wf.vars.time"},
                "fixer_brief": {
                    "goal": "Use the correct workflow path.",
                    "must_change": ["Replace wf.vars.time with wf.initVariables.recallTime."],
                    "must_preserve": ["Keep the conversion logic."],
                    "forbidden_fixes": ["Do not rewrite the business logic."],
                    "suggested_patch": "Read from wf.initVariables.recallTime and keep the rest unchanged.",
                    "patch_scope": "local",
                },
                "confidence": 0.88,
            }
        )
        agent = ContractVerifierAgent(llm)
        result = asyncio.run(
            agent.verify(
                {
                    "task": "Use wf.initVariables.recallTime and return unix time.",
                    "code": "return wf.vars.time",
                    "expected_workflow_paths": ["wf.initVariables.recallTime"],
                    "expected_result_action": "return",
                    "expected_return_path": "wf.initVariables.recallTime",
                    "runtime_result": "runtime-preview",
                    "after_state": {"wf": {"vars": {"time": "1681374030"}}},
                }
            )
        )
        self.assertFalse(result["passed"])
        self.assertEqual(result["verifier_name"], "ContractVerifier")
        self.assertEqual(result["error_code"], "wrong_read_path")
        self.assertEqual(result["field_path"], "wf.vars.time")
        self.assertEqual(result["fixer_brief"]["patch_scope"], "local")
        self.assertEqual(llm.call_count, 1)
        self.assertEqual(llm.last_system, _SYSTEM_PROMPT)
        self.assertEqual(llm.last_agent_name, "ContractVerifier")
        self.assertIn("runtime_result", llm.last_prompt)
        self.assertIn("after_state", llm.last_prompt)


class TestAggregateAdapter(unittest.TestCase):
    def test_aggregate_adapter_preserves_compatibility_fields(self) -> None:
        aggregate = to_aggregate_verification_result(
            {
                "verifier_name": "ContractVerifier",
                "passed": False,
                "error_family": "workflow_path",
                "error_code": "wrong_read_path",
                "severity": "high",
                "summary": "Reads the wrong workflow path.",
                "field_path": "wf.vars.time",
                "evidence": ["Reads wf.vars.time."],
                "expected": {"read_path": "wf.initVariables.recallTime"},
                "actual": {"read_path": "wf.vars.time"},
                "fixer_brief": {
                    "goal": "Use the correct workflow path.",
                    "must_change": ["Replace wf.vars.time with wf.initVariables.recallTime."],
                    "must_preserve": [],
                    "forbidden_fixes": [],
                    "suggested_patch": "Read from wf.initVariables.recallTime.",
                    "patch_scope": "local",
                },
                "confidence": 0.92,
            }
        )
        self.assertFalse(aggregate["passed"])
        self.assertEqual(aggregate["summary"], "Reads the wrong workflow path.")
        self.assertEqual(
            aggregate["missing_requirements"],
            ["Replace wf.vars.time with wf.initVariables.recallTime."],
        )
        self.assertEqual(aggregate["verifier_name"], "ContractVerifier")
        self.assertEqual(aggregate["error"], False)


class TestStateBridgeAndNode(unittest.TestCase):
    def test_build_input_from_state_uses_current_pipeline_fields(self) -> None:
        payload = build_contract_verifier_input_from_state(
            {
                "user_input": "fallback text",
                "generated_code": "wf.vars.iso_date = value\nreturn wf.vars.iso_date",
                "compiled_request": {
                    "verification_prompt": "Convert DATUM/TIME and save to wf.vars.iso_date.",
                    "selected_primary_path": "wf.vars.iso_date",
                    "expected_workflow_paths": ["wf.initVariables.json.IDOC.ZCDF_HEAD.DATUM"],
                    "has_parseable_context": True,
                    "parsed_context": {"wf": {"initVariables": {"json": {}}}},
                    "planner_result": {
                        "expected_result_action": "save_to_wf_vars",
                        "identified_workflow_paths": ["wf.vars.iso_date"],
                    },
                },
                "diagnostics": {
                    "result_preview": "",
                    "workflow_state": {"wf": {"vars": {"iso_date": "2026-04-13"}}},
                },
            }
        )
        self.assertEqual(payload["task"], "Convert DATUM/TIME and save to wf.vars.iso_date.")
        self.assertEqual(payload["expected_result_action"], "save_to_wf_vars")
        self.assertEqual(payload["expected_update_path"], "wf.vars.iso_date")
        self.assertIn("wf.initVariables.json.IDOC.ZCDF_HEAD.DATUM", payload["expected_workflow_paths"])
        self.assertIsNotNone(payload["before_state"])
        self.assertIsNotNone(payload["after_state"])

    def test_node_bridges_result_to_aggregate_verification(self) -> None:
        llm = StubLLM(
            response={
                "passed": True,
                "summary": "Contract check passed.",
                "severity": "low",
                "fixer_brief": {"patch_scope": "none"},
                "confidence": 0.94,
            }
        )
        node = create_contract_verifier_node(llm)
        result = asyncio.run(
            node(
                {
                    "generated_code": "return wf.vars.emails[#wf.vars.emails]",
                    "compiled_request": {
                        "verification_prompt": "Return the last email from wf.vars.emails.",
                        "selected_primary_path": "wf.vars.emails",
                        "expected_workflow_paths": ["wf.vars.emails"],
                        "planner_result": {"expected_result_action": "return"},
                    },
                    "diagnostics": {
                        "result_preview": "user3@example.com",
                    },
                }
            )
        )
        self.assertTrue(result["verification_passed"])
        self.assertIn("contract_verifier_result", result)
        self.assertEqual(result["verification"]["verifier_name"], "ContractVerifier")
        self.assertEqual(result["failure_stage"], "")


if __name__ == "__main__":
    unittest.main()
