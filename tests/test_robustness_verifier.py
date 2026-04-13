"""Unit tests for the standalone RobustnessVerifier agent module."""

import asyncio
import unittest

from src.agents.robustness_verifier import (
    RobustnessVerifierAgent,
    _SYSTEM_PROMPT,
    _build_robustness_verifier_prompt,
    _normalize_robustness_verifier_result,
    build_robustness_verifier_input_from_state,
    create_robustness_verifier_node,
    to_aggregate_verification_result,
)
from src.agents.verification_chain import create_verification_chain_nodes, get_verification_chain_specs


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


class TestNormalizeRobustnessVerifierResult(unittest.TestCase):
    def test_passed_result_gets_safe_defaults(self) -> None:
        result = _normalize_robustness_verifier_result({"passed": True, "summary": "OK"})
        self.assertTrue(result["passed"])
        self.assertEqual(result["verifier_name"], "RobustnessVerifier")
        self.assertIsNone(result["error_family"])
        self.assertIsNone(result["error_code"])
        self.assertEqual(result["severity"], "low")
        self.assertEqual(result["fixer_brief"]["patch_scope"], "none")

    def test_failed_result_defaults_to_high_severity(self) -> None:
        result = _normalize_robustness_verifier_result(
            {
                "passed": False,
                "summary": "Unsafe edge case.",
                "fixer_brief": {"must_change": ["Add a guard."]},
            }
        )
        self.assertFalse(result["passed"])
        self.assertEqual(result["severity"], "high")


class TestRobustnessVerifierPrompt(unittest.TestCase):
    def test_prompt_contains_runtime_error_and_source_context(self) -> None:
        prompt = _build_robustness_verifier_prompt(
            {
                "task": "Return prefix from wf.vars.phone.",
                "code": "return string.sub(wf.vars.phone, 1, 3)",
                "source_field_path": "wf.vars.phone",
                "before_state": {"wf": {"vars": {"phone": "123"}}},
                "run_error": "attempt to index a nil value",
            }
        )
        self.assertIn("run_error", prompt)
        self.assertIn("Resolved source value at wf.vars.phone", prompt)
        self.assertIn('"verifier_name": "RobustnessVerifier"', prompt)


