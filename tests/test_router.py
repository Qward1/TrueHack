"""Tests for RouterAgent using FakeLLMProvider (no LM Studio needed)."""

from __future__ import annotations

import pytest

from tests.conftest import FakeLLMProvider
from src.agents.router import RouterAgent
from src.core.state import AgentState


def _state(user_input: str, **kwargs) -> AgentState:
    base: AgentState = {
        "chat_id": "test",
        "messages": [],
        "user_input": user_input,
        "intent": "",
        "plan": [],
        "current_task_index": 0,
        "task_description": "",
        "generated_code": "",
        "generated_codes": {},
        "assembled_code": "",
        "fix_iterations": 0,
        "validation_passed": False,
        "validation_errors": "",
        "rag_context": "",
        "response": "",
        "response_type": "text",
        "metadata": {},
    }
    base.update(kwargs)
    return base


class TestRouterAgent:
    @pytest.mark.asyncio
    async def test_generate_clear_intent(self, settings):
        llm = FakeLLMProvider(structured_response={"intent": "generate_clear", "confidence": 0.95})
        agent = RouterAgent(llm=llm, settings=settings)
        result = await agent.run(_state("write a function to sort a table"))
        assert result["intent"] == "generate_clear"

    @pytest.mark.asyncio
    async def test_low_confidence_forces_unclear(self, settings):
        llm = FakeLLMProvider(structured_response={"intent": "generate_clear", "confidence": 0.3})
        agent = RouterAgent(llm=llm, settings=settings)
        result = await agent.run(_state("do something"))
        assert result["intent"] == "generate_unclear"

    @pytest.mark.asyncio
    async def test_question_intent(self, settings):
        llm = FakeLLMProvider(structured_response={"intent": "question", "confidence": 0.9})
        agent = RouterAgent(llm=llm, settings=settings)
        result = await agent.run(_state("what is a coroutine?"))
        assert result["intent"] == "question"

    @pytest.mark.asyncio
    async def test_fix_error_intent(self, settings):
        llm = FakeLLMProvider(structured_response={"intent": "fix_error", "confidence": 0.88})
        agent = RouterAgent(llm=llm, settings=settings)
        result = await agent.run(_state("it gives attempt to call a nil value"))
        assert result["intent"] == "fix_error"

    @pytest.mark.asyncio
    async def test_fallback_on_bad_json(self, settings):
        """When LLM returns unparseable JSON, fallback dict is used → generate_unclear."""
        from src.core.llm import LLMProvider

        class BrokenLLM(LLMProvider):
            async def generate(self, *a, **kw): return "not json"
            async def generate_structured(self, *a, fallback=None, **kw):
                return fallback or {}

        agent = RouterAgent(llm=BrokenLLM(), settings=settings)
        result = await agent.run(_state("hello"))
        # fallback dict has confidence=0.0 → intent forced to generate_unclear
        assert result["intent"] == "generate_unclear"

    @pytest.mark.asyncio
    async def test_has_previous_code_in_prompt(self, settings):
        """Router should receive has_previous_code=true when assembled_code is set."""
        captured = {}

        class CaptureLLM(FakeLLMProvider):
            async def generate_structured(self, prompt, system, schema, fallback=None):
                captured["prompt"] = prompt
                return {"intent": "refine", "confidence": 0.9}

        agent = RouterAgent(llm=CaptureLLM(), settings=settings)
        state = _state("make it faster", assembled_code='print("hi")')
        await agent.run(state)
        assert "true" in captured["prompt"].lower() or "true" in captured.get("prompt", "")


class TestPlannerAgent:
    @pytest.mark.asyncio
    async def test_plan_creates_tasks(self, settings):
        from src.agents.planner import PlannerAgent
        llm = FakeLLMProvider(structured_response={
            "tasks": [{"id": "t1", "description": "sort", "function_name": "sort_table",
                       "signature": "sort_table(t)", "dependencies": []}]
        })
        agent = PlannerAgent(llm=llm, settings=settings)
        result = await agent.plan(_state("sort a table"))
        assert len(result["plan"]) == 1
        assert result["plan"][0]["id"] == "t1"
        assert result["current_task_index"] == 0

    @pytest.mark.asyncio
    async def test_clarify_returns_questions(self, settings):
        from src.agents.planner import PlannerAgent
        llm = FakeLLMProvider(structured_response={"questions": ["Что на вход?", "Что вернуть?"]})
        agent = PlannerAgent(llm=llm, settings=settings)
        result = await agent.clarify(_state("сделай что-нибудь"))
        assert result["response_type"] == "clarification"
        assert "Что на вход?" in result["response"]
        assert result["clarification_questions"] == ["Что на вход?", "Что вернуть?"]

    def test_assemble_single_task(self, settings):
        from src.agents.planner import PlannerAgent
        agent = PlannerAgent(llm=FakeLLMProvider(), settings=settings)
        code = "local function f() return 1 end"
        state = _state("task", plan=[{"id": "t1"}], generated_codes={"t1": code})
        result = agent.assemble(state)
        assert result["assembled_code"] == code

    def test_assemble_deduplicates(self, settings):
        from src.agents.planner import PlannerAgent
        agent = PlannerAgent(llm=FakeLLMProvider(), settings=settings)
        code_a = "local function helper() return 1 end"
        code_b = "local function helper() return 1 end\nlocal function main() return helper() end"
        state = _state("task",
                       plan=[{"id": "t1"}, {"id": "t2"}],
                       generated_codes={"t1": code_a, "t2": code_b})
        result = agent.assemble(state)
        # helper should appear only once
        assembled = result["assembled_code"]
        assert assembled.count("function helper") == 1
