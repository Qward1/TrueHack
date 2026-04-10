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


# ── Refine preservation guard ───────────────────────────────────────────

_QUEUE_ORIGINAL = """local M = {}

local function isEmpty(queue)
    return #queue == 0
end

function M.enqueue(queue, value)
    table.insert(queue, value)
end

function M.dequeue(queue)
    if isEmpty(queue) then error("empty") end
    return table.remove(queue, 1)
end

function M.peek(queue)
    if isEmpty(queue) then error("empty") end
    return queue[1]
end

return M
"""

# The LLM's broken refined output: dropped M.peek even though the user
# never asked to remove it.
_QUEUE_REFINED_MISSING_PEEK = """local M = {}

function M.new()
    return {}
end

function M.is_empty(queue)
    return #queue == 0
end

function M.enqueue(queue, value)
    table.insert(queue, value)
end

function M.dequeue(queue)
    if M.is_empty(queue) then error("empty") end
    return table.remove(queue, 1)
end

return M
"""


class TestRefinePreservation:
    @pytest.mark.asyncio
    async def test_refine_restores_silently_dropped_function(self, settings):
        """The LLM drops M.peek; the preservation guard must add it back."""
        from src.agents.coder import CoderAgent

        class StubRAG:
            async def search(self, *a, **kw):
                return []

        llm = FakeLLMProvider(text_response=_QUEUE_REFINED_MISSING_PEEK)
        agent = CoderAgent(llm=llm, settings=settings, rag=StubRAG())

        state = _state(
            "поправки: добавь M.new() и сделай is_empty публичной",
            assembled_code=_QUEUE_ORIGINAL,
            generated_code=_QUEUE_ORIGINAL,
        )
        result = await agent.refine(state)
        code = result["generated_code"]

        # The user-requested additions should be present…
        assert "M.new" in code
        assert "is_empty" in code
        # …and the silently-dropped M.peek MUST be restored.
        assert "M.peek" in code or "peek" in code
        # The module must still end with `return M` (not after the restored
        # block appended naively at the bottom).
        assert code.rstrip().endswith("return M")

    @pytest.mark.asyncio
    async def test_refine_does_not_restore_explicitly_removed_function(self, settings):
        """When the user says "убери peek", the guard must NOT restore it."""
        from src.agents.coder import CoderAgent

        class StubRAG:
            async def search(self, *a, **kw):
                return []

        llm = FakeLLMProvider(text_response=_QUEUE_REFINED_MISSING_PEEK)
        agent = CoderAgent(llm=llm, settings=settings, rag=StubRAG())

        state = _state(
            "убери peek, он не нужен",
            assembled_code=_QUEUE_ORIGINAL,
            generated_code=_QUEUE_ORIGINAL,
        )
        result = await agent.refine(state)
        code = result["generated_code"]

        # User explicitly asked to delete peek → must stay gone.
        assert "M.peek" not in code
        assert "function peek" not in code

    @pytest.mark.asyncio
    async def test_refine_without_existing_code_falls_back_to_generate(self, settings):
        """Empty existing_code → must not hallucinate a diff, fall through."""
        from src.agents.coder import CoderAgent

        class StubRAG:
            async def search(self, *a, **kw):
                return []

        llm = FakeLLMProvider(text_response='local function foo() return 1 end')
        agent = CoderAgent(llm=llm, settings=settings, rag=StubRAG())

        state = _state("make it faster", assembled_code="", generated_code="")
        result = await agent.refine(state)
        # Should produce *some* code via generate fallback.
        assert "foo" in result["generated_code"]


class TestExtractLuaFunctionNames:
    def test_local_function(self):
        from src.core.utils import extract_lua_function_names
        names = extract_lua_function_names("local function foo(x) return x end")
        assert names == ["foo"]

    def test_module_function(self):
        from src.core.utils import extract_lua_function_names
        code = "local M = {}\nfunction M.bar() end\nfunction M.baz(x) end\nreturn M"
        names = extract_lua_function_names(code)
        assert names == ["M.bar", "M.baz"]

    def test_method_syntax(self):
        from src.core.utils import extract_lua_function_names
        code = "function Obj:greet(who) end"
        assert extract_lua_function_names(code) == ["Obj:greet"]

    def test_dedup_preserves_order(self):
        from src.core.utils import extract_lua_function_names
        code = "function f() end\nfunction g() end\nfunction f() end"
        assert extract_lua_function_names(code) == ["f", "g"]
