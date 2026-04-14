import asyncio
import os
import unittest
from unittest.mock import patch

from src.agents.planner import create_planner_node
from src.graph.nodes import create_nodes


class NoRouteLlmCallsStub:
    def __init__(self) -> None:
        self.generate_json_calls = 0

    async def generate_json(
        self,
        prompt: str,
        system: str = "",
        temperature: float = 0.0,
        *,
        agent_name: str = "",
    ) -> dict:
        self.generate_json_calls += 1
        raise AssertionError(f"route_intent should not call the LLM here: {system[:80]}")

    async def generate(
        self,
        prompt: str,
        system: str = "",
        temperature: float = 0.2,
        max_tokens: int | None = None,
        *,
        agent_name: str = "",
    ) -> str:
        raise AssertionError("generate should not be called")

    async def chat(
        self,
        messages: list[dict],
        temperature: float = 0.2,
        max_tokens: int | None = None,
        *,
        agent_name: str = "",
    ) -> str:
        raise AssertionError("chat should not be called")


class PlannerNodeStubLLM:
    def __init__(self, response: dict) -> None:
        self.response = response
        self.call_count = 0
        self.last_prompt = ""

    async def generate_json(
        self,
        prompt: str,
        system: str = "",
        temperature: float = 0.0,
        *,
        agent_name: str = "",
    ) -> dict:
        self.call_count += 1
        self.last_prompt = prompt
        return self.response

    async def generate(
        self,
        prompt: str,
        system: str = "",
        temperature: float = 0.2,
        max_tokens: int | None = None,
        *,
        agent_name: str = "",
    ) -> str:
        raise AssertionError("generate should not be called")

    async def chat(
        self,
        messages: list[dict],
        temperature: float = 0.2,
        max_tokens: int | None = None,
        *,
        agent_name: str = "",
    ) -> str:
        raise AssertionError("chat should not be called")


class FollowupRoutingTests(unittest.TestCase):
    def test_active_code_clarification_answer_routes_to_change_without_llm(self) -> None:
        llm = NoRouteLlmCallsStub()
        nodes = create_nodes(llm)
        state = {
            "user_input": "только VIP-контакты",
            "current_code": "return wf.vars.contacts",
            "active_clarifying_questions": ["Нужно вернуть все контакты или только VIP?"],
            "planner_pending_questions": [],
            "base_prompt": "Верни контакты из wf.vars.contacts",
            "target_path": "",
        }

        result = asyncio.run(nodes["route_intent"](state))

        self.assertEqual(result["intent"], "change")
        self.assertEqual(llm.generate_json_calls, 0)

    def test_planner_followup_without_followup_action_stays_on_code_path(self) -> None:
        llm = PlannerNodeStubLLM(
            {
                "reformulated_task": "Return wf.vars.last",
                "identified_workflow_paths": ["wf.vars.last"],
                "needs_clarification": False,
                "clarification_questions": [],
                "confidence": 0.9,
            }
        )

        with patch.dict(os.environ, {"PLANNER_ENABLED": "true"}):
            node = create_planner_node(llm)
            state = {
                "user_input": "последний из wf.vars.last",
                "intent": "",
                "current_code": "",
                "compiled_request": {},
                "awaiting_planner_clarification": True,
                "planner_original_input": "создай скрипт неясной задачи",
                "planner_pending_questions": ["What exactly?"],
                "planner_clarification_attempts": 1,
                "active_clarifying_questions": [],
            }

            result = asyncio.run(node(state))

        self.assertEqual(result["intent"], "create")
        self.assertFalse(result["awaiting_planner_clarification"])
        self.assertIn("Исходная задача:", result["user_input"])
        self.assertIn("Ответ пользователя:", result["user_input"])
        self.assertEqual(llm.call_count, 1)


if __name__ == "__main__":
    unittest.main()
