import asyncio
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from src.graph.nodes import create_nodes
from src.tools.rag_templates import TemplateMatch, render_template_prompt_block, retrieve_template_matches


ROUTE_SYSTEM_PREFIX = "You are an intent classifier"
EXPLAIN_SYSTEM_PREFIX = "You explain generated Lua code"
VERIFY_SYSTEM_PREFIX = "You are a strict verifier for LowCode Lua 5.5 workflow solutions."
PLANNER_SYSTEM_PREFIX = "You are a task analyst for a LowCode Lua 5.5 workflow script generator."


def _success_diagnostics() -> dict:
    return {
        "success": True,
        "started_ok": True,
        "timed_out": False,
        "program_mode": "workflow",
        "validation_context": "test",
        "mocked_init_variables": [],
        "mocked_var_paths": [],
        "contract_blockers": [],
        "contract_warnings": [],
        "run_output": "",
        "run_error": "",
        "run_warning": "",
        "runtime_fix_hints": [],
        "runtime_context": {},
        "result_value": None,
        "result_preview": "",
        "workflow_state": None,
        "workflow_state_preview": "",
        "luacheck_output": "",
        "luacheck_error": "",
        "luacheck_warning": "",
        "failure_kind": "",
    }


class RagPromptStubLLM:
    def __init__(self) -> None:
        self.last_generate_prompt = ""

    async def generate(
        self,
        prompt: str,
        system: str = "",
        temperature: float = 0.2,
        max_tokens: int | None = None,
        *,
        agent_name: str = "",
    ) -> str:
        self.last_generate_prompt = prompt
        return "lua{return wf.vars.json.IDOC.ZCDF_HEAD.DATUM}lua"

    async def generate_json(
        self,
        prompt: str,
        system: str = "",
        temperature: float = 0.0,
        *,
        agent_name: str = "",
    ) -> dict:
        if system.startswith(ROUTE_SYSTEM_PREFIX):
            return {"intent": "create", "confidence": 1.0}
        if system.startswith(PLANNER_SYSTEM_PREFIX):
            return {
                "reformulated_task": "Собрать ISO 8601 строку из wf.vars.json.IDOC.ZCDF_HEAD.DATUM и wf.vars.json.IDOC.ZCDF_HEAD.TIME",
                "identified_workflow_paths": [
                    "wf.vars.json.IDOC.ZCDF_HEAD.DATUM",
                    "wf.vars.json.IDOC.ZCDF_HEAD.TIME",
                ],
                "target_operation": "convert",
                "key_entities": ["DATUM", "TIME", "ISO 8601"],
                "data_types": {
                    "wf.vars.json.IDOC.ZCDF_HEAD.DATUM": "string",
                    "wf.vars.json.IDOC.ZCDF_HEAD.TIME": "string",
                },
                "expected_result_action": "return",
                "followup_action": "none",
                "needs_clarification": False,
                "clarification_questions": [],
                "confidence": 1.0,
            }
        if system.startswith(EXPLAIN_SYSTEM_PREFIX):
            return {
                "summary": "Скрипт подготовлен.",
                "what_is_in_code": [],
                "how_it_works": [],
                "suggested_changes": [],
                "clarifying_questions": [],
            }
        raise AssertionError(f"unexpected generate_json call: {system[:80]}")

    async def chat(
        self,
        messages: list[dict],
        temperature: float = 0.2,
        max_tokens: int | None = None,
        *,
        agent_name: str = "",
    ) -> str:
        system = str(messages[0].get("content", "")) if messages else ""
        if system.startswith(VERIFY_SYSTEM_PREFIX):
            return (
                '{"passed": true, "score": 100, "summary": "ok", '
                '"missing_requirements": [], "warnings": [], '
                '"checks": {"workflow_path_usage": {"status": "pass", "reason": ""}, '
                '"source_shape_understood": {"status": "pass", "reason": ""}, '
                '"target_shape_satisfied": {"status": "pass", "reason": ""}, '
                '"logic_correctness": {"status": "pass", "reason": ""}, '
                '"helper_api_usage": {"status": "pass", "reason": ""}, '
                '"edge_case_handling": {"status": "pass", "reason": ""}}}'
            )
        raise AssertionError(f"unexpected chat call: {system[:80]}")


