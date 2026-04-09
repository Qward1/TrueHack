"""Lua syntax validator + optional luacheck linter wrapper."""

from __future__ import annotations

import asyncio
import os
import re
import shutil
import tempfile

import structlog

from src.tools.base import Tool

logger = structlog.get_logger(__name__)

# Match "<path>:<line>: <message>" — non-greedy so Windows paths like
# "C:\...\tmp.lua:5: ..." work (the first "C:" does not contain digits after).
_LUA_ERROR_RE = re.compile(r"^(?:lua(?:54)?:\s*)?.*?:(\d+):\s*(.*)$")

# luacheck output (with --codes --ranges): "<path>:<line>:<col>-<col>: (<code>) <msg>"
_LUACHECK_LINE_RE = re.compile(r"^.+?:(\d+):\d+(?:-\d+)?:\s*\((\w+)\)\s*(.*)$")


class LuaValidator(Tool):
    """Validates Lua code via ``lua54`` (syntax) and optionally ``luacheck`` (lint)."""

    def __init__(self, lua_cmd: str) -> None:
        self._lua_cmd = lua_cmd
        self._luacheck_available: bool | None = None

    async def run(self, code: str, **_: object) -> dict:
        """Run full validation (syntax + lint). Primary tool entry point."""
        return await self.validate(code)

    async def check_syntax(self, code: str) -> dict:
        """Compile the code with ``loadfile`` (does NOT execute it)."""
        tmp_path = self._write_tmp(code)
        try:
            # loadfile returns (nil, errmsg) on parse error. We print the error
            # to stderr and exit 1, otherwise exit 0 — this way we never run
            # the user's code, we only check that it parses.
            check_script = (
                f"local f,e = loadfile([[{tmp_path}]]); "
                "if not f then io.stderr:write(e) io.stderr:write('\\n') os.exit(1) end"
            )
            proc = await asyncio.create_subprocess_exec(
                self._lua_cmd,
                "-e",
                check_script,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            _, stderr = await proc.communicate()
            if proc.returncode == 0:
                return {"valid": True, "errors": []}
            err_text = stderr.decode("utf-8", errors="replace").strip()
            return {"valid": False, "errors": self._parse_syntax_errors(err_text)}
        finally:
            self._remove_tmp(tmp_path)

    async def lint(self, code: str) -> dict:
        """Run luacheck if present; otherwise return ``available=False``."""
        if self._luacheck_available is None:
            self._luacheck_available = shutil.which("luacheck") is not None

        if not self._luacheck_available:
            return {"available": False, "warnings": [], "errors": []}

        tmp_path = self._write_tmp(code)
        try:
            proc = await asyncio.create_subprocess_exec(
                "luacheck",
                "--no-color",
                "--codes",
                "--ranges",
                tmp_path,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await proc.communicate()
            warnings, errors = self._parse_luacheck(
                stdout.decode("utf-8", errors="replace")
            )
            return {"available": True, "warnings": warnings, "errors": errors}
        finally:
            self._remove_tmp(tmp_path)

    async def validate(self, code: str) -> dict:
        """Syntax check → lint (if available) → consolidated issue list."""
        syntax = await self.check_syntax(code)
        issues: list[dict] = []

        for err in syntax["errors"]:
            issues.append(
                {
                    "type": "syntax",
                    "line": err.get("line", 0),
                    "message": err.get("message", ""),
                    "severity": "error",
                }
            )

        # Only run linter when the code parses — otherwise luacheck would duplicate errors.
        if syntax["valid"]:
            lint = await self.lint(code)
            if lint["available"]:
                for w in lint["warnings"]:
                    issues.append(
                        {
                            "type": "lint",
                            "line": w.get("line", 0),
                            "message": f"({w.get('code','')}) {w.get('message','')}".strip(),
                            "severity": "warning",
                        }
                    )
                for e in lint["errors"]:
                    issues.append(
                        {
                            "type": "lint",
                            "line": e.get("line", 0),
                            "message": f"({e.get('code','')}) {e.get('message','')}".strip(),
                            "severity": "error",
                        }
                    )

        passed = not any(i["severity"] == "error" for i in issues)
        return {"passed": passed, "issues": issues}

    # ── helpers ──────────────────────────────────────────────────────────
    @staticmethod
    def _write_tmp(code: str) -> str:
        with tempfile.NamedTemporaryFile(
            suffix=".lua", delete=False, mode="w", encoding="utf-8"
        ) as f:
            f.write(code)
            return f.name

    @staticmethod
    def _remove_tmp(path: str) -> None:
        try:
            os.unlink(path)
        except OSError:
            pass

    @staticmethod
    def _parse_syntax_errors(stderr: str) -> list[dict]:
        errors: list[dict] = []
        for raw_line in stderr.splitlines():
            line = raw_line.strip()
            if not line:
                continue
            m = _LUA_ERROR_RE.match(line)
            if m:
                errors.append(
                    {"line": int(m.group(1)), "message": m.group(2).strip()}
                )
            else:
                errors.append({"line": 0, "message": line})
        return errors

    @staticmethod
    def _parse_luacheck(output: str) -> tuple[list[dict], list[dict]]:
        warnings: list[dict] = []
        errors: list[dict] = []
        for raw_line in output.splitlines():
            m = _LUACHECK_LINE_RE.match(raw_line.strip())
            if not m:
                continue
            code = m.group(2)
            entry = {
                "line": int(m.group(1)),
                "code": code,
                "message": m.group(3).strip(),
            }
            if code.startswith("E"):
                errors.append(entry)
            else:
                warnings.append(entry)
        return warnings, errors
