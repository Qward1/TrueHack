"""Unit tests for the standalone SemanticLogicVerifier agent module."""

import asyncio
import unittest

from src.agents.semantic_logic_verifier import (
    SemanticLogicVerifierAgent,
    _SYSTEM_PROMPT,
    _build_semantic_logic_verifier_prompt,
    _normalize_semantic_logic_verifier_result,
    build_semantic_logic_verifier_input_from_state,
    create_semantic_logic_verifier_node,
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


class TestNormalizeSemanticLogicVerifierResult(unittest.TestCase):
    def test_passed_result_gets_safe_defaults(self) -> None:
        result = _normalize_semantic_logic_verifier_result({"passed": True, "summary": "OK"})
        self.assertTrue(result["passed"])
        self.assertEqual(result["verifier_name"], "SemanticLogicVerifier")
        self.assertIsNone(result["error_family"])
        self.assertIsNone(result["error_code"])
        self.assertEqual(result["severity"], "low")
        self.assertEqual(result["fixer_brief"]["patch_scope"], "none")

    def test_failed_result_keeps_behavior_fields(self) -> None:
        result = _normalize_semantic_logic_verifier_result(
            {
                "passed": False,
                "summary": "Wrong filter.",
                "expected": {"counterexample": {"id": 2}},
                "actual": {},
                "fixer_brief": {"must_change": ["Exclude invalid items."]},
            }
        )
        self.assertFalse(result["passed"])
        self.assertEqual(result["severity"], "high")
        self.assertEqual(result["expected"]["expected_behavior"], "Follow the requested semantic behavior.")
        self.assertEqual(result["actual"]["actual_behavior"], "Wrong filter.")


class TestSemanticLogicVerifierPrompt(unittest.TestCase):
    def test_prompt_contains_paths_and_runtime_sections(self) -> None:
        prompt = _build_semantic_logic_verifier_prompt(
            {
                "task": "Return only users with non-empty email.",
                "code": "return wf.vars.users",
                "source_field_path": "wf.vars.users",
                "output_field_path": "wf.vars.filtered_users",
                "selected_operation": "filter",
                "requested_item_keys": ["email"],
                "runtime_result": [{"id": 1, "email": "a@example.com"}],
                "before_state": {"wf": {"vars": {"users": [{"id": 1, "email": "a@example.com"}]}}},
            }
        )
        self.assertIn("source field path", prompt)
        self.assertIn("output field path", prompt)
        self.assertIn("runtime_result", prompt)
        self.assertIn('"verifier_name": "SemanticLogicVerifier"', prompt)


class TestSemanticLogicVerifierAgent(unittest.TestCase):
    def test_filter_runtime_mismatch_has_counterexample_and_skips_llm(self) -> None:
        llm = StubLLM(response={"passed": True, "summary": "Should not be used."})
        agent = SemanticLogicVerifierAgent(llm)
        result = asyncio.run(
            agent.verify(
                {
                    "task": "Return only users with non-empty email.",
                    "source_field_path": "wf.vars.users",
                    "selected_operation": "filter",
                    "requested_item_keys": ["email"],
                    "before_state": {
                        "wf": {
                            "vars": {
                                "users": [
                                    {"id": 1, "email": "a@example.com"},
                                    {"id": 2, "email": ""},
                                ]
                            }
                        }
                    },
                    "runtime_result": [
                        {"id": 1, "email": "a@example.com"},
                        {"id": 2, "email": ""},
                    ],
                    "code": "return wf.vars.users",
                }
            )
        )
        self.assertFalse(result["passed"])
        self.assertEqual(result["error_code"], "extra_items_included")
        self.assertEqual(llm.call_count, 0)
        self.assertIn("expected_behavior", result["expected"])
        self.assertIn("actual_behavior", result["actual"])
        self.assertEqual(result["expected"]["counterexample"]["id"], 2)

    def test_count_runtime_mismatch_skips_llm(self) -> None:
        llm = StubLLM(response={"passed": True, "summary": "Should not be used."})
        agent = SemanticLogicVerifierAgent(llm)
        result = asyncio.run(
            agent.verify(
                {
                    "task": "Count items in wf.vars.cart.items.",
                    "source_field_path": "wf.vars.cart.items",
                    "selected_operation": "count",
                    "before_state": {
                        "wf": {
                            "vars": {
                                "cart": {
                                    "items": [{"sku": "A"}, {"sku": "B"}]
                                }
                            }
                        }
                    },
                    "runtime_result": {"count": 3},
                    "code": "return { count = 3 }",
                }
            )
        )
        self.assertFalse(result["passed"])
        self.assertEqual(result["error_code"], "wrong_aggregation")
        self.assertEqual(result["expected"]["value"], 2)
        self.assertEqual(result["actual"]["value"], 3.0)
        self.assertEqual(llm.call_count, 0)

    def test_boolean_logic_short_circuits_without_llm(self) -> None:
        llm = StubLLM(response={"passed": True, "summary": "Should not be used."})
        agent = SemanticLogicVerifierAgent(llm)
        result = asyncio.run(
            agent.verify(
                {
                    "task": "Return only users where email is not empty and active is true.",
                    "source_field_path": "wf.vars.users",
                    "selected_operation": "filter",
                    "requested_item_keys": ["email", "active"],
                    "code": (
                        "for _, item in ipairs(wf.vars.users) do\n"
                        "    if item.email ~= \"\" or item.active then\n"
                        "        table.insert(result, item)\n"
                        "    end\n"
                        "end\n"
                    ),
                }
            )
        )
        self.assertFalse(result["passed"])
        self.assertEqual(result["error_code"], "boolean_operator_mismatch")
        self.assertEqual(llm.call_count, 0)

    def test_verify_calls_llm_when_no_evidence_or_local_blocker(self) -> None:
        llm = StubLLM(
            response={
                "passed": False,
                "error_family": "semantic_logic",
                "error_code": "wrong_computed_value",
                "severity": "high",
                "summary": "Wrong computed value.",
                "field_path": "wf.vars.total",
                "evidence": ["LLM reasoned about the code path."],
                "expected": {
                    "expected_behavior": "Return the correct total.",
                    "counterexample": {"price": 10, "qty": 2},
                },
                "actual": {
                    "actual_behavior": "Returned the wrong total.",
                    "counterexample_result": 15,
                },
                "fixer_brief": {
                    "goal": "Fix the semantic logic only.",
                    "must_change": ["Compute the total from price * qty."],
                    "must_preserve": ["Keep workflow contract unchanged."],
                    "forbidden_fixes": ["Do not hardcode the answer."],
                    "suggested_patch": "Recompute the numeric expression correctly.",
                    "patch_scope": "local",
                },
                "confidence": 0.88,
            }
        )
        agent = SemanticLogicVerifierAgent(llm)
        result = asyncio.run(
            agent.verify(
                {
                    "task": "Return the correct total.",
                    "source_field_path": "wf.vars.cart.items",
                    "selected_operation": "llm",
                    "code": "local total = 0\nreturn total",
                }
            )
        )
        self.assertFalse(result["passed"])
        self.assertEqual(result["error_code"], "wrong_computed_value")
        self.assertEqual(llm.call_count, 1)
        self.assertEqual(llm.last_system, _SYSTEM_PROMPT)
        self.assertEqual(llm.last_agent_name, "SemanticLogicVerifier")


class TestAggregateAdapter(unittest.TestCase):
    def test_aggregate_adapter_preserves_compatibility_fields(self) -> None:
        aggregate = to_aggregate_verification_result(
            {
                "verifier_name": "SemanticLogicVerifier",
                "passed": False,
                "error_family": "semantic_logic",
                "error_code": "extra_items_included",
                "severity": "high",
                "summary": "Wrong filter result.",
                "field_path": "wf.vars.users",
                "evidence": ["runtime_result includes invalid item"],
                "expected": {"expected_behavior": "Keep only valid users."},
                "actual": {"actual_behavior": "Included invalid users."},
                "fixer_brief": {
                    "goal": "Fix the semantic logic only.",
                    "must_change": ["Exclude invalid users."],
                    "must_preserve": [],
                    "forbidden_fixes": [],
                    "suggested_patch": "Fix the filter predicate.",
                    "patch_scope": "function_level",
                },
                "confidence": 0.91,
            }
        )
        self.assertFalse(aggregate["passed"])
        self.assertEqual(aggregate["missing_requirements"], ["Exclude invalid users."])
        self.assertEqual(aggregate["verifier_name"], "SemanticLogicVerifier")


class TestStateBridgeAndNode(unittest.TestCase):
    def test_build_input_from_state_uses_current_pipeline_fields(self) -> None:
        payload = build_semantic_logic_verifier_input_from_state(
            {
                "generated_code": "return { count = 3 }",
                "compiled_request": {
                    "verification_prompt": "Count items in wf.vars.cart.items.",
                    "selected_primary_path": "wf.vars.cart.items",
                    "selected_operation": "count",
                    "selected_save_path": "wf.vars.cart_count",
                    "operation_argument": None,
                    "semantic_expectations": ["numeric_aggregation"],
                    "requested_item_keys": ["sku"],
                    "expected_workflow_paths": ["wf.vars.cart.items"],
                    "has_parseable_context": True,
                    "parsed_context": {"wf": {"vars": {"cart": {"items": [{"sku": "A"}, {"sku": "B"}]}}}},
                },
                "diagnostics": {
                    "result_value": {"count": 2},
                    "workflow_state": {"wf": {"vars": {"cart_count": 2}}},
                },
            }
        )
        self.assertEqual(payload["source_field_path"], "wf.vars.cart.items")
        self.assertEqual(payload["output_field_path"], "wf.vars.cart_count")
        self.assertEqual(payload["selected_operation"], "count")
        self.assertEqual(payload["semantic_expectations"], ["numeric_aggregation"])
        self.assertEqual(payload["requested_item_keys"], ["sku"])

    def test_node_bridges_result_to_aggregate_verification(self) -> None:
        llm = StubLLM(
            response={
                "passed": True,
                "summary": "Semantic logic check passed.",
                "severity": "low",
                "fixer_brief": {"patch_scope": "none"},
                "confidence": 0.93,
            }
        )
        node = create_semantic_logic_verifier_node(llm)
        result = asyncio.run(
            node(
                {
                    "generated_code": "return wf.vars.users",
                    "compiled_request": {
                        "verification_prompt": "Return users.",
                        "selected_primary_path": "wf.vars.users",
                    },
                }
            )
        )
        self.assertTrue(result["verification_passed"])
        self.assertEqual(result["verification"]["verifier_name"], "SemanticLogicVerifier")
        self.assertEqual(result["failure_stage"], "")


class TestVerificationChainRegistry(unittest.TestCase):
    def test_registry_contains_first_three_verifiers_in_order(self) -> None:
        specs = get_verification_chain_specs()
        self.assertEqual(specs[0]["verifier_name"], "ContractVerifier")
        self.assertEqual(specs[1]["verifier_name"], "ShapeTypeVerifier")
        self.assertEqual(specs[2]["verifier_name"], "SemanticLogicVerifier")
        self.assertEqual(specs[2]["default_max_fix_iterations"], 1)

    def test_registry_builds_nodes(self) -> None:
        nodes = create_verification_chain_nodes(StubLLM())
        self.assertIn("verify_contract", nodes)
        self.assertIn("verify_shape_type", nodes)
        self.assertIn("verify_semantic_logic", nodes)
        self.assertTrue(callable(nodes["verify_semantic_logic"]))


if __name__ == "__main__":
    unittest.main()
