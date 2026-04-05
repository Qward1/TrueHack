import re

from agents.base import BaseAgent
from state import (
    STATUS_EXECUTED,
    STATUS_FAILED,
    STATUS_REPAIR_NEEDED,
    STATUS_TESTED,
    build_failure_result,
)


class TestCodeAgentV1(BaseAgent):
    role = "test_code"
    version = "v1"

    def run(self, state):
        defaults = self.with_defaults(state)
        attempts = defaults["test_attempts"] + 1
        current_status = str(defaults["status"])
        current_code = str(defaults["current_code"]).strip()
        execution_result = defaults["execution_result"]

        if current_status != STATUS_EXECUTED:
            return self._build_testing_failure(
                state,
                attempts=attempts,
                reason=(
                    f"Test Agent expects status={STATUS_EXECUTED}, "
                    f"got {current_status or '<empty>'}."
                ),
            )

        if not current_code:
            return self._build_testing_failure(
                state,
                attempts=attempts,
                reason="Test Agent requires current_code to be present.",
            )

        execution_error = self._validate_execution_result(execution_result)
        if execution_error:
            return self._build_testing_failure(
                state,
                attempts=attempts,
                reason=execution_error,
            )

        parsed_spec = self._normalize_spec(defaults["parsed_spec"])
        implementation_plan = self._normalize_plan(defaults["implementation_plan"])
        test_plan_outline = self._normalize_test_plan(defaults["test_plan_outline"])
        combined_text = self._build_combined_text(
            defaults["user_prompt"],
            parsed_spec,
            implementation_plan,
            test_plan_outline,
        )
        requirement_text = self._build_requirement_text(
            defaults["user_prompt"],
            parsed_spec,
            implementation_plan,
        )

        cases = [
            self._make_case(
                "Execution stage completed successfully",
                passed=True,
                reason="execution_result.execution_status is success.",
            )
        ]
        runtime_case = self._run_runtime_validation(current_code, combined_text)
        if runtime_case is not None:
            cases.append(runtime_case)

        cases.extend(
            self._build_constraint_cases(
                user_prompt=str(defaults["user_prompt"]),
                parsed_spec=parsed_spec,
                test_plan_outline=test_plan_outline,
                code=current_code,
                requirement_text=requirement_text,
            )
        )
        cases.extend(
            self._build_feature_cases(
                parsed_spec=parsed_spec,
                test_plan_outline=test_plan_outline,
                code=current_code,
                requirement_text=requirement_text,
            )
        )
        cases.extend(
            self._build_input_handling_cases(
                parsed_spec=parsed_spec,
                test_plan_outline=test_plan_outline,
                code=current_code,
                combined_text=combined_text,
            )
        )

        cases = self._deduplicate_cases(cases)
        test_result = self._build_test_result(cases)
        tests_passed = test_result["summary"]["failed"] == 0

        return {
            "test_attempts": attempts,
            "tests_passed": tests_passed,
            "test_result": test_result,
            "status": STATUS_TESTED if tests_passed else STATUS_REPAIR_NEEDED,
        }

    def _build_testing_failure(
        self,
        state,
        *,
        attempts: int,
        reason: str,
    ) -> dict:
        failure = build_failure_result(state, reason)
        return {
            **failure,
            "test_attempts": attempts,
            "test_result": {
                "summary": {"total": 0, "passed": 0, "failed": 0},
                "cases": [],
                "stderr": reason,
            },
            "status": STATUS_FAILED,
            "tests_passed": False,
        }

    @staticmethod
    def _validate_execution_result(execution_result) -> str | None:
        if not isinstance(execution_result, dict) or not execution_result:
            return "Test Agent requires execution_result to be present."

        execution_status = str(execution_result.get("execution_status", "")).strip()
        if execution_status != "success":
            return (
                "Test Agent requires execution_result.execution_status to be success, "
                f"got {execution_status or '<empty>'}."
            )
        return None

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

    def _normalize_test_plan(self, test_plan_outline) -> dict:
        if not isinstance(test_plan_outline, dict):
            test_plan_outline = {}
        return {
            "normal_cases": self._normalize_list(test_plan_outline.get("normal_cases")),
            "edge_cases": self._normalize_list(test_plan_outline.get("edge_cases")),
            "invalid_input_cases": self._normalize_list(test_plan_outline.get("invalid_input_cases")),
        }

    @staticmethod
    def _build_combined_text(
        user_prompt: str,
        parsed_spec: dict,
        implementation_plan: dict,
        test_plan_outline: dict,
    ) -> str:
        parts = [user_prompt, parsed_spec.get("goal", ""), implementation_plan.get("goal", "")]
        for key in ("inputs", "outputs", "constraints", "assumptions", "success_criteria"):
            parts.extend(parsed_spec.get(key, []))
        for key in ("steps", "components", "constraints", "assumptions"):
            parts.extend(implementation_plan.get(key, []))
        for key in ("normal_cases", "edge_cases", "invalid_input_cases"):
            parts.extend(test_plan_outline.get(key, []))
        return " ".join(part.lower() for part in parts if part)

    @staticmethod
    def _build_requirement_text(
        user_prompt: str,
        parsed_spec: dict,
        implementation_plan: dict,
    ) -> str:
        parts = [user_prompt, parsed_spec.get("goal", ""), implementation_plan.get("goal", "")]
        for key in ("inputs", "outputs", "constraints", "success_criteria"):
            parts.extend(parsed_spec.get(key, []))
        for key in ("components", "constraints"):
            parts.extend(implementation_plan.get(key, []))
        return " ".join(part.lower() for part in parts if part)

    def _run_runtime_validation(self, code: str, combined_text: str) -> dict | None:
        lowered_code = code.lower()
        should_run = any(
            marker in lowered_code
            for marker in ("__test__", "return m", "function m.", "local m = {}")
        )
        if not should_run:
            return None

        assertions = [
            "assert(target ~= nil, 'target must be available in test mode')",
        ]
        if any(marker in combined_text for marker in ("console", "консол", "input", "ввод", "menu", "меню")):
            assertions.append(
                "if type(target) == 'table' and target.main ~= nil then "
                "assert(type(target.main) == 'function', 'target.main must be a function') "
                "end"
            )

        runtime_result = self.lua_toolchain.run_tests(code, "\n".join(assertions))
        if runtime_result["success"]:
            return self._make_case(
                "Test-mode validation completed",
                passed=True,
                reason="The Lua code loaded successfully in test mode.",
            )

        stderr_text = str(runtime_result.get("stderr", "")).strip() or "Unknown test-mode failure."
        return self._make_case(
            "Test-mode validation completed",
            passed=False,
            reason=stderr_text,
        )

    def _build_constraint_cases(
        self,
        *,
        user_prompt: str,
        parsed_spec: dict,
        test_plan_outline: dict,
        code: str,
        requirement_text: str,
    ) -> list[dict]:
        cases: list[dict] = []
        code_lower = code.lower()
        line_count = len(code.splitlines())
        minimum_lines = self.extract_minimum_line_count(user_prompt)

        if minimum_lines is not None:
            passed = line_count >= minimum_lines
            cases.append(
                self._make_case(
                    "Minimum line-count requirement",
                    passed=passed,
                    reason=(
                        f"Observed {line_count} lines; required at least {minimum_lines}."
                    ),
                )
            )

        comment_required = self._requires_comments(requirement_text)
        if comment_required:
            comment_lines = sum(
                1 for line in code.splitlines() if line.strip().startswith("--")
            )
            minimum_comments = 3 if line_count >= 60 else 1
            passed = comment_lines >= minimum_comments
            cases.append(
                self._make_case(
                    "Major code blocks are commented",
                    passed=passed,
                    reason=(
                        f"Observed {comment_lines} Lua comment lines; "
                        f"required at least {minimum_comments}."
                    ),
                )
            )

        standard_lua_required = any(
            marker in requirement_text
            for marker in (
                "без сторонних библиотек",
                "without external libraries",
                "no external libraries",
                "use only standard lua",
            )
        )
        if standard_lua_required:
            requires = re.findall(r"\brequire\s*\(\s*['\"]([^'\"]+)['\"]\s*\)", code_lower)
            passed = not requires
            cases.append(
                self._make_case(
                    "No external Lua dependencies are referenced",
                    passed=passed,
                    reason=(
                        "No require(...) calls detected."
                        if passed
                        else f"Detected require(...) references: {', '.join(requires[:5])}."
                    ),
                )
            )

        standalone_required = any(
            marker in requirement_text for marker in ("standalone", "автоном", "полностью автоном")
        )
        if standalone_required:
            file_load_calls = re.findall(r"\b(require|dofile|loadfile)\s*\(", code_lower)
            passed = not file_load_calls
            cases.append(
                self._make_case(
                    "Standalone execution structure",
                    passed=passed,
                    reason=(
                        "The source does not reference extra Lua files or modules."
                        if passed
                        else "The source references additional files or modules."
                    ),
                )
            )

        if any(marker in requirement_text for marker in ("console", "консол")):
            passed = any(marker in code_lower for marker in ("print(", "io.write", "io.read", "m.main"))
            cases.append(
                self._make_case(
                    "Console interaction path exists",
                    passed=passed,
                    reason=(
                        "Detected console input/output markers in the source."
                        if passed
                        else "The source does not expose an obvious console interaction path."
                    ),
                )
            )

        if parsed_spec["success_criteria"] or test_plan_outline["normal_cases"]:
            cases.append(
                self._make_case(
                    "Primary normal-flow scenario remains represented",
                    passed=True,
                    reason="The script executed successfully and a normal-flow plan is present.",
                )
            )

        return cases

    def _build_feature_cases(
        self,
        *,
        parsed_spec: dict,
        test_plan_outline: dict,
        code: str,
        requirement_text: str,
    ) -> list[dict]:
        cases: list[dict] = []
        code_lower = code.lower()

        any_input_cases = bool(test_plan_outline["edge_cases"] or test_plan_outline["invalid_input_cases"])

        feature_rules = [
            (
                "Persistence system is present",
                ("save", "load", "persist", "serialization", "serialize", "deserialize", "сохран", "загруз"),
                ("save", "load", "io.open"),
            ),
            (
                "Inventory system is present",
                ("inventory", "инвентар"),
                ("inventory", "item"),
            ),
            (
                "Shop or trading system is present",
                ("shop", "магаз", "торгов"),
                ("shop", "buy", "sell"),
            ),
            (
                "Quest system is present",
                ("quest", "квест", "задан"),
                ("quest",),
            ),
            (
                "Combat system is present",
                ("battle", "combat", "бой", "enemy", "враг"),
                ("battle", "attack", "enemy"),
            ),
            (
                "Location system is present",
                (
                    "location",
                    "локац",
                    "forest",
                    "cave",
                    "village",
                    "ruins",
                    "camp",
                    "лес",
                    "пещер",
                    "деревн",
                    "руин",
                    "лагер",
                ),
                ("location", "forest", "cave", "village", "ruins", "camp"),
            ),
            (
                "Leveling or progression system is present",
                ("level", "уров", "experience", "опыт", "xp"),
                ("level", "experience", "xp"),
            ),
            (
                "Random event logic is present",
                ("random", "случайн", "event", "событ"),
                ("math.random", "random", "event"),
            ),
            (
                "Priority queue subsystem is present",
                ("priority queue", "приоритет"),
                ("priorityqueue", "priority_queue", "heap", "enqueue", "dequeue"),
            ),
            (
                "Scheduler subsystem is present",
                ("scheduler", "task scheduler", "планировщик"),
                ("scheduler", "schedule", "dispatch", "tick", "run_loop"),
            ),
            (
                "Event bus subsystem is present",
                ("event bus", "event-driven", "событ"),
                ("eventbus", "event_bus", "subscribe", "publish", "emit"),
            ),
            (
                "Retry or backoff subsystem is present",
                ("retry", "backoff", "повтор"),
                ("retry", "backoff", "attempt", "max_attempt"),
            ),
            (
                "Timer subsystem is present",
                ("timer", "таймер", "delayed task", "periodic"),
                ("timer", "delay", "interval", "next_run", "periodic"),
            ),
            (
                "Logging subsystem is present",
                ("logging", "logger", "лог"),
                ("logger", "log", "debug", "info", "error"),
            ),
            (
                "Config subsystem is present",
                ("config", "конфиг", "configuration"),
                ("config",),
            ),
            (
                "Coroutine-based execution is present",
                ("coroutine", "coroutine-based", "корутин"),
                ("coroutine.create", "coroutine.resume", "coroutine.yield", "coroutine.status"),
            ),
        ]

        for name, prompt_markers, code_markers in feature_rules:
            if self._contains_requirement(requirement_text, prompt_markers):
                passed = self._contains_any(code_lower, code_markers)
                cases.append(
                    self._make_case(
                        name,
                        passed=passed,
                        reason=(
                            "Observed matching feature markers in the source."
                            if passed
                            else "The source does not contain markers for this required feature."
                        ),
                    )
                )

        stat_rules = [
            ("Player health stat is represented", ("health", "здоров"), ("health", "hp", "здоров")),
            ("Player energy stat is represented", ("energy", "энерг"), ("energy", "stamina", "энерг")),
            ("Player hunger stat is represented", ("hunger", "голод"), ("hunger", "голод")),
            ("Player gold stat is represented", ("gold", "золот"), ("gold", "coin", "золот")),
            ("Player experience stat is represented", ("experience", "опыт", "xp"), ("experience", "xp", "опыт")),
            ("Player level stat is represented", ("level", "уров"), ("level", "уров")),
        ]
        for name, prompt_markers, code_markers in stat_rules:
            if self._contains_requirement(requirement_text, prompt_markers):
                passed = self._contains_any(code_lower, code_markers)
                cases.append(
                    self._make_case(
                        name,
                        passed=passed,
                        reason=(
                            "Observed matching stat markers in the source."
                            if passed
                            else "The required player stat is not clearly represented in the source."
                        ),
                    )
                )

        item_type_rules = [
            ("Food item type is represented", ("food", "еда"), ("food", "еда")),
            ("Potion item type is represented", ("potion", "зель"), ("potion", "зель")),
            ("Weapon item type is represented", ("weapon", "оруж"), ("weapon", "оруж")),
            ("Armor item type is represented", ("armor", "брон"), ("armor", "armour", "брон")),
            ("Artifact item type is represented", ("artifact", "артефакт"), ("artifact", "artefact", "артефакт")),
        ]
        for name, prompt_markers, code_markers in item_type_rules:
            if self._contains_requirement(requirement_text, prompt_markers):
                passed = self._contains_any(code_lower, code_markers)
                cases.append(
                    self._make_case(
                        name,
                        passed=passed,
                        reason=(
                            "Observed matching item-type markers in the source."
                            if passed
                            else "The required item type is not clearly represented in the source."
                        ),
                    )
                )

        if any_input_cases and self._contains_requirement(requirement_text, ("escape", "сбежать", "defend", "защит")):
            passed = self._contains_any(code_lower, ("escape", "flee", "defend", "guard", "защит", "сбежать"))
            cases.append(
                self._make_case(
                    "Alternative combat actions are represented",
                    passed=passed,
                    reason=(
                        "Observed defend/escape markers in the source."
                        if passed
                        else "The source does not clearly represent defend or escape combat actions."
                    ),
                )
            )

        if self._contains_requirement(requirement_text, ("metatable", "metatables")):
            passed = self._contains_any(code_lower, ("setmetatable",))
            cases.append(
                self._make_case(
                    "Metatable-based design is present",
                    passed=passed,
                    reason=(
                        "Observed metatable usage in the source."
                        if passed
                        else "The source does not clearly use metatables where they were requested."
                    ),
                )
            )

        return cases

    def _build_input_handling_cases(
        self,
        *,
        parsed_spec: dict,
        test_plan_outline: dict,
        code: str,
        combined_text: str,
    ) -> list[dict]:
        cases: list[dict] = []
        code_lower = code.lower()
        input_requirement_text = " ".join(
            [parsed_spec.get("goal", ""), *parsed_spec["inputs"]]
        ).lower()
        requires_input_handling = bool(parsed_spec["inputs"]) or self._contains_requirement(
            input_requirement_text,
            ("input", "ввод", "io.read", "user input"),
        )
        if not requires_input_handling:
            return cases

        invalid_markers = (
            "if not ",
            "== nil",
            "or 0",
            "or \"\"",
            "tonumber(",
            "type(",
            "math.max(",
            "math.min(",
        )
        invalid_input_passed = self._contains_any(code_lower, invalid_markers)
        cases.append(
            self._make_case(
                "Invalid input handling path is present",
                passed=invalid_input_passed,
                reason=(
                    "Observed defensive input-handling markers in the source."
                    if invalid_input_passed
                    else "The source does not show clear invalid-input handling."
                ),
            )
        )

        if self._contains_any(input_requirement_text, ("empty", "пуст", "missing", "отсутств")):
            empty_markers = ("== ''", '== ""', "not input", "not choice", "not command", "or nil")
            empty_input_passed = self._contains_any(code_lower, empty_markers)
            cases.append(
                self._make_case(
                    "Empty input handling path is present",
                    passed=empty_input_passed,
                    reason=(
                        "Observed empty-input checks in the source."
                        if empty_input_passed
                        else "The source does not show clear empty-input handling."
                    ),
                )
            )

        return cases

    @staticmethod
    def _make_case(name: str, *, passed: bool, reason: str) -> dict:
        return {
            "name": name,
            "result": "passed" if passed else "failed",
            "reason": reason,
        }

    @staticmethod
    def _deduplicate_cases(cases: list[dict]) -> list[dict]:
        deduplicated: list[dict] = []
        seen_names: set[str] = set()
        for case in cases:
            name = str(case.get("name", "")).strip()
            if not name or name in seen_names:
                continue
            seen_names.add(name)
            deduplicated.append(case)
        return deduplicated

    @staticmethod
    def _contains_any(text: str, markers: tuple[str, ...] | list[str]) -> bool:
        return any(marker in text for marker in markers)

    @staticmethod
    def _contains_requirement(text: str, markers: tuple[str, ...] | list[str]) -> bool:
        for marker in markers:
            normalized = marker.lower()
            if re.fullmatch(r"[a-z0-9_]+", normalized):
                pattern = rf"(?<![a-z0-9_]){re.escape(normalized)}(?![a-z0-9_])"
                if re.search(pattern, text):
                    return True
            elif normalized in text:
                return True
        return False

    @classmethod
    def _requires_comments(cls, text: str) -> bool:
        negative_markers = (
            "без комментар",
            "без бессмысленных комментар",
            "no comment",
            "without comment",
            "no meaningless comment",
            "without meaningless comment",
        )
        if any(marker in text for marker in negative_markers):
            return False
        return cls._contains_requirement(
            text,
            (
                "добавь комментар",
                "добавить комментар",
                "с комментар",
                "add comment",
                "include comment",
                "with comment",
            ),
        )

    def _build_test_result(self, cases: list[dict]) -> dict:
        passed = sum(1 for case in cases if case["result"] == "passed")
        failed_cases = [case for case in cases if case["result"] == "failed"]
        failure_text = "\n".join(
            f"{case['name']}: {case['reason']}" for case in failed_cases
        )
        return {
            "summary": {
                "total": len(cases),
                "passed": passed,
                "failed": len(failed_cases),
            },
            "cases": cases,
            "stderr": failure_text,
        }


AGENT_CLASS = TestCodeAgentV1
