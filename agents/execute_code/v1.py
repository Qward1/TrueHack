from agents.base import BaseAgent
from state import (
    STATUS_CODE_GENERATED,
    STATUS_EXECUTED,
    STATUS_FAILED,
    STATUS_REPAIR_NEEDED,
    build_failure_result,
)


class ExecuteCodeAgentV1(BaseAgent):
    role = "execute_code"
    version = "v1"

    def run(self, state):
        defaults = self.with_defaults(state)
        current_status = str(defaults["status"])
        current_code = str(defaults["current_code"]).strip()
        attempts = defaults["execution_attempts"] + 1

        if current_status != STATUS_CODE_GENERATED:
            failure = build_failure_result(
                state,
                f"Debug / Execution Agent expects status={STATUS_CODE_GENERATED}, got {current_status or '<empty>'}.",
            )
            return {
                **failure,
                "execution_attempts": attempts,
                "status": STATUS_FAILED,
            }

        if not current_code:
            failure = build_failure_result(
                state,
                "Debug / Execution Agent received empty current_code.",
            )
            return {
                **failure,
                "execution_attempts": attempts,
                "status": STATUS_FAILED,
            }

        if self._should_use_smoke_check(current_code, defaults["parsed_spec"]):
            runtime_result = self.lua_toolchain.run_smoke_check(current_code)
        else:
            runtime_result = self.lua_toolchain.run_script(
                current_code,
                stdin_data=self._build_sample_stdin(defaults["parsed_spec"], current_code),
            )
        execution_result = self._build_execution_result(runtime_result)
        lint_result = self._build_lint_result(runtime_result.get("lint", {}))
        lint_is_clean = lint_result["status"] != "issues_found"
        next_status = (
            STATUS_EXECUTED
            if execution_result["execution_status"] == "success" and lint_is_clean
            else STATUS_REPAIR_NEEDED
        )

        return {
            "execution_attempts": attempts,
            "execution_ok": execution_result["execution_status"] == "success" and lint_is_clean,
            "execution_result": execution_result,
            "lint_result": lint_result,
            "status": next_status,
        }

    def _build_execution_result(self, runtime_result: dict) -> dict:
        stdout = str(runtime_result.get("stdout", ""))
        stderr = str(runtime_result.get("stderr", ""))
        exit_code = runtime_result.get("exit_code")
        execution_status = self._classify_execution_status(
            success=bool(runtime_result.get("success")),
            stderr=stderr,
            exit_code=exit_code,
        )

        return {
            "execution_status": execution_status,
            "stdout": stdout,
            "stderr": stderr,
            "exit_code": exit_code,
        }

    @staticmethod
    def _classify_execution_status(
        *,
        success: bool,
        stderr: str,
        exit_code,
    ) -> str:
        if success:
            return "success"

        lowered_stderr = stderr.lower()
        if "timed out" in lowered_stderr or "timeout" in lowered_stderr:
            return "timeout"
        if isinstance(exit_code, int) and exit_code < 0:
            return "crash"

        syntax_markers = [
            "syntax error",
            "<eof> expected",
            "expected near",
            "unexpected symbol near",
            "unfinished string",
            "malformed number",
            "cannot use '...' outside a vararg function",
        ]
        if any(marker in lowered_stderr for marker in syntax_markers):
            return "syntax_error"

        return "runtime_error"

    @staticmethod
    def _build_lint_result(raw_lint: dict) -> dict:
        if not isinstance(raw_lint, dict) or not raw_lint:
            return {
                "status": "unavailable",
                "issues": [],
            }

        if not raw_lint.get("available", False):
            return {
                "status": "unavailable",
                "issues": [],
            }

        issues: list[str] = []
        stdout = str(raw_lint.get("stdout", "")).strip()
        stderr = str(raw_lint.get("stderr", "")).strip()

        for block in (stdout, stderr):
            if not block:
                continue
            for line in block.splitlines():
                normalized = line.strip()
                if normalized and normalized not in issues:
                    issues.append(normalized)

        return {
            "status": "clean" if raw_lint.get("success") else "issues_found",
            "issues": issues,
        }

    @staticmethod
    def _normalize_list(value) -> list[str]:
        if value is None:
            return []
        if not isinstance(value, list):
            value = [value]
        normalized: list[str] = []
        for item in value:
            text = str(item).strip()
            if text and text not in normalized:
                normalized.append(text)
        return normalized

    def _should_use_smoke_check(self, code: str, parsed_spec) -> bool:
        code_lower = code.lower()
        if "io.read" not in code_lower:
            return False

        spec_inputs = []
        if isinstance(parsed_spec, dict):
            spec_inputs = self._normalize_list(parsed_spec.get("inputs"))

        input_text = " ".join(spec_inputs).lower()
        has_console_inputs = any(
            marker in input_text
            for marker in ("console", "консол", "menu", "меню", "input", "ввод", "guess", "choice")
        )
        has_test_guard = "run_mode" in code_lower or "__test__" in code_lower
        return has_console_inputs and has_test_guard

    def _build_sample_stdin(self, parsed_spec, code: str) -> str:
        samples: list[str] = []
        if isinstance(parsed_spec, dict):
            for item in self._normalize_list(parsed_spec.get("inputs")):
                lowered = item.lower()
                if any(marker in lowered for marker in ("menu", "меню", "choice", "option")):
                    samples.append("1")
                elif any(marker in lowered for marker in ("guess", "number", "числ")):
                    samples.append("50")
                elif any(marker in lowered for marker in ("shop", "магаз", "buy", "sell")):
                    samples.append("0")
                else:
                    samples.append("1")

        if "io.read" in code.lower() and not samples:
            samples.extend(["1", "50", "0", "3"])

        return "\n".join(samples[:8]) + ("\n" if samples else "")


AGENT_CLASS = ExecuteCodeAgentV1
