import json
import re
from datetime import datetime
from pathlib import Path

from agents.base import BaseAgent
from state import STATUS_FAILED, STATUS_FINALIZED, STATUS_TESTED, build_failure_result


class FinalizeArtifactAgentV1(BaseAgent):
    role = "finalize_artifact"
    version = "v1"

    def run(self, state):
        defaults = self.with_defaults(state)
        current_status = str(defaults["status"])
        current_code = str(defaults["current_code"]).strip()
        test_result = defaults["test_result"]

        if current_status != STATUS_TESTED:
            return self._build_finalization_failure(
                state,
                reason=(
                    f"Packager / Finalizer Agent expects status={STATUS_TESTED}, "
                    f"got {current_status or '<empty>'}."
                ),
            )

        if not current_code:
            return self._build_finalization_failure(
                state,
                reason="Packager / Finalizer Agent requires current_code to be present.",
            )

        if not self._tests_passed(test_result):
            return self._build_finalization_failure(
                state,
                reason="Packager / Finalizer Agent requires test_result to indicate that all required tests passed.",
            )

        parsed_spec = self._normalize_spec(defaults["parsed_spec"])
        implementation_plan = self._normalize_plan(defaults["implementation_plan"])
        repair_history = self._normalize_history(defaults["repair_history"])

        final_artifact = {
            "task_goal": parsed_spec["goal"] or implementation_plan["goal"],
            "lua_code": current_code,
            "implementation_summary": self._build_implementation_summary(
                parsed_spec=parsed_spec,
                implementation_plan=implementation_plan,
            ),
            "validation_summary": self._build_validation_summary(
                execution_result=defaults["execution_result"],
                test_result=test_result,
            ),
            "usage_notes": self._build_usage_notes(parsed_spec),
            "limitations": self._build_limitations(parsed_spec, repair_history),
        }
        final_notes = self._build_final_notes(
            execution_result=defaults["execution_result"],
            test_result=test_result,
            repair_history=repair_history,
        )

        try:
            artifact_dir, saved_files = self._save_artifact_bundle(
                final_artifact=final_artifact,
                final_notes=final_notes,
            )
        except OSError as exc:
            return self._build_finalization_failure(
                state,
                reason=f"Packager / Finalizer Agent could not save the final artifact bundle: {exc}",
            )

        final_artifact["artifact_dir"] = artifact_dir
        final_artifact["saved_files"] = saved_files
        final_notes.append(f"Saved final artifact bundle to {artifact_dir}.")

        return {
            "final_artifact": final_artifact,
            "final_notes": final_notes,
            "status": STATUS_FINALIZED,
        }

    def _build_finalization_failure(self, state, *, reason: str) -> dict:
        failure = build_failure_result(state, reason)
        return {
            **failure,
            "final_notes": [reason],
            "status": STATUS_FAILED,
        }

    @staticmethod
    def _tests_passed(test_result) -> bool:
        if not isinstance(test_result, dict):
            return False

        summary = test_result.get("summary", {})
        if not isinstance(summary, dict):
            return False

        try:
            total = int(summary.get("total", 0))
            failed = int(summary.get("failed", 0))
        except (TypeError, ValueError):
            return False

        return total > 0 and failed == 0

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

    def _normalize_spec(self, parsed_spec) -> dict:
        if not isinstance(parsed_spec, dict):
            parsed_spec = {}
        return {
            "goal": str(parsed_spec.get("goal", "")).strip(),
            "inputs": self._normalize_list(parsed_spec.get("inputs")),
            "outputs": self._normalize_list(parsed_spec.get("outputs")),
            "constraints": self._normalize_list(parsed_spec.get("constraints")),
            "assumptions": self._normalize_list(parsed_spec.get("assumptions")),
            "success_criteria": self._normalize_list(parsed_spec.get("success_criteria")),
        }

    def _normalize_plan(self, implementation_plan) -> dict:
        if not isinstance(implementation_plan, dict):
            implementation_plan = {}
        return {
            "goal": str(implementation_plan.get("goal", "")).strip(),
            "steps": self._normalize_list(implementation_plan.get("steps")),
            "components": self._normalize_list(implementation_plan.get("components")),
            "constraints": self._normalize_list(implementation_plan.get("constraints")),
            "assumptions": self._normalize_list(implementation_plan.get("assumptions")),
        }

    def _normalize_history(self, value) -> list[dict]:
        if value is None:
            return []
        if not isinstance(value, list):
            value = [value]

        history: list[dict] = []
        for entry in value:
            if isinstance(entry, dict):
                reason = str(entry.get("reason", "")).strip()
                changes = self._normalize_list(entry.get("changes"))
            else:
                reason = str(entry).strip()
                changes = []

            if not reason and not changes:
                continue

            history.append(
                {
                    "reason": reason or "Previous repair attempt.",
                    "changes": changes,
                }
            )
        return history

    def _build_implementation_summary(
        self,
        *,
        parsed_spec: dict,
        implementation_plan: dict,
    ) -> list[str]:
        summary: list[str] = []

        if implementation_plan["components"]:
            for component in implementation_plan["components"][:4]:
                summary.append(f"Includes component: {component}.")

        if not summary and implementation_plan["steps"]:
            for step in implementation_plan["steps"][:4]:
                summary.append(step)

        for output in parsed_spec["outputs"][:2]:
            summary.append(f"Produces required output: {output}")

        if not summary:
            summary.append("Implements the validated Lua solution defined by the parsed task and plan.")

        return summary[:6]

    def _build_validation_summary(self, *, execution_result: dict, test_result: dict) -> dict:
        execution_status = str(execution_result.get("execution_status", "")).strip() or "unknown"
        execution_exit_code = execution_result.get("exit_code")
        execution_text = f"Execution status: {execution_status}"
        if execution_exit_code is not None:
            execution_text += f", exit_code={execution_exit_code}"

        summary = test_result.get("summary", {}) if isinstance(test_result, dict) else {}
        total = summary.get("total", 0)
        passed = summary.get("passed", 0)
        failed = summary.get("failed", 0)
        tests_text = f"Tests passed: {passed}/{total}, failed: {failed}"

        return {
            "execution": execution_text,
            "tests": tests_text,
        }

    def _build_usage_notes(self, parsed_spec: dict) -> list[str]:
        notes: list[str] = []
        constraints_text = " ".join(parsed_spec["constraints"]).lower()
        inputs_text = " ".join(parsed_spec["inputs"]).lower()

        if any(marker in constraints_text for marker in ("console", "консол")) or any(
            marker in inputs_text for marker in ("console", "консол", "user input")
        ):
            notes.append("Run the Lua script in a console-capable local runtime.")

        if any(marker in " ".join(parsed_spec["constraints"]).lower() for marker in ("file", "файл")):
            notes.append("Ensure the working directory allows file creation if persistence is part of the task.")

        return notes

    def _build_limitations(self, parsed_spec: dict, repair_history: list[dict]) -> list[str]:
        limitations: list[str] = []
        for assumption in parsed_spec["assumptions"][:3]:
            limitations.append(f"Assumption carried into the final artifact: {assumption}")

        if repair_history:
            limitations.append(
                "The final code reflects one or more repair iterations based on observed execution or test diagnostics."
            )

        return limitations[:4]

    def _save_artifact_bundle(
        self,
        *,
        final_artifact: dict,
        final_notes: list[str],
    ) -> tuple[str, dict[str, str]]:
        artifact_dir = self._allocate_artifact_dir(final_artifact["task_goal"])
        artifact_dir.mkdir(parents=True, exist_ok=False)

        solution_path = artifact_dir / "solution.lua"
        solution_path.write_text(
            final_artifact["lua_code"].rstrip() + "\n",
            encoding="utf-8",
        )

        saved_files = {
            "solution.lua": str(solution_path.resolve()),
        }
        artifact_payload = {
            **final_artifact,
            "artifact_dir": str(artifact_dir.resolve()),
            "saved_files": saved_files,
            "final_notes": list(final_notes),
        }

        metadata_path = artifact_dir / "final_artifact.json"
        metadata_path.write_text(
            json.dumps(artifact_payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        saved_files["final_artifact.json"] = str(metadata_path.resolve())

        return str(artifact_dir.resolve()), saved_files

    def _allocate_artifact_dir(self, task_goal: str) -> Path:
        base_dir = Path(self.runtime_config.artifacts_dir).expanduser().resolve()
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        slug = self._slugify_task_goal(task_goal) or "lua-task"
        candidate = base_dir / f"{timestamp}_{slug}"
        suffix = 1
        while candidate.exists():
            candidate = base_dir / f"{timestamp}_{slug}_{suffix}"
            suffix += 1
        return candidate

    @staticmethod
    def _slugify_task_goal(task_goal: str) -> str:
        normalized = re.sub(r"[^a-z0-9]+", "-", task_goal.lower())
        normalized = normalized.strip("-")
        if not normalized:
            return "lua-task"
        return normalized[:48].rstrip("-")

    def _build_final_notes(
        self,
        *,
        execution_result: dict,
        test_result: dict,
        repair_history: list[dict],
    ) -> list[str]:
        notes = [
            "Final artifact assembled only after successful execution and passing tests.",
        ]

        if repair_history:
            notes.append(
                f"Included validated code after {len(repair_history)} repair iteration(s)."
            )

        execution_status = str(execution_result.get("execution_status", "")).strip()
        if execution_status:
            notes.append(f"Final execution status recorded as {execution_status}.")

        summary = test_result.get("summary", {}) if isinstance(test_result, dict) else {}
        try:
            passed = int(summary.get("passed", 0))
            total = int(summary.get("total", 0))
            notes.append(f"Final test summary recorded as {passed}/{total} passed.")
        except (TypeError, ValueError):
            pass

        return notes[:4]


AGENT_CLASS = FinalizeArtifactAgentV1
