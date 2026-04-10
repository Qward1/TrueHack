"""Tests for LuaValidator and LuaExecutor (duplicate-free from test_lua_tools.py)."""

from __future__ import annotations

import pytest

from src.tools.lua_executor import LuaExecutor
from src.tools.lua_validator import LuaValidator

LUA_CMD = "lua54"  # PATH patched by conftest.py


# ── LuaValidator ─────────────────────────────────────────────────────────

class TestValidator:
    @pytest.fixture
    def v(self):
        return LuaValidator(LUA_CMD)

    @pytest.mark.asyncio
    async def test_valid_code(self, v):
        r = await v.check_syntax("local x = 1 + 1")
        assert r["valid"] is True

    @pytest.mark.asyncio
    async def test_syntax_error(self, v):
        r = await v.check_syntax("local x = (")
        assert r["valid"] is False
        assert r["errors"]

    @pytest.mark.asyncio
    async def test_validate_passed(self, v):
        r = await v.validate("local function f() return 1 end")
        assert r["passed"] is True
        # When luacheck is installed the code may emit warnings (e.g. W211
        # "unused variable"); we only require no *errors*.
        assert [i for i in r["issues"] if i["severity"] == "error"] == []

    @pytest.mark.asyncio
    async def test_validate_failed(self, v):
        r = await v.validate("end end")
        assert r["passed"] is False
        errors = [i for i in r["issues"] if i["severity"] == "error"]
        assert errors

    @pytest.mark.asyncio
    async def test_lint_no_luacheck(self, v, monkeypatch):
        import shutil
        monkeypatch.setattr(shutil, "which", lambda _: None)
        v._luacheck_available = None
        r = await v.lint("local x = 1")
        assert r["available"] is False


# ── LuaExecutor ───────────────────────────────────────────────────────────

class TestExecutor:
    @pytest.fixture
    def ex(self):
        return LuaExecutor(LUA_CMD)

    @pytest.mark.asyncio
    async def test_run_valid(self, ex):
        r = await ex.execute('io.write("hi")')
        assert r["success"] is True
        assert "hi" in r["stdout"]
        assert r["timed_out"] is False

    @pytest.mark.asyncio
    async def test_run_error(self, ex):
        r = await ex.execute("local x = nil; x()")
        assert r["success"] is False

    @pytest.mark.asyncio
    async def test_timeout(self, ex):
        r = await ex.execute("while true do end", timeout=1)
        assert r["timed_out"] is True

    @pytest.mark.asyncio
    async def test_sandbox(self, ex):
        r = await ex.execute('os.execute("echo hi")')
        assert r["success"] is False   # os.execute is nil'd

    @pytest.mark.asyncio
    async def test_run_with_tests_ok(self, ex):
        code = "local function add(a,b) return a+b end"
        tests = "assert(add(1,2)==3)"
        r = await ex.run_with_tests(code, tests)
        assert r["passed"] is True

    @pytest.mark.asyncio
    async def test_run_with_tests_fail(self, ex):
        code = "local function add(a,b) return 0 end"
        tests = 'assert(add(1,2)==3, "wrong")'
        r = await ex.run_with_tests(code, tests)
        assert r["passed"] is False
