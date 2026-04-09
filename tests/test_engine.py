"""Tests for AgentEngine initialization and graph structure."""

from __future__ import annotations

import os

import pytest

# Ensure lua54 is findable even if not in system PATH
_LUA_DIR = r"C:\lua54"
if os.path.isdir(_LUA_DIR) and _LUA_DIR not in os.environ.get("PATH", ""):
    os.environ["PATH"] = _LUA_DIR + os.pathsep + os.environ.get("PATH", "")

from src.core.config import get_settings
from src.graph.engine import AgentEngine


@pytest.fixture
async def engine(tmp_path):
    """Fresh engine pointing at a temp DB and RAG index."""
    settings = get_settings()
    # Override paths to avoid polluting the real data directory
    settings.paths.database = str(tmp_path / "test.db")
    settings.paths.rag_index = str(tmp_path / "rag_index")
    eng = AgentEngine(settings)
    await eng.initialize()
    yield eng
    await eng.close()


class TestEngineInit:
    @pytest.mark.asyncio
    async def test_initialize_creates_db(self, tmp_path):
        """initialize() should create the database file."""
        settings = get_settings()
        db_path = str(tmp_path / "test.db")
        rag_path = str(tmp_path / "rag_index")
        settings.paths.database = db_path
        settings.paths.rag_index = rag_path

        eng = AgentEngine(settings)
        await eng.initialize()
        await eng.close()

        import os
        assert os.path.exists(db_path), "Database file should be created"

    @pytest.mark.asyncio
    async def test_graph_has_all_nodes(self, engine):
        """Compiled graph must contain every expected node."""
        expected = {
            "__start__",
            "route_intent",
            "plan_task",
            "generate_code",
            "validate_code",
            "fix_code",
            "assemble_code",
            "refine_code",
            "answer_question",
            "prepare_response",
        }
        assert expected.issubset(set(engine._graph.nodes.keys()))

    @pytest.mark.asyncio
    async def test_repo_is_set_after_init(self, engine):
        """_repo should not be None after initialize()."""
        assert engine._repo is not None

    @pytest.mark.asyncio
    async def test_execute_code_valid(self, engine):
        """execute_code should run Lua and return stdout."""
        result = await engine.execute_code('print("engine test")')
        assert result["success"] is True
        assert "engine test" in result["stdout"]

    @pytest.mark.asyncio
    async def test_execute_code_invalid(self, engine):
        """execute_code should capture runtime errors."""
        result = await engine.execute_code("this is not lua !!!")
        assert result["success"] is False


class TestConditions:
    """Unit tests for graph condition functions — no LLM needed."""

    def test_route_by_intent_generate(self):
        from src.graph.conditions import route_by_intent
        for intent in ("generate_clear", "generate_unclear"):
            state = {"intent": intent}
            assert route_by_intent(state) == "plan"

    def test_route_by_intent_refine(self):
        from src.graph.conditions import route_by_intent
        for intent in ("refine", "fix_error"):
            assert route_by_intent({"intent": intent}) == "refine"

    def test_route_by_intent_answer(self):
        from src.graph.conditions import route_by_intent
        for intent in ("question", "explain", "general", "unknown"):
            assert route_by_intent({"intent": intent}) == "answer"

    def test_check_plan_result_respond(self):
        from src.graph.conditions import check_plan_result
        state = {"clarification_questions": ["что именно?"]}
        assert check_plan_result(state) == "respond"

    def test_check_plan_result_generate(self):
        from src.graph.conditions import check_plan_result
        assert check_plan_result({}) == "generate"
        assert check_plan_result({"clarification_questions": []}) == "generate"

    def test_has_more_tasks_loop(self):
        from src.graph.conditions import has_more_tasks
        state = {"plan": [{"id": "t1"}, {"id": "t2"}], "current_task_index": 1}
        assert has_more_tasks(state) == "generate"

    def test_has_more_tasks_done(self):
        from src.graph.conditions import has_more_tasks
        state = {"plan": [{"id": "t1"}], "current_task_index": 1}
        assert has_more_tasks(state) == "validate"

    def test_check_validation_passed(self):
        from src.graph.conditions import check_validation
        assert check_validation({"validation_passed": True, "fix_iterations": 0}) == "assemble"

    def test_check_validation_fix(self):
        from src.graph.conditions import check_validation
        state = {"validation_passed": False, "fix_iterations": 1}
        assert check_validation(state, max_fix_iterations=3) == "fix"

    def test_check_validation_force_assemble(self):
        from src.graph.conditions import check_validation
        state = {"validation_passed": False, "fix_iterations": 3}
        assert check_validation(state, max_fix_iterations=3) == "force_assemble"
