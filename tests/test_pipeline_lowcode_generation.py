import asyncio
import unittest
from pathlib import Path
from unittest.mock import patch

from src.graph.engine import PipelineEngine


ROUTE_SYSTEM_PREFIX = "You are an intent classifier"
EXPLAIN_SYSTEM_PREFIX = "You explain generated Lua code"
VERIFY_SYSTEM_PREFIX = "You review whether a Lua solution fully satisfies the user's request."
FIX_SYSTEM_PREFIX = "You fix broken Lua workflow scripts."


class StubLLM:
    def __init__(self, *, generate_responses: list[str], fix_response: str) -> None:
        self._generate_responses = list(generate_responses)
        self._fix_response = fix_response
        self.fix_calls = 0

    async def generate(self, prompt: str, system: str = "", temperature: float = 0.2, max_tokens: int | None = None) -> str:
        if system.startswith(ROUTE_SYSTEM_PREFIX):
            raise AssertionError("route_intent should use generate_json")
        if system.startswith(EXPLAIN_SYSTEM_PREFIX):
            raise AssertionError("explain_solution should use generate_json")
        if self._generate_responses:
            return self._generate_responses.pop(0)
        raise AssertionError(f"unexpected generate call: {system[:80]}")

    async def generate_json(self, prompt: str, system: str = "", temperature: float = 0.0) -> dict:
        if system.startswith(ROUTE_SYSTEM_PREFIX):
            return {"intent": "create", "confidence": 1.0}
        if system.startswith(EXPLAIN_SYSTEM_PREFIX):
            return {
                "summary": "Workflow script prepared.",
                "what_is_in_code": ["Uses wf.vars directly."],
                "how_it_works": ["Returns the requested value."],
                "suggested_changes": [],
                "clarifying_questions": [],
            }
        raise AssertionError(f"unexpected generate_json call: {system[:80]}")

    async def chat(self, messages: list[dict], temperature: float = 0.2, max_tokens: int | None = None) -> str:
        system = str(messages[0].get("content", "")) if messages else ""
        if system.startswith(VERIFY_SYSTEM_PREFIX):
            return (
                '{"passed": true, "score": 100, "summary": "LLM verification passed.", '
                '"missing_requirements": [], "warnings": []}'
            )
        if system.startswith(FIX_SYSTEM_PREFIX):
            self.fix_calls += 1
            return self._fix_response
        raise AssertionError(f"unexpected chat call: {system[:80]}")


def _success_diagnostics() -> dict:
    return {
        "success": True,
        "started_ok": True,
        "timed_out": False,
        "program_mode": "workflow",
        "validation_context": "test",
        "mocked_init_variables": [],
        "mocked_var_paths": ["emails"],
        "contract_blockers": [],
        "contract_warnings": [],
        "run_output": "",
        "run_error": "",
        "run_warning": "",
        "luacheck_output": "",
        "luacheck_error": "",
        "luacheck_warning": "",
        "failure_kind": "",
    }


class PipelineLowcodeGenerationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp_path = Path("tests_tmp_pipeline")
        self.tmp_path.mkdir(exist_ok=True)

    def tearDown(self) -> None:
        if self.tmp_path.exists():
            for child in self.tmp_path.iterdir():
                child.unlink()
            self.tmp_path.rmdir()

    def test_accepts_public_sample_style_generation(self) -> None:
        async def fake_run_diagnostics(code: str, lua_bin: str = "lua55", startup_timeout: float = 3.0) -> dict:
            return _success_diagnostics()

        def fake_save_output(target_path: str, code: str, jsonstring_code: str = "") -> dict:
            return {
                "lua_path": target_path,
                "jsonstring_path": f"{target_path}.jsonstring.txt",
            }

        llm = StubLLM(
            generate_responses=["lua{return wf.vars.emails[#wf.vars.emails]}lua"],
            fix_response="lua{return wf.vars.emails[#wf.vars.emails]}lua",
        )
        target_path = self.tmp_path / "sample.lua"
        prompt = """Return the last email from the provided workflow context.

{
  "wf": {
    "vars": {
      "emails": ["user1@example.com", "user2@example.com", "user3@example.com"]
    }
  }
}"""

        with patch("src.graph.nodes.async_run_diagnostics", new=fake_run_diagnostics), patch(
            "src.graph.nodes.save_final_output",
            new=fake_save_output,
        ):
            engine = PipelineEngine(llm=llm)
            result = asyncio.run(
                engine.process_message(
                    chat_id=1,
                    user_input=prompt,
                    workspace_root=str(self.tmp_path),
                    target_path=str(target_path),
                )
            )

        self.assertTrue(result["save_success"])
        self.assertEqual(result["generated_code"].strip(), "return wf.vars.emails[#wf.vars.emails]")
        self.assertTrue(result["verification"]["passed"])
        self.assertEqual(llm.fix_calls, 0)

    def test_sends_app_style_generation_into_fix_loop(self) -> None:
        async def fake_run_diagnostics(code: str, lua_bin: str = "lua55", startup_timeout: float = 3.0) -> dict:
            return _success_diagnostics()

        def fake_save_output(target_path: str, code: str, jsonstring_code: str = "") -> dict:
            return {
                "lua_path": target_path,
                "jsonstring_path": f"{target_path}.jsonstring.txt",
            }

        llm = StubLLM(
            generate_responses=[
                """lua{
local emails = {"user1@example.com", "user2@example.com", "user3@example.com"}
return emails[#emails]
}lua"""
            ],
            fix_response="lua{return wf.vars.emails[#wf.vars.emails]}lua",
        )
        target_path = self.tmp_path / "sample.lua"
        prompt = """Return the last email from the provided workflow context.

{
  "wf": {
    "vars": {
      "emails": ["user1@example.com", "user2@example.com", "user3@example.com"]
    }
  }
}"""

        with patch("src.graph.nodes.async_run_diagnostics", new=fake_run_diagnostics), patch(
            "src.graph.nodes.save_final_output",
            new=fake_save_output,
        ):
            engine = PipelineEngine(llm=llm)
            result = asyncio.run(
                engine.process_message(
                    chat_id=1,
                    user_input=prompt,
                    workspace_root=str(self.tmp_path),
                    target_path=str(target_path),
                )
            )

        self.assertEqual(llm.fix_calls, 1)
        self.assertTrue(result["save_success"])
        self.assertEqual(result["generated_code"].strip(), "return wf.vars.emails[#wf.vars.emails]")
        self.assertEqual(result["verification"]["anti_patterns"], [])


if __name__ == "__main__":
    unittest.main()