class TestRobustnessVerifierAgent(unittest.TestCase):
    def test_runtime_nil_error_skips_llm(self) -> None:
        llm = StubLLM(response={"passed": True, "summary": "Should not be used."})
        agent = RobustnessVerifierAgent(llm)
        result = asyncio.run(
            agent.verify(
                {
                    "task": "Return phone prefix.",
                    "source_field_path": "wf.vars.phone",
                    "before_state": {"wf": {"vars": {}}},
                    "run_error": "attempt to index a nil value",
                    "failure_kind": "runtime",
                    "code": "return string.sub(wf.vars.phone, 1, 3)",
                }
            )
        )
        self.assertFalse(result["passed"])
        self.assertEqual(result["error_code"], "missing_field_runtime_error")
        self.assertEqual(result["field_path"], "wf.vars.phone")
        self.assertEqual(llm.call_count, 0)

    def test_concrete_runtime_success_passes_without_llm(self) -> None:
        llm = StubLLM(response={"passed": False, "summary": "Should not be used."})
        agent = RobustnessVerifierAgent(llm)
        result = asyncio.run(
            agent.verify(
                {
                    "task": "Count items.",
                    "source_field_path": "wf.vars.items",
                    "before_state": {"wf": {"vars": {"items": []}}},
                    "runtime_result": {"count": 0},
                    "code": "for _, item in ipairs(wf.vars.items) do end\nreturn { count = 0 }",
                }
            )
        )
        self.assertTrue(result["passed"])
        self.assertEqual(llm.call_count, 0)
        self.assertIn("Concrete execution evidence", result["summary"])

    def test_unsafe_ipairs_without_concrete_evidence_skips_llm(self) -> None:
        llm = StubLLM(response={"passed": True, "summary": "Should not be used."})
        agent = RobustnessVerifierAgent(llm)
        result = asyncio.run(
            agent.verify(
                {
                    "task": "Return items.",
                    "source_field_path": "wf.vars.items",
                    "code": "for _, item in ipairs(wf.vars.items) do\n    table.insert(result, item)\nend",
                }
            )
        )
        self.assertFalse(result["passed"])
        self.assertEqual(result["error_code"], "unsafe_ipairs")
        self.assertEqual(llm.call_count, 0)

    def test_short_string_unhandled_skips_llm(self) -> None:
        llm = StubLLM(response={"passed": True, "summary": "Should not be used."})
        agent = RobustnessVerifierAgent(llm)
        result = asyncio.run(
            agent.verify(
                {
                    "task": "Return code prefix.",
                    "source_field_path": "wf.vars.code",
                    "before_state": {"wf": {"vars": {"code": "12"}}},
                    "code": "return string.sub(wf.vars.code, 1, 5)",
                }
            )
        )
        self.assertFalse(result["passed"])
        self.assertEqual(result["error_code"], "short_string_unhandled")
        self.assertEqual(llm.call_count, 0)

    def test_unsafe_tonumber_skips_llm(self) -> None:
        llm = StubLLM(response={"passed": True, "summary": "Should not be used."})
        agent = RobustnessVerifierAgent(llm)
        result = asyncio.run(
            agent.verify(
                {
                    "task": "Increment count.",
                    "source_field_path": "wf.vars.count",
                    "before_state": {"wf": {"vars": {}}},
                    "code": "local n = tonumber(wf.vars.count)\nreturn n + 1",
                }
            )
        )
        self.assertFalse(result["passed"])
        self.assertEqual(result["error_code"], "unsafe_tonumber")
        self.assertEqual(llm.call_count, 0)

    def test_verify_calls_llm_when_no_concrete_evidence_or_heuristic(self) -> None:
        llm = StubLLM(
            response={
                "passed": False,
                "error_family": "robustness",
                "error_code": "fragile_input_handling",
                "severity": "high",
                "summary": "The code is brittle for partial input.",
                "field_path": "wf.vars.profile",
                "evidence": ["LLM found a fragile input assumption."],
                "expected": {"edge_case": "profile is partial", "expected_behavior": "Handle partial profile safely."},
                "actual": {"behavior": "Assumes all nested fields exist."},
                "fixer_brief": {
                    "goal": "Make the code safe for missing or empty input.",
                    "must_change": ["Guard nested profile fields."],
                    "must_preserve": ["Keep valid input behavior unchanged."],
                    "forbidden_fixes": ["Do not hardcode a perfect sample."],
                    "suggested_patch": "Add nil-safe nested field guards.",
                    "patch_scope": "function_level",
                },
                "confidence": 0.84,
            }
        )
        agent = RobustnessVerifierAgent(llm)
        result = asyncio.run(
            agent.verify(
                {
                    "task": "Normalize profile.",
                    "source_field_path": "wf.vars.profile",
                    "before_state": {"wf": {"vars": {"profile": {"name": "Ivan"}}}},
                    "code": "local profile = wf.vars.profile\nreturn profile",
                }
            )
        )
        self.assertFalse(result["passed"])
        self.assertEqual(result["error_code"], "fragile_input_handling")
        self.assertEqual(llm.call_count, 1)
        self.assertEqual(llm.last_system, _SYSTEM_PROMPT)
        self.assertEqual(llm.last_agent_name, "RobustnessVerifier")