class RagTemplateTests(unittest.IsolatedAsyncioTestCase):
    async def test_retrieve_template_matches_by_description_but_render_only_code(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            kb_path = Path(tmp_dir) / "kb.jsonl"
            kb_path.write_text(
                (
                    '{"id":"date_iso","title":"ISO 8601 из DATUM и TIME","task_type":"convert",'
                    '"retrieval_text":"Шаблон для задач где дата YYYYMMDD и время HHMMSS нужно собрать в ISO 8601 строку.",'
                    '"use_cases":["Собрать ISO из DATUM/TIME"],'
                    '"tags":["convert","datetime","datum","time"],'
                    '"input_shape":"object","output_shape":"string",'
                    '"llm_context":"lua{\\nlocal datum = wf.vars.json.IDOC.ZCDF_HEAD.DATUM\\nlocal time = wf.vars.json.IDOC.ZCDF_HEAD.TIME\\nreturn datum .. time\\n}lua"}\n'
                    '{"id":"remove_keys","title":"Удаление ключей","task_type":"remove_keys",'
                    '"retrieval_text":"Шаблон для удаления ключей из объектов массива.",'
                    '"use_cases":["Удалить ID"],'
                    '"tags":["remove_keys","array_object"],'
                    '"input_shape":"array_object","output_shape":"array_object",'
                    '"llm_context":"lua{\\nreturn wf.vars.items\\n}lua"}\n'
                ),
                encoding="utf-8",
            )

            compiled_request = {
                "task_text": "Преобразуй DATUM и TIME в ISO 8601 строку",
                "selected_operation": "convert",
                "selected_primary_type": "object",
                "planner_result": {
                    "reformulated_task": "Собрать ISO 8601 строку из полей DATUM и TIME",
                    "target_operation": "convert",
                },
                "semantic_expectations": ["datetime_to_iso8601"],
                "requested_item_keys": ["DATUM", "TIME"],
            }

            with patch.dict(
                os.environ,
                {
                    "RAG_TEMPLATES_ENABLED": "true",
                    "RAG_TEMPLATES_KB_PATH": str(kb_path),
                    "RAG_TEMPLATES_TOP_K": "1",
                    "RAG_TEMPLATES_REQUIRE_PLANNER": "false",
                },
                clear=False,
            ), patch(
                "src.tools.rag_templates._embedding_rank",
                new=AsyncMock(side_effect=RuntimeError("embedding unavailable")),
            ):
                matches = await retrieve_template_matches(compiled_request)

            self.assertEqual(len(matches), 1)
            self.assertEqual(matches[0].id, "date_iso")

            rendered = render_template_prompt_block(matches)
            self.assertIn("Relevant Lua template patterns:", rendered)
            self.assertIn("local datum = wf.vars.json.IDOC.ZCDF_HEAD.DATUM", rendered)
            self.assertNotIn("Шаблон для задач где дата YYYYMMDD", rendered)

    async def test_generator_prompt_includes_retrieved_template_code_only(self) -> None:
        async def fake_retrieve_templates(compiled_request: dict[str, object]) -> list[TemplateMatch]:
            return [
                TemplateMatch(
                    id="date_iso",
                    title="ISO 8601 из DATUM и TIME",
                    task_type="convert",
                    input_shape="object",
                    output_shape="string",
                    retrieval_text="ISO 8601 template for DATUM/TIME conversion",
                    llm_context=(
                        "lua{\n"
                        "local datum = wf.vars.json.IDOC.ZCDF_HEAD.DATUM\n"
                        "local time = wf.vars.json.IDOC.ZCDF_HEAD.TIME\n"
                        "return datum .. time\n"
                        "}lua"
                    ),
                    score=0.99,
                )
            ]

        llm = RagPromptStubLLM()
        nodes = create_nodes(llm)
        state = {
            "user_input": "Преобразуй дату и время в ISO 8601 строку.",
            "base_prompt": "Преобразуй дату и время в ISO 8601 строку.",
            "target_path": "",
            "workspace_root": ".",
            "target_directory": ".",
            "target_explicit": False,
            "compiled_request": {
                "task_text": "Собрать ISO 8601 строку из wf.vars.json.IDOC.ZCDF_HEAD.DATUM и wf.vars.json.IDOC.ZCDF_HEAD.TIME",
                "raw_context": "",
                "clarification_text": "",
                "selected_operation": "convert",
                "selected_primary_path": "wf.vars.json.IDOC.ZCDF_HEAD.DATUM",
                "selected_primary_type": "object",
                "requested_item_keys": ["DATUM", "TIME"],
                "planner_result": {
                    "reformulated_task": "Собрать ISO 8601 строку из wf.vars.json.IDOC.ZCDF_HEAD.DATUM и wf.vars.json.IDOC.ZCDF_HEAD.TIME",
                    "identified_workflow_paths": [
                        "wf.vars.json.IDOC.ZCDF_HEAD.DATUM",
                        "wf.vars.json.IDOC.ZCDF_HEAD.TIME",
                    ],
                    "target_operation": "convert",
                    "expected_result_action": "return",
                    "data_types": {
                        "wf.vars.json.IDOC.ZCDF_HEAD.DATUM": "string",
                        "wf.vars.json.IDOC.ZCDF_HEAD.TIME": "string",
                    },
                },
            },
        }

        with patch(
            "src.graph.nodes.retrieve_template_matches",
            new=fake_retrieve_templates,
        ):
            result = await nodes["generate_code"](state)

        self.assertEqual(result["generated_code"].strip(), "return wf.vars.json.IDOC.ZCDF_HEAD.DATUM")
        self.assertIn("Relevant Lua template patterns:", llm.last_generate_prompt)
        self.assertIn("local datum = wf.vars.json.IDOC.ZCDF_HEAD.DATUM", llm.last_generate_prompt)
        self.assertNotIn("Шаблон для задач", llm.last_generate_prompt)


if __name__ == "__main__":
    unittest.main()
