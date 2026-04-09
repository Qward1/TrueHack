"""Sandboxed Lua executor."""

from __future__ import annotations

import asyncio
import os
import tempfile

import structlog

from src.tools.base import Tool

logger = structlog.get_logger(__name__)

# Disable dangerous stdlib functions before running user code.
_SANDBOX_PREAMBLE = """\
os.execute = nil
os.remove  = nil
os.rename  = nil
io.popen   = nil
loadfile   = nil
dofile     = nil
"""


class LuaExecutor(Tool):
    """Execute Lua code in a restricted sandbox via ``lua54``."""

    def __init__(self, lua_cmd: str) -> None:
        self._lua_cmd = lua_cmd

    async def run(self, code: str, **kwargs: object) -> dict:
        """Primary tool entry point — delegates to :meth:`execute`."""
        return await self.execute(code, timeout=int(kwargs.get("timeout", 5)))

    async def execute(self, code: str, timeout: int = 5) -> dict:
        """Run *code* with sandbox preamble, return stdout/stderr/timing.

        Returns::

            {
                "success": bool,
                "stdout":  str,
                "stderr":  str,
                "timed_out": bool,
            }
        """
        sandboxed = _SANDBOX_PREAMBLE + code
        tmp_path = self._write_tmp(sandboxed)
        try:
            proc = await asyncio.create_subprocess_exec(
                self._lua_cmd,
                tmp_path,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                stdout_b, stderr_b = await asyncio.wait_for(
                    proc.communicate(), timeout=timeout
                )
            except asyncio.TimeoutError:
                proc.kill()
                try:
                    await proc.communicate()
                except Exception:  # noqa: BLE001
                    pass
                logger.warning("lua_executor_timeout", timeout=timeout)
                return {
                    "success": False,
                    "stdout": "",
                    "stderr": f"Execution timed out after {timeout}s",
                    "timed_out": True,
                }

            stdout = stdout_b.decode("utf-8", errors="replace")
            stderr = stderr_b.decode("utf-8", errors="replace")
            success = proc.returncode == 0

            logger.info(
                "lua_executor_done",
                success=success,
                stdout_len=len(stdout),
                stderr_len=len(stderr),
            )
            return {"success": success, "stdout": stdout, "stderr": stderr, "timed_out": False}
        finally:
            self._remove_tmp(tmp_path)

    async def run_with_tests(self, code: str, test_code: str) -> dict:
        """Concatenate *code* + *test_code* and execute.

        Returns::

            {"passed": bool, "output": str, "errors": str}
        """
        combined = f"{code}\n\n-- Tests\n{test_code}"
        result = await self.execute(combined)
        return {
            "passed": result["success"] and not result["timed_out"],
            "output": result["stdout"],
            "errors": result["stderr"],
        }

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
