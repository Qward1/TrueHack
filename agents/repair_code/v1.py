import re

from agents.base import BaseAgent
from llm_client import LocalModelError
from state import (
    STATUS_CODE_GENERATED,
    STATUS_FAILED,
    STATUS_REPAIR_NEEDED,
    build_failure_result,
)


class RepairCodeAgentV1(BaseAgent):
    role = "repair_code"
    version = "v1"

    def run(self, state):
        defaults = self.with_defaults(state)
        current_status = str(defaults["status"])
        current_code = str(defaults["current_code"]).strip()
        attempts = defaults["repair_attempts"] + 1
        repair_history = self._normalize_history(defaults["repair_history"])

        if current_status != STATUS_REPAIR_NEEDED:
            return self._build_repair_failure(
                state,
                attempts=attempts,
                repair_history=repair_history,
                reason=(
                    f"Repair Agent expects status={STATUS_REPAIR_NEEDED}, "
                    f"got {current_status or '<empty>'}."
                ),
            )

        if not current_code:
            return self._build_repair_failure(
                state,
                attempts=attempts,
                repair_history=repair_history,
                reason="Repair Agent requires current_code to be present.",
            )

        diagnostics = self._collect_problem_diagnostics(defaults)
        if not diagnostics["has_problems"]:
            return self._build_repair_failure(
                state,
                attempts=attempts,
                repair_history=repair_history,
                reason=(
                    "Repair Agent did not receive enough failing diagnostics in "
                    "execution_result, lint_result, or test_result."
                ),
            )

        if attempts > defaults["max_attempts"]:
            staged_candidate = self._attempt_staged_rebuild(
                defaults=defaults,
                diagnostics=diagnostics,
                current_code=current_code,
            )
            if staged_candidate is not None:
                return self._build_repair_success(
                    defaults=defaults,
                    repair_history=repair_history,
                    diagnostics=diagnostics,
                    candidate=staged_candidate,
                    attempts=max(0, defaults["max_attempts"] - 3),
                )

            emergency_candidate = self._attempt_emergency_rewrite(
                defaults=defaults,
                diagnostics=diagnostics,
                repair_history=repair_history,
                current_code=current_code,
                attempts=attempts,
            )
            if emergency_candidate is not None:
                return self._build_repair_success(
                    defaults=defaults,
                    repair_history=repair_history,
                    diagnostics=diagnostics,
                    candidate=emergency_candidate,
                    attempts=max(0, defaults["max_attempts"] - 1),
                )

            return self._build_repair_failure(
                state,
                attempts=attempts,
                repair_history=repair_history,
                reason="Exceeded the maximum number of repair attempts.",
            )

        raw_baseline_code = current_code.strip()
        normalized_baseline_code = self.normalize_lua_code(current_code).strip()
        candidate = None
        attempt_failures: list[str] = []

        candidate = self._attempt_targeted_unit_repair(
            defaults=defaults,
            diagnostics=diagnostics,
            current_code=current_code,
            raw_baseline_code=raw_baseline_code,
            normalized_baseline_code=normalized_baseline_code,
            previous_failures=attempt_failures,
        )

        rewrite_only = self._looks_like_non_lua_pseudocode(current_code)

        if candidate is None:
            force_rewrite_options = (True,) if rewrite_only else (False, True)
            for force_rewrite in force_rewrite_options:
                try:
                    parsed_response = self._request_model_repair(
                        defaults=defaults,
                        diagnostics=diagnostics,
                        repair_history=repair_history,
                        current_code=current_code,
                        force_rewrite=force_rewrite,
                        previous_failures=attempt_failures,
                    )
                except (LocalModelError, ValueError, TypeError) as exc:
                    attempt_failures.append(
                        f"Model repair attempt failed: {exc}"
                    )
                    continue

                repaired_code = self.normalize_lua_code(parsed_response["current_code"])
                if not repaired_code:
                    attempt_failures.append(
                        "Model repair attempt returned empty current_code."
                    )
                    continue

                if not self._is_material_change(
                    repaired_code,
                    raw_baseline_code=raw_baseline_code,
                    normalized_baseline_code=normalized_baseline_code,
                ):
                    attempt_failures.append(
                        "Model repair attempt returned code that is effectively unchanged."
                    )
                    continue

                candidate = {
                    "current_code": repaired_code,
                    "repair_notes": parsed_response["repair_notes"],
                    "code_units": [],
                    "code_unit_map": [],
                }
                break

        if candidate is None:
            local_candidate = self._apply_local_repairs(
                current_code=current_code,
                diagnostics=diagnostics,
            )
            if local_candidate is not None:
                repaired_code = self.normalize_lua_code(local_candidate["current_code"])
                if repaired_code and self._is_material_change(
                    repaired_code,
                    raw_baseline_code=raw_baseline_code,
                    normalized_baseline_code=normalized_baseline_code,
                ):
                    candidate = {
                        "current_code": repaired_code,
                        "repair_notes": local_candidate["repair_notes"],
                    }
                else:
                    attempt_failures.append(
                        "Local repair heuristics did not produce a materially changed code version."
                    )

        if candidate is None:
            staged_candidate = self._attempt_staged_rebuild(
                defaults=defaults,
                diagnostics=diagnostics,
                current_code=current_code,
            )
            if staged_candidate is not None:
                candidate = staged_candidate

        if candidate is None:
            regenerated_candidate = self._attempt_fallback_regeneration(
                defaults=defaults,
                diagnostics=diagnostics,
                repair_history=repair_history,
                current_code=current_code,
                previous_failures=attempt_failures,
            )
            if regenerated_candidate is not None:
                candidate = regenerated_candidate
            else:
                attempt_failures.append(
                    "Fallback regeneration did not produce a materially changed code version."
                )

        if candidate is None:
            failure_reason = "Repair Agent could not produce a materially changed repaired code version."
            if attempt_failures:
                failure_reason += " " + " ".join(attempt_failures[-3:])
            return self._build_repair_failure(
                state,
                attempts=attempts,
                repair_history=repair_history,
                reason=failure_reason,
            )

        return self._build_repair_success(
            defaults=defaults,
            repair_history=repair_history,
            diagnostics=diagnostics,
            candidate=candidate,
            attempts=attempts,
        )

    def _build_repair_failure(
        self,
        state,
        *,
        attempts: int,
        repair_history: list[dict],
        reason: str,
    ) -> dict:
        failure = build_failure_result(state, reason)
        return {
            **failure,
            "repair_attempts": attempts,
            "repair_history": repair_history,
            "repair_notes": [reason],
            "status": STATUS_FAILED,
        }

    def _build_repair_success(
        self,
        *,
        defaults: dict,
        repair_history: list[dict],
        diagnostics: dict,
        candidate: dict,
        attempts: int,
    ) -> dict:
        repaired_code = candidate["current_code"]
        repair_notes = candidate["repair_notes"]
        version_entry = self._build_version_entry(
            defaults["code_versions"],
            notes=repair_notes,
        )
        history_entry = {
            "reason": diagnostics["summary"],
            "changes": repair_notes,
        }

        return {
            "current_code": repaired_code,
            "code_unit_plan": list(candidate.get("code_unit_plan", defaults.get("code_unit_plan", []))),
            "code_units": list(candidate.get("code_units", [])),
            "code_unit_map": list(candidate.get("code_unit_map", [])),
            "code_versions": [*defaults["code_versions"], version_entry],
            "repair_history": [*repair_history, history_entry],
            "repair_notes": repair_notes,
            "repair_attempts": attempts,
            "execution_ok": False,
            "tests_passed": False,
            "execution_result": {},
            "lint_result": {},
            "test_result": {},
            "status": STATUS_CODE_GENERATED,
        }

    def _attempt_staged_rebuild(
        self,
        *,
        defaults: dict,
        diagnostics: dict,
        current_code: str,
    ) -> dict | None:
        try:
            from agents.generate_code.v1 import GenerateCodeAgentV1
        except Exception:
            return None

        raw_baseline_code = current_code.strip()
        normalized_baseline_code = self.normalize_lua_code(current_code).strip()

        generator = GenerateCodeAgentV1(
            model_client=self.model_client,
            lua_toolchain=self.lua_toolchain,
            runtime_config=self.runtime_config,
        )
        try:
            unit_plan = generator._build_code_unit_plan(defaults)
            rebuilt = generator._attempt_unit_based_generation(
                defaults=defaults,
                unit_plan=unit_plan,
            )
        except Exception:
            return None

        rebuilt_code = self.normalize_lua_code(str(rebuilt.get("current_code", "")))
        if not rebuilt_code:
            return None

        if not self._is_material_change(
            rebuilt_code,
            raw_baseline_code=raw_baseline_code,
            normalized_baseline_code=normalized_baseline_code,
        ):
            return None

        repair_notes = [
            "[staged-rebuild] Rebuilt the program from smaller code units after repeated repair failures.",
        ]
        generation_notes = rebuilt.get("generation_notes", [])
        if isinstance(generation_notes, list):
            for note in generation_notes[:3]:
                text = str(note).strip()
                if text:
                    repair_notes.append(text)

        return {
            "current_code": rebuilt_code,
            "code_unit_plan": list(rebuilt.get("code_unit_plan", unit_plan)),
            "code_units": list(rebuilt.get("code_units", [])),
            "code_unit_map": list(rebuilt.get("code_unit_map", [])),
            "repair_notes": repair_notes,
        }

    def _attempt_targeted_unit_repair(
        self,
        *,
        defaults: dict,
        diagnostics: dict,
        current_code: str,
        raw_baseline_code: str,
        normalized_baseline_code: str,
        previous_failures: list[str],
    ) -> dict | None:
        code_units = self._normalize_code_units(defaults.get("code_units"))
        code_unit_map = self._normalize_code_unit_map(defaults.get("code_unit_map"))
        if not code_units or not code_unit_map:
            return None

        target_indexes = self._select_target_unit_indexes(
            diagnostics=diagnostics,
            code_units=code_units,
            code_unit_map=code_unit_map,
        )
        if not target_indexes:
            return None

        for target_index in target_indexes[:2]:
            target_unit = code_units[target_index]
            unit_name = str(target_unit.get("name", "")).strip() or f"unit_{target_index + 1}"

            try:
                repaired_fragment = self._request_targeted_unit_repair(
                    defaults=defaults,
                    diagnostics=diagnostics,
                    target_unit=target_unit,
                    code_units=code_units,
                    target_index=target_index,
                    previous_failures=previous_failures,
                )
            except (LocalModelError, ValueError, TypeError) as exc:
                previous_failures.append(
                    f"Targeted repair for unit '{unit_name}' failed: {exc}"
                )
                continue

            repaired_fragment = self.sanitize_lua_unit_fragment(repaired_fragment)
            if not repaired_fragment:
                previous_failures.append(
                    f"Targeted repair for unit '{unit_name}' returned an empty fragment."
                )
                continue

            original_fragment = self.sanitize_lua_unit_fragment(str(target_unit.get("code", "")))
            if repaired_fragment.strip() == original_fragment.strip():
                previous_failures.append(
                    f"Targeted repair for unit '{unit_name}' returned an unchanged fragment."
                )
                continue

            updated_units = [dict(unit) for unit in code_units]
            updated_units[target_index] = {
                **updated_units[target_index],
                "code": repaired_fragment,
            }
            rebuilt_code, rebuilt_map, normalized_units = self.assemble_lua_program_from_units(
                updated_units
            )
            repaired_code = self.normalize_lua_code(rebuilt_code)

            if not self._is_material_change(
                repaired_code,
                raw_baseline_code=raw_baseline_code,
                normalized_baseline_code=normalized_baseline_code,
            ):
                previous_failures.append(
                    f"Targeted repair for unit '{unit_name}' did not materially change the assembled file."
                )
                continue

            return {
                "current_code": repaired_code,
                "code_units": normalized_units,
                "code_unit_map": rebuilt_map,
                "repair_notes": [
                    f"[targeted-unit-repair] Repaired code unit '{unit_name}' selected from the execution/test diagnostics.",
                ],
            }

        return None

    def _request_targeted_unit_repair(
        self,
        *,
        defaults: dict,
        diagnostics: dict,
        target_unit: dict,
        code_units: list[dict],
        target_index: int,
        previous_failures: list[str],
    ) -> str:
        context_units = []
        for offset in (-1, 1):
            neighbour_index = target_index + offset
            if 0 <= neighbour_index < len(code_units):
                neighbour = code_units[neighbour_index]
                context_units.append(
                    {
                        "name": neighbour.get("name", ""),
                        "purpose": neighbour.get("purpose", ""),
                        "dependencies": list(neighbour.get("dependencies", []))[:4],
                    }
                )

        response_text = self.ask_text(
            system_prompt=(
                "You are Repair Agent in a local multi-agent pipeline for generating and validating Lua code.\n"
                "Repair only one cohesive Lua code unit inside a larger single-file program.\n"
                "Return only one fenced Lua code block containing the full replacement for the requested unit.\n"
                "Do not return the whole file.\n"
                "Do not include local M = {}, local run_mode = ..., return M, or a top-level main() call.\n"
                "Use standard Lua only.\n"
            ),
            user_prompt=(
                "Repair the requested code unit.\n\n"
                f"Prompt excerpt:\n{self.clip_text(str(defaults['user_prompt']), max_chars=500)}\n\n"
                f"Parsed spec:\n{self.summarize_spec_for_prompt(defaults['parsed_spec'], max_chars=700)}\n\n"
                f"Plan summary:\n{self.summarize_plan_for_prompt(defaults['implementation_plan'], max_chars=650)}\n\n"
                f"Diagnostics:\n{self._serialize_for_prompt(self._compact_diagnostics(diagnostics), max_chars=700)}\n\n"
                f"Target unit:\n{self._serialize_for_prompt(self._compact_unit_descriptor(target_unit), max_chars=450)}\n\n"
                f"Neighboring units:\n{self._serialize_for_prompt(context_units, max_chars=250)}\n\n"
                f"Previous targeted failures:\n{self._serialize_for_prompt(previous_failures[-2:], max_chars=250)}\n\n"
                f"Current unit code:\n{self.clip_text(str(target_unit.get('code', '')), max_chars=2200)}\n\n"
                "Return only ```lua ... ``` for the replacement unit."
            ),
            temperature=0.15,
        )
        return self._parse_targeted_unit_response(response_text)

    def _request_model_repair(
        self,
        *,
        defaults: dict,
        diagnostics: dict,
        repair_history: list[dict],
        current_code: str,
        force_rewrite: bool,
        previous_failures: list[str],
    ) -> dict:
        rewrite_instruction = ""
        if self._looks_like_non_lua_pseudocode(current_code):
            rewrite_instruction += (
                "The current file uses non-Lua or Python-like syntax.\n"
                "Rewrite it as standard Lua instead of trying to preserve the invalid structure.\n\n"
            )
        if force_rewrite:
            rewrite_instruction = (
                f"{rewrite_instruction}"
                "The previous repair attempt was unchanged or insufficient.\n"
                "Return a materially different corrected Lua file.\n"
                "If the smallest valid fix still leaves the file broken, rewrite the file from scratch while preserving the parsed task goal.\n"
                "Do not repeat the same source with formatting-only changes.\n\n"
            )

        response_text = self.ask_text(
            system_prompt=(
                "You are Repair Agent in a local multi-agent pipeline for generating and validating Lua code.\n"
                "Your role: repair the current Lua code based on observed execution errors, lint issues, and failed tests.\n"
                'Return only JSON: {"current_code": "", "repair_notes": [], "status": "CODE_GENERATED"}\n'
                "Important:\n"
                "- Fix only the observed problems.\n"
                "- Preserve the original task goal and implementation intent.\n"
                "- Do not add external dependencies unless explicitly required.\n"
                "- Do not declare success; only produce the repaired Lua code for the next execution cycle.\n"
                "- The repaired file must be materially different if the current file still contains the observed problems."
            ),
            user_prompt=(
                f"{rewrite_instruction}"
                "Repair the current Lua code using the observed diagnostics.\n\n"
                f"Prompt excerpt:\n{self.clip_text(str(defaults['user_prompt']), max_chars=600)}\n\n"
                f"Parsed specification:\n{self.summarize_spec_for_prompt(defaults['parsed_spec'], max_chars=850)}\n\n"
                f"Implementation plan:\n{self.summarize_plan_for_prompt(defaults['implementation_plan'], max_chars=750)}\n\n"
                f"Preferred exported API:\n{self._serialize_for_prompt(self.suggest_api_from_prompt(defaults['user_prompt']), max_chars=400)}\n\n"
                f"Observed diagnostics:\n{self._serialize_for_prompt(self._compact_diagnostics(diagnostics), max_chars=850)}\n\n"
                f"Recent repair history:\n{self._serialize_for_prompt(repair_history[-2:], max_chars=350)}\n\n"
                f"Previous repair attempt failures:\n{self._serialize_for_prompt(previous_failures[-2:], max_chars=250)}\n\n"
                f"Current Lua code focus:\n{self._build_repair_code_focus(current_code, diagnostics, max_chars=2200)}"
            ),
            temperature=0.25 if force_rewrite else 0.0,
        )
        try:
            return self._parse_repair_response(response_text)
        except (ValueError, TypeError) as exc:
            fallback = self._attempt_raw_lua_repair(
                defaults=defaults,
                diagnostics=diagnostics,
                repair_history=repair_history,
                current_code=current_code,
                previous_failures=previous_failures,
                mode_label="repair",
                extra_instruction=(
                    "The previous response was malformed or unusable.\n"
                    "Return only one fenced Lua code block and nothing else.\n"
                ),
            )
            if fallback is not None:
                return fallback
            raise exc

    def _attempt_fallback_regeneration(
        self,
        *,
        defaults: dict,
        diagnostics: dict,
        repair_history: list[dict],
        current_code: str,
        previous_failures: list[str],
    ) -> dict | None:
        if self._count_marked_repairs(repair_history, "[fallback-regeneration]") >= 1:
            return None

        raw_baseline_code = current_code.strip()
        normalized_baseline_code = self.normalize_lua_code(current_code).strip()
        try:
            response_text = self.ask_text(
                system_prompt=(
                    "You are Repair Agent in a local multi-agent pipeline for generating and validating Lua code.\n"
                    "Repeated repair attempts returned unchanged or ineffective code.\n"
                    "Regenerate a corrected Lua solution from scratch using the parsed task, implementation plan, and observed diagnostics.\n"
                    'Return only JSON: {"current_code": "", "repair_notes": [], "status": "CODE_GENERATED"}\n'
                    "Important:\n"
                    "- Produce a fresh, runnable Lua implementation.\n"
                    "- Preserve the original task goal.\n"
                    "- Fix the observed diagnostics directly.\n"
                    "- The new file must be materially different from the current one."
                ),
                user_prompt=(
                    "Fallback regeneration request after repeated ineffective repairs.\n\n"
                    f"Prompt excerpt:\n{self.clip_text(str(defaults['user_prompt']), max_chars=600)}\n\n"
                    f"Parsed specification:\n{self.summarize_spec_for_prompt(defaults['parsed_spec'], max_chars=850)}\n\n"
                    f"Implementation plan:\n{self.summarize_plan_for_prompt(defaults['implementation_plan'], max_chars=750)}\n\n"
                    f"Observed diagnostics:\n{self._serialize_for_prompt(self._compact_diagnostics(diagnostics), max_chars=850)}\n\n"
                    f"Previous repair attempt failures:\n{self._serialize_for_prompt(previous_failures[-3:], max_chars=300)}\n\n"
                    f"Recent repair history:\n{self._serialize_for_prompt(repair_history[-3:], max_chars=350)}\n\n"
                    f"Current Lua code focus:\n{self._build_repair_code_focus(current_code, diagnostics, max_chars=1600)}"
                ),
                temperature=0.35,
            )
            try:
                parsed_response = self._parse_repair_response(response_text)
            except (ValueError, TypeError):
                parsed_response = self._attempt_raw_lua_repair(
                    defaults=defaults,
                    diagnostics=diagnostics,
                    repair_history=repair_history,
                    current_code=current_code,
                    previous_failures=previous_failures,
                    mode_label="fallback-regeneration",
                    extra_instruction=(
                        "Return a fresh Lua implementation.\n"
                        "Return only one fenced Lua code block and nothing else.\n"
                    ),
                )
                if parsed_response is None:
                    return None
        except (LocalModelError, ValueError, TypeError):
            return None

        repaired_code = self.normalize_lua_code(parsed_response["current_code"])
        if not repaired_code or not self._is_material_change(
            repaired_code,
            raw_baseline_code=raw_baseline_code,
            normalized_baseline_code=normalized_baseline_code,
        ):
            return None

        repair_notes = list(parsed_response["repair_notes"])
        repair_notes.insert(
            0,
            "[fallback-regeneration] Regenerated the code after repeated unchanged repair attempts.",
        )
        return {
            "current_code": repaired_code,
            "repair_notes": repair_notes,
        }

    def _attempt_emergency_rewrite(
        self,
        *,
        defaults: dict,
        diagnostics: dict,
        repair_history: list[dict],
        current_code: str,
        attempts: int,
    ) -> dict | None:
        if self._count_emergency_repairs(repair_history) >= 1:
            return None

        raw_baseline_code = current_code.strip()
        normalized_baseline_code = self.normalize_lua_code(current_code).strip()
        try:
            response_text = self.ask_text(
                system_prompt=(
                    "You are Repair Agent in a local multi-agent pipeline for generating and validating Lua code.\n"
                    "The normal repair loop has already exhausted its attempt budget.\n"
                    "Perform one emergency rewrite from scratch using the original parsed task and implementation plan.\n"
                    'Return only JSON: {"current_code": "", "repair_notes": [], "status": "CODE_GENERATED"}\n'
                    "Important:\n"
                    "- Rewrite the Lua file from scratch if necessary.\n"
                    "- Preserve the parsed task goal.\n"
                    "- Address the observed diagnostics directly.\n"
                    "- Produce a materially different code version."
                ),
                user_prompt=(
                    "Emergency rewrite request.\n\n"
                    f"Repair attempt budget exhausted on attempt {attempts}.\n\n"
                    f"Prompt excerpt:\n{self.clip_text(str(defaults['user_prompt']), max_chars=600)}\n\n"
                    f"Parsed specification:\n{self.summarize_spec_for_prompt(defaults['parsed_spec'], max_chars=850)}\n\n"
                    f"Implementation plan:\n{self.summarize_plan_for_prompt(defaults['implementation_plan'], max_chars=750)}\n\n"
                    f"Observed diagnostics:\n{self._serialize_for_prompt(self._compact_diagnostics(diagnostics), max_chars=850)}\n\n"
                    f"Recent repair history:\n{self._serialize_for_prompt(repair_history[-3:], max_chars=350)}\n\n"
                    f"Current Lua code focus:\n{self._build_repair_code_focus(current_code, diagnostics, max_chars=1500)}"
                ),
                temperature=0.45,
            )
            try:
                parsed_response = self._parse_repair_response(response_text)
            except (ValueError, TypeError):
                parsed_response = self._attempt_raw_lua_repair(
                    defaults=defaults,
                    diagnostics=diagnostics,
                    repair_history=repair_history,
                    current_code=current_code,
                    previous_failures=[],
                    mode_label="emergency-rewrite",
                    extra_instruction=(
                        "Return a materially different Lua file rebuilt from scratch.\n"
                        "Return only one fenced Lua code block and nothing else.\n"
                    ),
                )
                if parsed_response is None:
                    return None
        except (LocalModelError, ValueError, TypeError):
            return None

        repaired_code = self.normalize_lua_code(parsed_response["current_code"])
        if not repaired_code or not self._is_material_change(
            repaired_code,
            raw_baseline_code=raw_baseline_code,
            normalized_baseline_code=normalized_baseline_code,
        ):
            return None

        repair_notes = list(parsed_response["repair_notes"])
        repair_notes.insert(0, "[emergency-rewrite] Rebuilt the code after exhausting the normal repair budget.")
        return {
            "current_code": repaired_code,
            "repair_notes": repair_notes,
        }

    def _collect_problem_diagnostics(self, state: dict) -> dict:
        details: list[dict] = []
        summary_parts: list[str] = []

        execution_result = state.get("execution_result", {})
        if isinstance(execution_result, dict):
            execution_status = str(execution_result.get("execution_status", "")).strip()
            if execution_status and execution_status != "success":
                stderr_text = self.clip_text(
                    str(execution_result.get("stderr", "")),
                    max_chars=1800,
                )
                details.append(
                    {
                        "source": "execution_result",
                        "status": execution_status,
                        "stderr": stderr_text,
                        "exit_code": execution_result.get("exit_code"),
                    }
                )
                summary_parts.append(f"Execution failed with status={execution_status}.")

        lint_result = state.get("lint_result", {})
        if isinstance(lint_result, dict):
            lint_status = str(lint_result.get("status", "")).strip().lower()
            lint_issues = self._normalize_notes(lint_result.get("issues"))
            if lint_status == "issues_found" or lint_issues:
                details.append(
                    {
                        "source": "lint_result",
                        "status": lint_status or "issues_found",
                        "issues": lint_issues,
                    }
                )
                summary_parts.append(f"Lint reported {len(lint_issues)} issue(s).")

        test_result = state.get("test_result", {})
        if isinstance(test_result, dict):
            summary = test_result.get("summary", {})
            failed_count = 0
            if isinstance(summary, dict):
                try:
                    failed_count = int(summary.get("failed", 0))
                except (TypeError, ValueError):
                    failed_count = 0

            failed_cases = []
            for case in test_result.get("cases", []):
                if not isinstance(case, dict):
                    continue
                if str(case.get("result", "")).strip().lower() != "failed":
                    continue
                failed_cases.append(
                    {
                        "name": str(case.get("name", "")).strip(),
                        "reason": str(case.get("reason", "")).strip(),
                    }
                )

            if failed_count > 0 or failed_cases:
                details.append(
                    {
                        "source": "test_result",
                        "failed_count": max(failed_count, len(failed_cases)),
                        "failed_cases": failed_cases,
                    }
                )
                summary_parts.append(
                    f"Tests failed: {max(failed_count, len(failed_cases))} case(s)."
                )

        return {
            "has_problems": bool(details),
            "summary": " ".join(summary_parts).strip() or "Observed diagnostics require repair.",
            "details": details,
        }

    def _apply_local_repairs(
        self,
        *,
        current_code: str,
        diagnostics: dict,
    ) -> dict | None:
        repaired_code = current_code
        notes: list[str] = []

        lint_issues = self._extract_lint_issues(diagnostics)
        syntax_text = self._diagnostic_text(diagnostics)
        failed_case_text = self._failed_case_text(diagnostics)

        syntax_fixed_code, syntax_notes = self._repair_non_lua_modifiers(
            repaired_code,
            syntax_text,
        )
        if syntax_notes:
            repaired_code = syntax_fixed_code
            notes.extend(syntax_notes)

        localized_code, localized_names = self._localize_global_symbols(
            repaired_code,
            lint_issues,
        )
        if localized_names:
            repaired_code = localized_code
            notes.append(
                f"Localized top-level Lua symbols to satisfy luacheck: {', '.join(localized_names)}."
            )

        json_fixed_code, json_fix_notes = self._repair_undefined_json_usage(
            repaired_code,
            lint_issues,
        )
        if json_fix_notes:
            repaired_code = json_fixed_code
            notes.extend(json_fix_notes)

        if self._requires_module_entrypoint_repair(failed_case_text):
            moduleized_code = self._moduleize_main_entrypoint(repaired_code)
            if moduleized_code != repaired_code:
                repaired_code = moduleized_code
                notes.append(
                    "Converted the script entry point to a testable M.main module flow with a __test__ guard."
                )

        if self._requires_comment_repair(failed_case_text):
            commented_code = self._ensure_top_level_comment(repaired_code)
            if commented_code != repaired_code:
                repaired_code = commented_code
                notes.append("Added a top-level comment for the main code block.")

        if self._requires_console_input_guard(failed_case_text):
            guarded_code = self._ensure_console_input_guard(repaired_code)
            if guarded_code != repaired_code:
                repaired_code = guarded_code
                notes.append(
                    "Added explicit invalid and empty console-input handling after io.read()."
                )

        if not notes:
            return None

        return {
            "current_code": repaired_code,
            "repair_notes": notes,
        }

    def _parse_repair_response(self, response_text: str) -> dict:
        try:
            payload = self.parse_json_response(response_text)
            if not isinstance(payload, dict):
                raise ValueError("Repair response must be a JSON object.")

            current_code = str(payload.get("current_code", "")).strip()
            if not current_code:
                raise ValueError("Repair response does not contain current_code.")

            repair_notes = self._normalize_notes(payload.get("repair_notes"))
            if not repair_notes:
                repair_notes = self._extract_notes_from_history(payload.get("repair_history"))
            if not repair_notes:
                repair_notes = [
                    "Applied the minimal changes required to address the observed diagnostics."
                ]

            return {
                "current_code": current_code,
                "repair_notes": repair_notes,
            }
        except Exception:
            salvaged = self._salvage_repair_response(response_text)
            if salvaged is not None:
                return salvaged
            raise ValueError(
                "Repair response was neither valid structured JSON nor a recoverable Lua payload."
            )

    def _attempt_raw_lua_repair(
        self,
        *,
        defaults: dict,
        diagnostics: dict,
        repair_history: list[dict],
        current_code: str,
        previous_failures: list[str],
        mode_label: str,
        extra_instruction: str,
    ) -> dict | None:
        try:
            response_text = self.ask_text(
                system_prompt=(
                    "You are Repair Agent in a local multi-agent pipeline for generating and validating Lua code.\n"
                    "Return only one fenced Lua code block.\n"
                    "Do not return JSON, markdown prose, notes, or explanations outside the code fence.\n"
                    "Produce standard runnable Lua.\n"
                ),
                user_prompt=(
                    f"{extra_instruction}\n"
                    f"Prompt excerpt:\n{self.clip_text(str(defaults['user_prompt']), max_chars=500)}\n\n"
                    f"Parsed specification:\n{self.summarize_spec_for_prompt(defaults['parsed_spec'], max_chars=700)}\n\n"
                    f"Implementation plan:\n{self.summarize_plan_for_prompt(defaults['implementation_plan'], max_chars=650)}\n\n"
                    f"Observed diagnostics:\n{self._serialize_for_prompt(self._compact_diagnostics(diagnostics), max_chars=700)}\n\n"
                    f"Recent repair history:\n{self._serialize_for_prompt(repair_history[-2:], max_chars=250)}\n\n"
                    f"Previous failures:\n{self._serialize_for_prompt(previous_failures[-2:], max_chars=200)}\n\n"
                    f"Current Lua code focus:\n{self._build_repair_code_focus(current_code, diagnostics, max_chars=1800)}"
                ),
                temperature=0.2 if mode_label == "repair" else 0.35,
            )
        except (LocalModelError, ValueError, TypeError):
            return None

        repaired_code = self._extract_lua_candidate(response_text)
        if not repaired_code or not self.looks_like_lua_source(repaired_code):
            return None

        return {
            "current_code": repaired_code,
            "repair_notes": [
                f"[{mode_label}-raw-lua-fallback] Recovered by forcing the model to return only a Lua code block.",
            ],
        }

    def _salvage_repair_response(self, response_text: str) -> dict | None:
        repaired_code = self._extract_lua_candidate(response_text)
        if repaired_code and self.looks_like_lua_source(repaired_code):
            return {
                "current_code": repaired_code,
                "repair_notes": [
                    "Recovered Lua code from a malformed structured repair response.",
                ],
            }

        return None

    def _extract_lua_candidate(self, response_text: str) -> str:
        stripped = response_text.strip()

        if "```" in stripped:
            repaired_code = self.extract_code_block(stripped, language="lua")
            if repaired_code and self.looks_like_lua_source(repaired_code):
                return repaired_code

        salvaged_string = self._extract_current_code_from_json_like(stripped)
        if salvaged_string and self.looks_like_lua_source(salvaged_string):
            return salvaged_string

        if not self.looks_like_json_payload(stripped) and self.looks_like_lua_source(stripped):
            return stripped

        return ""

    def _extract_current_code_from_json_like(self, text: str) -> str:
        if not self.looks_like_json_payload(text):
            return ""

        lines = text.strip().splitlines()
        collecting = False
        collected: list[str] = []
        current_code_pattern = re.compile(
            r'(?:"current_code"|\'current_code\'|current_code)\s*:'
        )
        next_key_pattern = re.compile(
            r'^\s*(?:"(?:code_versions|repair_history|repair_notes|status)"|\'(?:code_versions|repair_history|repair_notes|status)\'|(?:code_versions|repair_history|repair_notes|status))\s*:'
        )

        for line in lines:
            if not collecting:
                if not current_code_pattern.search(line):
                    continue
                _, _, tail = line.partition(":")
                candidate = tail.lstrip()
                if candidate:
                    collected.append(candidate)
                collecting = True
                continue

            if next_key_pattern.match(line):
                break
            collected.append(line)

        if not collected:
            return ""

        raw_value = "\n".join(collected).strip().rstrip(",").strip()
        if raw_value[:1] in {'"', "'"}:
            raw_value = raw_value[1:]
        if raw_value[-1:] in {'"', "'"}:
            raw_value = raw_value[:-1]

        raw_value = raw_value.replace('\\"', '"')
        raw_value = raw_value.replace("\\'", "'")
        raw_value = raw_value.replace("\\n", "\n")
        raw_value = raw_value.replace("\\t", "\t")
        raw_value = raw_value.replace("\\r", "")
        raw_value = raw_value.replace("\\\\", "\\")

        return raw_value.strip()

    def _compact_diagnostics(self, diagnostics: dict) -> dict:
        compact_details: list[dict] = []
        for detail in diagnostics.get("details", [])[:3]:
            if not isinstance(detail, dict):
                continue
            compact_detail: dict[str, object] = {}
            source = str(detail.get("source", "")).strip()
            if source:
                compact_detail["source"] = source

            status = str(detail.get("status", "")).strip()
            if status:
                compact_detail["status"] = status

            stderr = str(detail.get("stderr", "")).strip()
            if stderr:
                compact_detail["stderr"] = self.clip_text(stderr, max_chars=350)

            issues = detail.get("issues", [])
            if isinstance(issues, list) and issues:
                compact_detail["issues"] = [str(item).strip() for item in issues[:4] if str(item).strip()]

            failed_cases = detail.get("failed_cases", [])
            if isinstance(failed_cases, list) and failed_cases:
                compact_detail["failed_cases"] = [
                    {
                        "name": str(case.get("name", "")).strip(),
                        "reason": self.clip_text(str(case.get("reason", "")).strip(), max_chars=140),
                    }
                    for case in failed_cases[:4]
                    if isinstance(case, dict)
                ]

            if "exit_code" in detail and detail.get("exit_code") is not None:
                compact_detail["exit_code"] = detail.get("exit_code")

            if "failed_count" in detail:
                compact_detail["failed_count"] = detail.get("failed_count")

            if compact_detail:
                compact_details.append(compact_detail)

        return {
            "summary": self.clip_text(str(diagnostics.get("summary", "")).strip(), max_chars=220),
            "details": compact_details,
        }

    @staticmethod
    def _compact_unit_descriptor(unit: dict) -> dict:
        return {
            "name": str(unit.get("name", "")).strip(),
            "purpose": str(unit.get("purpose", "")).strip(),
            "dependencies": list(unit.get("dependencies", []))[:4]
            if isinstance(unit.get("dependencies", []), list)
            else [],
        }

    def _build_repair_code_focus(
        self,
        current_code: str,
        diagnostics: dict,
        *,
        max_chars: int,
    ) -> str:
        if not current_code.strip():
            return ""

        line_numbers = self._extract_diagnostic_line_numbers(diagnostics)
        if line_numbers:
            excerpt = self._extract_code_excerpt_by_lines(
                current_code,
                line_numbers=line_numbers,
                radius=28,
            )
            if excerpt.strip():
                return self.clip_text(excerpt, max_chars=max_chars)

        return self.clip_text(current_code, max_chars=max_chars)

    @staticmethod
    def _extract_code_excerpt_by_lines(
        current_code: str,
        *,
        line_numbers: list[int],
        radius: int,
    ) -> str:
        lines = current_code.splitlines()
        if not lines:
            return ""

        chunks: list[str] = []
        seen_ranges: list[tuple[int, int]] = []
        total_lines = len(lines)
        for line_number in line_numbers[:2]:
            start = max(1, line_number - radius)
            end = min(total_lines, line_number + radius)
            current_range = (start, end)

            if any(not (end < existing_start or start > existing_end) for existing_start, existing_end in seen_ranges):
                continue
            seen_ranges.append(current_range)

            excerpt_lines = [f"-- excerpt lines {start}-{end}"]
            for index in range(start, end + 1):
                excerpt_lines.append(f"{index:04d}: {lines[index - 1]}")
            chunks.append("\n".join(excerpt_lines))

        return "\n\n".join(chunks)

    def _parse_targeted_unit_response(self, response_text: str) -> str:
        candidate = self.extract_code_block(response_text, language="lua")
        if candidate and (self.looks_like_lua_source(candidate) or "function M.main" in candidate):
            return candidate

        stripped = response_text.strip()
        if not self.looks_like_json_payload(stripped) and (
            self.looks_like_lua_source(stripped) or "function M.main" in stripped
        ):
            return stripped

        raise ValueError("Targeted repair did not return a valid Lua replacement fragment.")

    def _select_target_unit_indexes(
        self,
        *,
        diagnostics: dict,
        code_units: list[dict],
        code_unit_map: list[dict],
    ) -> list[int]:
        if not code_units or not code_unit_map:
            return []

        scores = [0 for _ in code_units]
        line_numbers = self._extract_diagnostic_line_numbers(diagnostics)
        diagnostic_text = self._diagnostic_text(diagnostics)
        failed_case_text = self._failed_case_text(diagnostics)
        identifier_hits = self._extract_diagnostic_identifiers(diagnostics)

        for line_number in line_numbers:
            for index, item in enumerate(code_unit_map):
                start_line = int(item.get("start_line", 0))
                end_line = int(item.get("end_line", 0))
                if start_line <= line_number <= end_line:
                    scores[index] += 10

        for index, unit in enumerate(code_units):
            unit_name = str(unit.get("name", "")).lower()
            unit_purpose = str(unit.get("purpose", "")).lower()
            unit_code = str(unit.get("code", "")).lower()

            name_tokens = [token for token in unit_name.split("_") if len(token) > 2]
            for token in name_tokens:
                if token in diagnostic_text or token in failed_case_text:
                    scores[index] += 3

            purpose_tokens = [
                token
                for token in re.findall(r"[a-zа-я0-9_]+", unit_purpose)
                if len(token) > 4
            ]
            for token in purpose_tokens[:6]:
                if token in diagnostic_text or token in failed_case_text:
                    scores[index] += 1

            for identifier in identifier_hits:
                if identifier in unit_code:
                    scores[index] += 4

        ranked = [
            index
            for index, score in sorted(
                enumerate(scores),
                key=lambda item: item[1],
                reverse=True,
            )
            if score > 0
        ]
        if ranked:
            return ranked

        fallback_indexes = []
        for preferred_name in ("main_entry", "scheduler_core", "core_logic", "io_flow"):
            for index, unit in enumerate(code_units):
                if unit.get("name") == preferred_name and index not in fallback_indexes:
                    fallback_indexes.append(index)
        if fallback_indexes:
            return fallback_indexes

        return [len(code_units) - 1]

    @staticmethod
    def _extract_diagnostic_line_numbers(diagnostics: dict) -> list[int]:
        text_parts = []
        for detail in diagnostics.get("details", []):
            if not isinstance(detail, dict):
                continue
            for value in detail.values():
                if isinstance(value, list):
                    text_parts.extend(str(item) for item in value)
                else:
                    text_parts.append(str(value))
        text = " ".join(text_parts)
        line_numbers: list[int] = []
        patterns = (
            r":(\d+):",
            r"line\s+(\d+)",
            r"строк[ае]\s+(\d+)",
        )
        for pattern in patterns:
            for match in re.findall(pattern, text, flags=re.IGNORECASE):
                try:
                    line_number = int(match)
                except ValueError:
                    continue
                if line_number > 0 and line_number not in line_numbers:
                    line_numbers.append(line_number)
        return line_numbers

    @staticmethod
    def _extract_diagnostic_identifiers(diagnostics: dict) -> list[str]:
        text_parts = []
        for detail in diagnostics.get("details", []):
            if not isinstance(detail, dict):
                continue
            for value in detail.values():
                if isinstance(value, list):
                    text_parts.extend(str(item) for item in value)
                else:
                    text_parts.append(str(value))
        text = " ".join(text_parts).lower()
        identifiers: list[str] = []
        patterns = (
            r"'([a-z_][a-z0-9_]*)'",
            r'"([a-z_][a-z0-9_]*)"',
            r"\bfunction\s+([a-z_][a-z0-9_\.]*)",
            r"\bvariable\s+([a-z_][a-z0-9_]*)",
        )
        for pattern in patterns:
            for match in re.findall(pattern, text):
                normalized = str(match).strip().lower()
                if normalized and normalized not in identifiers:
                    identifiers.append(normalized)
        return identifiers

    @staticmethod
    def _normalize_code_units(value) -> list[dict]:
        if value is None:
            return []
        if not isinstance(value, list):
            value = [value]

        normalized: list[dict] = []
        for entry in value:
            if not isinstance(entry, dict):
                continue
            code = str(entry.get("code", "")).strip()
            if not code:
                continue
            normalized.append(
                {
                    "name": str(entry.get("name", "")).strip(),
                    "purpose": str(entry.get("purpose", "")).strip(),
                    "dependencies": list(entry.get("dependencies", []))
                    if isinstance(entry.get("dependencies", []), list)
                    else [],
                    "code": code,
                }
            )
        return normalized

    @staticmethod
    def _normalize_code_unit_map(value) -> list[dict]:
        if value is None:
            return []
        if not isinstance(value, list):
            value = [value]

        normalized: list[dict] = []
        for entry in value:
            if not isinstance(entry, dict):
                continue
            try:
                start_line = int(entry.get("start_line", 0))
                end_line = int(entry.get("end_line", 0))
            except (TypeError, ValueError):
                continue
            if start_line <= 0 or end_line < start_line:
                continue
            normalized.append(
                {
                    "name": str(entry.get("name", "")).strip(),
                    "purpose": str(entry.get("purpose", "")).strip(),
                    "start_line": start_line,
                    "end_line": end_line,
                }
            )
        return normalized

    @staticmethod
    def _normalize_notes(value) -> list[str]:
        if value is None:
            return []
        if not isinstance(value, list):
            value = [value]

        notes: list[str] = []
        for item in value:
            text = str(item).strip()
            if text and text not in notes:
                notes.append(text)
        return notes

    def _normalize_history(self, value) -> list[dict]:
        if value is None:
            return []
        if not isinstance(value, list):
            value = [value]

        history: list[dict] = []
        for entry in value:
            if isinstance(entry, dict):
                reason = str(entry.get("reason", "")).strip()
                changes = self._normalize_notes(entry.get("changes"))
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

    def _extract_notes_from_history(self, value) -> list[str]:
        history = self._normalize_history(value)
        extracted: list[str] = []
        for entry in history:
            for change in entry.get("changes", []):
                if change not in extracted:
                    extracted.append(change)
        return extracted

    def _count_emergency_repairs(self, repair_history: list[dict]) -> int:
        return self._count_marked_repairs(repair_history, "[emergency-rewrite]")

    @staticmethod
    def _count_marked_repairs(repair_history: list[dict], marker: str) -> int:
        count = 0
        for entry in repair_history:
            if not isinstance(entry, dict):
                continue
            for change in entry.get("changes", []):
                if marker in str(change):
                    count += 1
                    break
        return count

    @staticmethod
    def _is_material_change(
        candidate_code: str,
        *,
        raw_baseline_code: str,
        normalized_baseline_code: str,
    ) -> bool:
        candidate_text = candidate_code.strip()
        if not candidate_text:
            return False
        if candidate_text == raw_baseline_code:
            return False
        if candidate_text != normalized_baseline_code:
            return True
        return raw_baseline_code != normalized_baseline_code

    @staticmethod
    def _looks_like_non_lua_pseudocode(code: str) -> bool:
        patterns = (
            r"(?m)^\s*class\s+[A-Za-z_][A-Za-z0-9_]*\b",
            r"(?m)^\s*def\s+[A-Za-z_][A-Za-z0-9_]*\b",
            r"(?<![A-Za-z0-9_])lambda(?![A-Za-z0-9_])",
            r"(?m)^\s*try\s*:\s*$",
            r"(?m)^\s*except\b",
            r"(?<![A-Za-z0-9_])self\.",
            r"(?<![A-Za-z0-9_])append\s*\(",
            r"\+=",
        )
        return any(re.search(pattern, code) for pattern in patterns)

    @staticmethod
    def _extract_lint_issues(diagnostics: dict) -> list[str]:
        for detail in diagnostics.get("details", []):
            if not isinstance(detail, dict):
                continue
            if detail.get("source") != "lint_result":
                continue
            issues = detail.get("issues", [])
            if isinstance(issues, list):
                return [str(item).strip() for item in issues if str(item).strip()]
        return []

    @staticmethod
    def _failed_case_text(diagnostics: dict) -> str:
        parts: list[str] = []
        for detail in diagnostics.get("details", []):
            if not isinstance(detail, dict):
                continue
            if detail.get("source") != "test_result":
                continue
            for case in detail.get("failed_cases", []):
                if not isinstance(case, dict):
                    continue
                name = str(case.get("name", "")).strip()
                reason = str(case.get("reason", "")).strip()
                if name:
                    parts.append(name)
                if reason:
                    parts.append(reason)
        return " ".join(parts).lower()

    @staticmethod
    def _diagnostic_text(diagnostics: dict) -> str:
        parts: list[str] = []
        for detail in diagnostics.get("details", []):
            if not isinstance(detail, dict):
                continue
            for key in ("status", "stderr"):
                value = str(detail.get(key, "")).strip()
                if value:
                    parts.append(value)
            issues = detail.get("issues", [])
            if isinstance(issues, list):
                for issue in issues:
                    issue_text = str(issue).strip()
                    if issue_text:
                        parts.append(issue_text)
        return " ".join(parts).lower()

    def _localize_global_symbols(
        self,
        code: str,
        lint_issues: list[str],
    ) -> tuple[str, list[str]]:
        updated = code
        localized_names: list[str] = []
        symbol_names: list[str] = []

        patterns = [
            r"non-standard global variable '([A-Za-z_][A-Za-z0-9_]*)'",
            r"undefined variable '([A-Za-z_][A-Za-z0-9_]*)'",
        ]
        for issue in lint_issues:
            for pattern in patterns:
                for name in re.findall(pattern, issue):
                    if name not in symbol_names and name not in {"M", "_G"}:
                        symbol_names.append(name)

        for name in symbol_names:
            function_pattern = rf"(?m)^(\s*)function\s+{re.escape(name)}\s*\("
            function_replacement = rf"\1local function {name}("
            candidate, count = re.subn(function_pattern, function_replacement, updated, count=1)
            if count:
                updated = candidate
                localized_names.append(name)
                continue

            assign_pattern = rf"(?m)^(\s*){re.escape(name)}\s*="
            assign_replacement = rf"\1local {name} ="
            candidate, count = re.subn(assign_pattern, assign_replacement, updated, count=1)
            if count:
                updated = candidate
                localized_names.append(name)

        return updated, localized_names

    @staticmethod
    def _repair_non_lua_modifiers(
        code: str,
        diagnostic_text: str,
    ) -> tuple[str, list[str]]:
        updated = code
        notes: list[str] = []

        needs_modifier_fix = any(
            marker in diagnostic_text
            for marker in (
                "syntax error near 'function'",
                "expected '=' near 'function'",
                "expected '=' near \"function\"",
            )
        ) or bool(
            re.search(
                r"(?m)^\s*(global|public|private|protected|export)\s+(?:local\s+)?function\b",
                code,
            )
        )
        if needs_modifier_fix:
            candidate = re.sub(
                r"(?m)^(\s*)(global|public|private|protected|export)\s+local\s+function\b",
                r"\1local function",
                updated,
            )
            candidate = re.sub(
                r"(?m)^(\s*)(global|public|private|protected|export)\s+function\b",
                r"\1function",
                candidate,
            )
            if candidate != updated:
                updated = candidate
                notes.append(
                    "Removed non-Lua visibility modifiers from function declarations to restore valid Lua syntax."
                )

        typed_syntax_detected = any(
            marker in diagnostic_text
            for marker in (
                "expected '=' near",
                "syntax error near ':'",
            )
        ) or bool(
            re.search(r"(?m)^\s*type\s+[A-Za-z_][A-Za-z0-9_]*\s*=", updated)
        ) or bool(
            re.search(r"(?m)^\s*local\s+[A-Za-z_][A-Za-z0-9_]*\s*:\s*", updated)
        ) or bool(
            re.search(r"([,(]\s*[A-Za-z_][A-Za-z0-9_]*)\s*:\s*[^,)=]+", updated)
        )
        if typed_syntax_detected:
            candidate = BaseAgent.normalize_lua_code(updated)
            if candidate != updated:
                updated = candidate
                notes.append(
                    "Removed non-standard typed-Lua syntax so the file stays compatible with standard Lua."
                )

        return updated, notes

    @staticmethod
    def _repair_undefined_json_usage(
        code: str,
        lint_issues: list[str],
    ) -> tuple[str, list[str]]:
        json_issue = any("undefined variable 'json'" in issue.lower() for issue in lint_issues)
        if not json_issue and "json.encode" not in code and "json.decode" not in code:
            return code, []

        updated = code.replace("json.encode", "encode_state").replace("json.decode", "decode_state")
        if updated == code:
            return code, []

        helpers = (
            "local function encode_state(value)\n"
            "    if type(value) ~= 'table' then\n"
            "        return tostring(value)\n"
            "    end\n"
            "    local parts = {}\n"
            "    for key, item in pairs(value) do\n"
            "        parts[#parts + 1] = tostring(key) .. '=' .. tostring(item)\n"
            "    end\n"
            "    table.sort(parts)\n"
            "    return table.concat(parts, '\\n')\n"
            "end\n\n"
            "local function decode_state(text)\n"
            "    local result = {}\n"
            "    for line in string.gmatch(text or '', '[^\\r\\n]+') do\n"
            "        local key, value = line:match('^([^=]+)=(.*)$')\n"
            "        if key then\n"
            "            local numeric = tonumber(value)\n"
            "            result[key] = numeric ~= nil and numeric or value\n"
            "        end\n"
            "    end\n"
            "    return result\n"
            "end\n\n"
        )

        if "local function encode_state" not in updated:
            anchor = "local M = {}\n\n"
            if anchor in updated:
                updated = updated.replace(anchor, anchor + helpers, 1)
            else:
                updated = helpers + updated

        return updated, [
            "Replaced unsupported json.encode/json.decode usage with local standard-Lua save/load helpers."
        ]

    @staticmethod
    def _requires_module_entrypoint_repair(failed_case_text: str) -> bool:
        markers = (
            "target.main",
            "target must be available",
            "test mode",
            "__test__",
        )
        return any(marker in failed_case_text for marker in markers)

    @staticmethod
    def _requires_comment_repair(failed_case_text: str) -> bool:
        return "comment" in failed_case_text or "комментар" in failed_case_text

    @staticmethod
    def _requires_console_input_guard(failed_case_text: str) -> bool:
        markers = (
            "invalid input handling path is present",
            "empty input handling path is present",
            "invalid-input handling",
            "empty-input handling",
        )
        return any(marker in failed_case_text for marker in markers)

    def _moduleize_main_entrypoint(self, code: str) -> str:
        updated = code
        if re.search(r"(?m)^\s*local\s+function\s+main\s*\(", updated):
            updated = re.sub(
                r"(?m)^(\s*)local\s+function\s+main\s*\(",
                r"\1function M.main(",
                updated,
                count=1,
            )
        elif re.search(r"(?m)^\s*function\s+main\s*\(", updated):
            updated = re.sub(
                r"(?m)^(\s*)function\s+main\s*\(",
                r"\1function M.main(",
                updated,
                count=1,
            )

        if "function M.main(" not in updated:
            return code

        if not re.search(r"(?m)^\s*main\s*\(\s*\)\s*$", updated):
            return self.normalize_lua_code(updated)

        updated = re.sub(
            r"(?m)^\s*main\s*\(\s*\)\s*$",
            "if run_mode ~= '__test__' then\n    M.main()\nend",
            updated,
        )
        return self.normalize_lua_code(updated)

    @staticmethod
    def _ensure_top_level_comment(code: str) -> str:
        if any(line.strip().startswith("--") for line in code.splitlines()):
            return code
        return "-- Main application flow\n" + code

    @staticmethod
    def _ensure_console_input_guard(code: str) -> str:
        pattern = re.compile(
            r"(?m)^(\s*)local\s+([A-Za-z_][A-Za-z0-9_]*)\s*=\s*io\.read\([^)]*\)\s*$"
        )
        match = pattern.search(code)
        if not match:
            return code

        variable_name = match.group(2)
        guard_pattern = rf"if\s+not\s+{re.escape(variable_name)}(?:\s+or\s+{re.escape(variable_name)}\s*==\s*['\"]['\"])?"
        if re.search(guard_pattern, code):
            return code

        insertion = (
            f"{match.group(0)}\n"
            f"{match.group(1)}if not {variable_name} or {variable_name} == '' then\n"
            f"{match.group(1)}    print('No input provided')\n"
            f"{match.group(1)}    return\n"
            f"{match.group(1)}end"
        )
        return pattern.sub(insertion, code, count=1)

    def _build_version_entry(self, existing_versions: list, *, notes: list[str]) -> dict:
        highest_version = 0
        for item in existing_versions:
            if not isinstance(item, dict):
                continue
            try:
                highest_version = max(highest_version, int(item.get("version", 0)))
            except (TypeError, ValueError):
                continue

        return {
            "version": highest_version + 1,
            "source": "repair",
            "notes": notes,
        }

    def _serialize_for_prompt(self, payload, *, max_chars: int) -> str:
        return self.clip_text(self.to_prompt_json(payload), max_chars=max_chars)


AGENT_CLASS = RepairCodeAgentV1