class TestAggregateAdapter(unittest.TestCase):
    def test_aggregate_adapter_preserves_compatibility_fields(self) -> None:
        aggregate = to_aggregate_verification_result(
            {
                "verifier_name": "RobustnessVerifier",
                "passed": False,
                "error_family": "robustness",
                "error_code": "unsafe_ipairs",
                "severity": "high",
                "summary": "Unsafe ipairs.",
                "field_path": "wf.vars.items",
                "evidence": ["detected ipairs without guard"],
                "expected": {"expected_behavior": "Guard the collection."},
                "actual": {"behavior": "Assumes the collection exists."},
                "fixer_brief": {
                    "goal": "Make the code safe for missing or empty input.",
                    "must_change": ["Guard wf.vars.items before iterating."],
                    "must_preserve": [],
                    "forbidden_fixes": [],
                    "suggested_patch": "Default items to {}.",
                    "patch_scope": "local",
                },
                "confidence": 0.93,
            }
        )
        self.assertFalse(aggregate["passed"])
        self.assertEqual(aggregate["missing_requirements"], ["Guard wf.vars.items before iterating."])
        self.assertEqual(aggregate["verifier_name"], "RobustnessVerifier")


class TestStateBridgeAndNode(unittest.TestCase):
    def test_build_input_from_state_uses_current_pipeline_fields(self) -> None:
        payload = build_robustness_verifier_input_from_state(
            {
                "generated_code": "return string.sub(wf.vars.phone, 1, 3)",
                "compiled_request": {
                    "verification_prompt": "Return phone prefix.",
                    "selected_primary_path": "wf.vars.phone",
                    "selected_operation": "return",
                    "selected_save_path": "",
                    "semantic_expectations": [],
                    "expected_workflow_paths": ["wf.vars.phone"],
                    "has_parseable_context": True,
                    "parsed_context": {"wf": {"vars": {"phone": "123"}}},
                },
                "diagnostics": {
                    "run_output": "",
                    "run_error": "attempt to index a nil value",
                    "failure_kind": "runtime",
                    "result_value": None,
                    "workflow_state": None,
                },
            }
        )
        self.assertEqual(payload["source_field_path"], "wf.vars.phone")
        self.assertEqual(payload["selected_operation"], "return")
        self.assertEqual(payload["run_error"], "attempt to index a nil value")
        self.assertEqual(payload["failure_kind"], "runtime")

    def test_node_bridges_result_to_aggregate_verification(self) -> None:
        llm = StubLLM(
            response={
                "passed": True,
                "summary": "Robustness check passed.",
                "severity": "low",
                "fixer_brief": {"patch_scope": "none"},
                "confidence": 0.91,
            }
        )
        node = create_robustness_verifier_node(llm)
        result = asyncio.run(
            node(
                {
                    "generated_code": "return wf.vars.users",
                    "compiled_request": {
                        "verification_prompt": "Return users.",
                        "selected_primary_path": "wf.vars.users",
                    },
                    "diagnostics": {
                        "result_value": [{"id": 1}],
                    },
                }
            )
        )
        self.assertTrue(result["verification_passed"])
        self.assertEqual(result["verification"]["verifier_name"], "RobustnessVerifier")
        self.assertEqual(result["failure_stage"], "")


class TestVerificationChainRegistry(unittest.TestCase):
    def test_registry_contains_first_five_verifiers_in_order(self) -> None:
        specs = get_verification_chain_specs()
        self.assertEqual(specs[0]["verifier_name"], "ContractVerifier")
        self.assertEqual(specs[1]["verifier_name"], "ShapeTypeVerifier")
        self.assertEqual(specs[2]["verifier_name"], "SemanticLogicVerifier")
        self.assertEqual(specs[3]["verifier_name"], "RuntimeStateVerifier")
        self.assertEqual(specs[4]["verifier_name"], "RobustnessVerifier")
        self.assertEqual(specs[4]["default_max_fix_iterations"], 1)

    def test_registry_builds_nodes(self) -> None:
        nodes = create_verification_chain_nodes(StubLLM())
        self.assertIn("verify_contract", nodes)
        self.assertIn("verify_shape_type", nodes)
        self.assertIn("verify_semantic_logic", nodes)
        self.assertIn("verify_runtime_state", nodes)
        self.assertIn("verify_robustness", nodes)
        self.assertTrue(callable(nodes["verify_robustness"]))


if __name__ == "__main__":
    unittest.main()
