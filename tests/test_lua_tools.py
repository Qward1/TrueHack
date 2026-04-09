"""Tests for LuaValidator and LuaExecutor.

Requires lua54 on PATH (or C:\\lua54 added to PATH before running).
"""

from __future__ import annotations

import os
import sys

import pytest

# Make lua54 discoverable without requiring it to be in the system PATH.
_LUA_DIR = r"C:\lua54"
if os.path.isdir(_LUA_DIR) and _LUA_DIR not in os.environ.get("PATH", ""):
    os.environ["PATH"] = _LUA_DIR + os.pathsep + os.environ.get("PATH", "")

from src.tools.lua_executor import LuaExecutor
from src.tools.lua_validator import LuaValidator

LUA_CMD = "lua54"


# ──────────────────────── LuaValidator ───────────────────────────────────

class TestLuaValidatorSyntax:
    @pytest.fixture
    def validator(self):
        return LuaValidator(LUA_CMD)

    @pytest.mark.asyncio
    async def test_valid_code_passes(self, validator):
        code = 'print("hello")'
        result = await validator.check_syntax(code)
        assert result["valid"] is True
        assert result["errors"] == []

    @pytest.mark.asyncio
    async def test_syntax_error_detected(self, validator):
        code = "local x = (1 + "   # incomplete expression
        result = await validator.check_syntax(code)
        assert result["valid"] is False
        assert len(result["errors"]) > 0
        # error should carry a line number
        assert result["errors"][0]["line"] >= 1

    @pytest.mark.asyncio
    async def test_multiline_valid(self, validator):
        code = """
local function add(a, b)
    return a + b
end
print(add(1, 2))
"""
        result = await validator.check_syntax(code)
        assert result["valid"] is True

    @pytest.mark.asyncio
    async def test_validate_returns_passed_for_valid_code(self, validator):
        result = await validator.validate('local x = 42')
        assert result["passed"] is True
        assert result["issues"] == []

    @pytest.mark.asyncio
    async def test_validate_returns_issues_for_invalid_code(self, validator):
        result = await validator.validate("end end end")
        assert result["passed"] is False
        errors = [i for i in result["issues"] if i["severity"] == "error"]
        assert len(errors) > 0

    @pytest.mark.asyncio
    async def test_lint_returns_available_false_when_no_luacheck(self, validator, monkeypatch):
        import shutil
        monkeypatch.setattr(shutil, "which", lambda _cmd: None)
        validator._luacheck_available = None  # reset cache
        result = await validator.lint('local x = 1')
        assert result["available"] is False


# ──────────────────────── LuaExecutor ────────────────────────────────────

class TestLuaExecutor:
    @pytest.fixture
    def executor(self):
        return LuaExecutor(LUA_CMD)

    @pytest.mark.asyncio
    async def test_executes_valid_code(self, executor):
        result = await executor.execute('print("hello world")')
        assert result["success"] is True
        assert "hello world" in result["stdout"]
        assert result["timed_out"] is False

    @pytest.mark.asyncio
    async def test_captures_runtime_error(self, executor):
        # Call a nil value → runtime error
        result = await executor.execute("local x = nil; x()")
        assert result["success"] is False
        assert result["stderr"] != ""
        assert result["timed_out"] is False

    @pytest.mark.asyncio
    async def test_timeout_kills_infinite_loop(self, executor):
        result = await executor.execute("while true do end", timeout=1)
        assert result["timed_out"] is True
        assert result["success"] is False

    @pytest.mark.asyncio
    async def test_sandbox_blocks_os_execute(self, executor):
        # os.execute should be nil — calling it is a runtime error
        result = await executor.execute('os.execute("echo hi")')
        assert result["success"] is False

    @pytest.mark.asyncio
    async def test_run_with_tests_passes(self, executor):
        code = """
local function add(a, b) return a + b end
"""
        tests = """
assert(add(2, 3) == 5, "add failed")
print("ok")
"""
        result = await executor.run_with_tests(code, tests)
        assert result["passed"] is True
        assert "ok" in result["output"]

    @pytest.mark.asyncio
    async def test_run_with_tests_fails_on_assertion(self, executor):
        code = "local function add(a, b) return 0 end"
        tests = 'assert(add(1,1) == 2, "wrong result")'
        result = await executor.run_with_tests(code, tests)
        assert result["passed"] is False
        assert result["errors"] != ""

    @pytest.mark.asyncio
    async def test_multiline_output(self, executor):
        code = '\n'.join(f'print({i})' for i in range(5))
        result = await executor.execute(code)
        assert result["success"] is True
        for i in range(5):
            assert str(i) in result["stdout"]


# ──────────────────────── utils ──────────────────────────────────────────

class TestParseUtils:
    def test_parse_plain_json(self):
        from src.core.utils import parse_llm_json
        result = parse_llm_json('{"intent": "generate"}', fallback={})
        assert result == {"intent": "generate"}

    def test_parse_json_fence(self):
        from src.core.utils import parse_llm_json
        raw = '```json\n{"intent": "fix"}\n```'
        assert parse_llm_json(raw, fallback={}) == {"intent": "fix"}

    def test_parse_with_leading_text(self):
        from src.core.utils import parse_llm_json
        raw = 'Sure, here you go:\n{"a": 1}'
        assert parse_llm_json(raw, fallback={}) == {"a": 1}

    def test_parse_invalid_returns_fallback(self):
        from src.core.utils import parse_llm_json
        fb = {"intent": "generate_unclear"}
        result = parse_llm_json("not json at all !!!", fallback=fb)
        assert result is fb

    def test_extract_lua_code_from_fence(self):
        from src.core.utils import extract_lua_code
        raw = '```lua\nprint("hi")\n```'
        assert extract_lua_code(raw) == 'print("hi")'

    def test_extract_lua_code_no_fence(self):
        from src.core.utils import extract_lua_code
        raw = '  print("hi")  \n'
        assert extract_lua_code(raw) == 'print("hi")'
