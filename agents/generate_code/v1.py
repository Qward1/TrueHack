import re

from agents.base import BaseAgent
from llm_client import LocalModelError
from state import STATUS_CODE_GENERATED, STATUS_FAILED, STATUS_PLANNED, build_failure_result


class GenerateCodeAgentV1(BaseAgent):
    role = "generate_code"
    version = "v1"

    def run(self, state):
        defaults = self.with_defaults(state)
        current_status = str(defaults["status"])
        parsed_spec = defaults["parsed_spec"]
        implementation_plan = defaults["implementation_plan"]

        if current_status != STATUS_PLANNED:
            failure = build_failure_result(
                state,
                f"Code Generator Agent expects status={STATUS_PLANNED}, got {current_status or '<empty>'}.",
            )
            return {
                **failure,
                "generation_notes": [
                    "Generation aborted because the state precondition was not satisfied.",
                ],
                "status": STATUS_FAILED,
            }

        plan_error = self._validate_inputs(parsed_spec, implementation_plan)
        if plan_error:
            failure = build_failure_result(state, plan_error)
            return {
                **failure,
                "generation_notes": [
                    "Generation failed because the planning data is missing or inconsistent.",
                    plan_error,
                ],
                "status": STATUS_FAILED,
            }

        try:
            parsed_response = self._generate_best_candidate(defaults)
        except (LocalModelError, ValueError, TypeError) as exc:
            failure = build_failure_result(
                state,
                f"Code generation failed: {exc}",
            )
            return {
                **failure,
                "generation_notes": [
                    "Generation failed because the local model response was unavailable or invalid.",
                    str(exc),
                ],
                "status": STATUS_FAILED,
            }

        normalized_code = self.normalize_lua_code(parsed_response["current_code"])
        generation_notes = parsed_response["generation_notes"]
        version_entry = self._build_version_entry(
            defaults["code_versions"],
            notes=generation_notes,
        )

        return {
            "current_code": normalized_code,
            "code_unit_plan": list(parsed_response.get("code_unit_plan", [])),
            "code_units": list(parsed_response.get("code_units", [])),
            "code_unit_map": list(parsed_response.get("code_unit_map", [])),
            "code_versions": [*defaults["code_versions"], version_entry],
            "generation_notes": generation_notes,
            "execution_ok": False,
            "tests_passed": False,
            "execution_result": {},
            "test_result": {},
            "status": STATUS_CODE_GENERATED,
        }

    def _generate_best_candidate(self, defaults: dict) -> dict:
        unit_plan = self._build_code_unit_plan(defaults)
        best_candidate: dict | None = None
        best_score: tuple[int, int, int] | None = None
        collected_errors: list[str] = []

        try:
            unit_candidate = self._attempt_unit_based_generation(
                defaults=defaults,
                unit_plan=unit_plan,
            )
            unit_issues = list(unit_candidate.pop("quality_issues", []))
            unit_score = self._score_candidate(unit_candidate["current_code"], unit_issues)
            best_candidate = unit_candidate
            best_score = unit_score
            if not unit_issues:
                return unit_candidate
            collected_errors.extend(unit_issues[:6])
        except (LocalModelError, ValueError, TypeError) as exc:
            collected_errors.append(f"Function-by-function generation failed: {exc}")

        monolithic_issues: list[str] = []
        try:
            monolithic_candidate = self._generate_monolithic_candidate(
                defaults=defaults,
                seed_feedback=collected_errors[:8],
            )
            monolithic_issues = list(monolithic_candidate.pop("quality_issues", []))
            monolithic_score = self._score_candidate(
                monolithic_candidate["current_code"],
                monolithic_issues,
            )

            if best_candidate is None or best_score is None or monolithic_score < best_score:
                best_candidate = monolithic_candidate
                best_score = monolithic_score
        except Exception as exc:
            collected_errors.append(f"Monolithic fallback failed: {exc}")

        if best_candidate is None:
            raise ValueError("The generator could not produce any Lua code candidate.")

        residual_issues = monolithic_issues if best_candidate.get("code_units") == [] else collected_errors[:6]
        if residual_issues:
            best_candidate["generation_notes"] = [
                *best_candidate["generation_notes"],
                "Generation completed with residual issues that may need execution/test-driven repair: "
                + " | ".join(dict.fromkeys(residual_issues[:6])),
            ]
        return best_candidate

    def _attempt_unit_based_generation(
        self,
        *,
        defaults: dict,
        unit_plan: list[dict],
    ) -> dict:
        if not unit_plan:
            raise ValueError("Unit-based generation did not produce a usable unit plan.")

        built_units: list[dict] = []
        unit_failures: list[str] = []

        for unit_spec in unit_plan:
            unit_code, unit_notes = self._generate_single_unit(
                defaults=defaults,
                unit_spec=unit_spec,
                built_units=built_units,
                recent_failures=unit_failures,
            )
            built_units.append(
                {
                    "name": unit_spec["name"],
                    "purpose": unit_spec["purpose"],
                    "dependencies": list(unit_spec.get("dependencies", [])),
                    "code": unit_code,
                }
            )
            unit_failures.extend(unit_notes[-2:])

        assembled_code, code_unit_map, normalized_units = self.assemble_lua_program_from_units(
            built_units
        )
        quality_issues = self._assess_generated_code(
            defaults=defaults,
            code=assembled_code,
        )
        generation_notes = [
            "Used staged function-by-function generation and assembled the final Lua file from cohesive code units.",
            f"Generated {len(normalized_units)} code units before assembling the final program.",
        ]
        if quality_issues:
            generation_notes.append(
                "Unit-based generation still left some quality issues before execution: "
                + " | ".join(quality_issues[:6])
            )

        return {
            "current_code": assembled_code,
            "code_unit_plan": unit_plan,
            "code_units": normalized_units,
            "code_unit_map": code_unit_map,
            "generation_notes": generation_notes,
            "quality_issues": quality_issues,
        }

    def _generate_single_unit(
        self,
        *,
        defaults: dict,
        unit_spec: dict,
        built_units: list[dict],
        recent_failures: list[str],
    ) -> tuple[str, list[str]]:
        best_code = ""
        best_notes: list[str] = []
        best_issue_count: int | None = None
        feedback = list(recent_failures[-2:])

        for attempt_index, temperature in enumerate((0.0, 0.15), start=1):
            response_text = self.ask_text(
                system_prompt=self._build_unit_generation_system_prompt(),
                user_prompt=self._build_unit_generation_user_prompt(
                    defaults=defaults,
                    unit_spec=unit_spec,
                    built_units=built_units,
                    attempt_index=attempt_index,
                    feedback=feedback,
                ),
                temperature=temperature,
            )
            unit_code = self._parse_unit_generation_response(response_text)
            unit_code = self.sanitize_lua_unit_fragment(unit_code)
            issues = self._assess_unit_fragment(unit_spec=unit_spec, code=unit_code)
            notes = [
                f"Generated unit '{unit_spec['name']}' on attempt {attempt_index}.",
            ]
            if issues:
                notes.append(
                    "Unit refinement requested another pass: " + " | ".join(issues[:4])
                )
            if best_issue_count is None or len(issues) < best_issue_count:
                best_code = unit_code
                best_notes = notes
                best_issue_count = len(issues)
            if not issues and unit_code:
                return unit_code, notes
            feedback = issues

        if best_code:
            return best_code, best_notes

        raise ValueError(f"Could not generate a valid Lua fragment for unit '{unit_spec['name']}'.")

    @staticmethod
    def _build_unit_generation_system_prompt() -> str:
        return (
            "You are Code Generator Agent in a local multi-agent pipeline for generating and validating Lua code.\n"
            "Generate one cohesive code unit for a larger single-file Lua program.\n"
            "Return only one fenced Lua code block and nothing else.\n"
            "Important:\n"
            "- Return only the code for the requested unit, not the whole file.\n"
            "- Do not return local M = {}, local run_mode = ..., return M, or a top-level main() call.\n"
            "- Use standard Lua only.\n"
            "- No pseudocode, placeholders, TODOs, or Python-like syntax.\n"
        )

    def _build_unit_generation_user_prompt(
        self,
        *,
        defaults: dict,
        unit_spec: dict,
        built_units: list[dict],
        attempt_index: int,
        feedback: list[str],
    ) -> str:
        feedback_block = ""
        if feedback:
            feedback_block = (
                "Fix these issues in this unit now:\n"
                f"{self._serialize_for_prompt(feedback[:6], max_chars=700)}\n\n"
            )

        existing_context = ""
        if built_units:
            existing_context = (
                "Units already generated:\n"
                f"{self._serialize_for_prompt(self._summarize_units_for_prompt(built_units), max_chars=1800)}\n\n"
            )

        return (
            "Generate the requested Lua unit.\n\n"
            f"Attempt: {attempt_index}\n\n"
            f"Raw user prompt excerpt:\n{self.clip_text(str(defaults['user_prompt']), max_chars=1200)}\n\n"
            f"Generation contract:\n{self._serialize_for_prompt(self._build_generation_contract(defaults), max_chars=1800)}\n\n"
            f"Requested unit:\n{self._serialize_for_prompt(unit_spec, max_chars=1000)}\n\n"
            f"{existing_context}"
            f"{feedback_block}"
            "Return only ```lua ... ``` for this unit."
        )

    def _build_code_unit_plan(self, defaults: dict) -> list[dict]:
        requirement_text = self._build_requirement_text(defaults)
        component_texts = [
            str(item).strip()
            for item in defaults.get("implementation_plan", {}).get("components", [])
            if str(item).strip()
        ]
        units: list[dict] = []
        seen: set[str] = set()

        def add_unit(
            name: str,
            purpose: str,
            *,
            required_markers: list[str] | None = None,
            dependencies: list[str] | None = None,
        ) -> None:
            slug = self.slugify_identifier(name)
            if slug in seen:
                return
            seen.add(slug)
            units.append(
                {
                    "name": slug,
                    "purpose": purpose,
                    "required_markers": list(required_markers or []),
                    "dependencies": list(dependencies or []),
                }
            )

        add_unit(
            "shared_utilities",
            "Small shared helper functions for validation, table/string helpers, and reusable local utilities.",
            required_markers=["local function"],
        )
        add_unit(
            "config_state",
            "Configuration tables and base mutable state needed by the program.",
            required_markers=["config", "state"],
            dependencies=["shared_utilities"],
        )

        unit_rules = [
            (
                ("priority queue", "приоритет"),
                "priority_queue",
                "Priority queue implementation with push/pop/peek behavior for ordered tasks or events.",
                ["priorityqueue", "push", "pop"],
                ["shared_utilities"],
            ),
            (
                ("event bus", "event-driven", "событ"),
                "event_bus",
                "Event bus implementation with subscribe/unsubscribe/publish or emit behavior.",
                ["event", "subscribe", "publish"],
                ["shared_utilities"],
            ),
            (
                ("retry", "backoff", "повтор"),
                "retry_backoff",
                "Retry and backoff logic for transient task failures.",
                ["retry", "backoff"],
                ["shared_utilities", "config_state"],
            ),
            (
                ("timer", "таймер", "delayed", "periodic"),
                "timer_management",
                "Timer and delayed/periodic task scheduling helpers.",
                ["timer", "delay"],
                ["shared_utilities", "config_state"],
            ),
            (
                ("logging", "logger", "лог"),
                "logging_subsystem",
                "Structured logging helpers for important runtime events.",
                ["logger", "log"],
                ["shared_utilities", "config_state"],
            ),
            (
                ("scheduler", "task scheduler", "планировщик"),
                "scheduler_core",
                "Core scheduler orchestration with coroutine jobs, queues, retry integration, and dispatch loop.",
                ["scheduler", "coroutine", "resume"],
                ["config_state", "priority_queue", "event_bus", "retry_backoff", "timer_management", "logging_subsystem"],
            ),
            (
                ("магазин", "shop", "upgrade", "улучш"),
                "shop_and_upgrades",
                "Shop logic, pricing, currency spending, and upgrade application.",
                ["shop", "buy"],
                ["config_state", "shared_utilities"],
            ),
            (
                ("guess", "угадай", "число", "number game"),
                "round_logic",
                "Guessing round logic, random target generation, attempt tracking, hints, and result evaluation.",
                ["math.random", "attempt", "guess"],
                ["config_state", "shared_utilities"],
            ),
            (
                ("menu", "меню", "rules", "правил"),
                "menu_flow",
                "Console menu flow, rules output, and high-level navigation.",
                ["print", "menu"],
                ["config_state", "shared_utilities"],
            ),
            (
                ("inventory", "инвентар", "item", "предмет"),
                "inventory_system",
                "Inventory storage and item manipulation helpers.",
                ["inventory", "item"],
                ["config_state", "shared_utilities"],
            ),
            (
                ("battle", "combat", "бой", "враг", "enemy"),
                "combat_system",
                "Combat rules, enemy turns, damage handling, and battle actions.",
                ["attack", "enemy", "damage"],
                ["config_state", "shared_utilities"],
            ),
            (
                ("quest", "квест", "задан"),
                "quest_system",
                "Quest tracking, progress updates, and completion rewards.",
                ["quest", "progress"],
                ["config_state", "shared_utilities"],
            ),
            (
                ("save", "load", "сохран", "загруз", "file"),
                "persistence",
                "Save/load helpers using standard Lua file IO.",
                ["io.open", "write", "read"],
                ["config_state", "shared_utilities"],
            ),
            (
                ("demo", "демо", "example", "usage"),
                "demo_scenario",
                "Demonstration scenario that exercises the main program features.",
                ["demo", "scenario"],
                ["config_state", "shared_utilities"],
            ),
        ]

        for markers, name, purpose, required_markers, dependencies in unit_rules:
            if self._contains_requirement(requirement_text, markers):
                add_unit(
                    name,
                    purpose,
                    required_markers=required_markers,
                    dependencies=dependencies,
                )

        for component_text in component_texts:
            slug = self.slugify_identifier(component_text, default="component")
            if slug in seen:
                continue
            add_unit(
                slug,
                f"Implement the following planned component in a focused cohesive unit: {component_text}",
                required_markers=[slug.split("_")[0]],
                dependencies=["shared_utilities", "config_state"],
            )

        if len(units) < 5:
            add_unit(
                "core_logic",
                "Core domain logic and state transitions that drive the requested program behavior.",
                required_markers=["function"],
                dependencies=["config_state", "shared_utilities"],
            )
            add_unit(
                "io_flow",
                "Console input/output flow, user-facing messages, and orchestration helpers.",
                required_markers=["print", "io"],
                dependencies=["core_logic"],
            )

        add_unit(
            "main_entry",
            "Public entrypoint function M.main() that wires the units together and runs the program flow.",
            required_markers=["function m.main"],
            dependencies=[unit["name"] for unit in units if unit["name"] != "main_entry"][-4:],
        )

        if len(units) <= 10:
            return units

        main_entry = next((unit for unit in units if unit["name"] == "main_entry"), None)
        trimmed = [unit for unit in units if unit["name"] != "main_entry"][:9]
        if main_entry is not None:
            trimmed.append(main_entry)
        return trimmed

    def _generate_monolithic_candidate(
        self,
        *,
        defaults: dict,
        seed_feedback: list[str],
    ) -> dict:
        best_candidate: dict | None = None
        best_score: tuple[int, int, int] | None = None
        feedback: list[str] = list(seed_feedback)
        errors: list[str] = list(seed_feedback)

        for attempt_index, temperature in enumerate((0.0, 0.15, 0.3), start=1):
            try:
                response_text = self.ask_text(
                    system_prompt=self._build_generation_system_prompt(),
                    user_prompt=self._build_generation_user_prompt(
                        defaults=defaults,
                        attempt_index=attempt_index,
                        feedback=feedback,
                    ),
                    temperature=temperature,
                )
                parsed_response = self._parse_generation_response(response_text)
            except (LocalModelError, ValueError, TypeError) as exc:
                errors.append(str(exc))
                feedback = [str(exc)]
                continue

            normalized_code = self.normalize_lua_code(parsed_response["current_code"])
            issues = self._assess_generated_code(
                defaults=defaults,
                code=normalized_code,
            )
            notes = list(parsed_response["generation_notes"])
            if issues:
                notes.append(
                    "Generation quality gate requested another pass: " + " | ".join(issues[:6])
                )
            candidate = {
                "current_code": normalized_code,
                "code_unit_plan": self._build_code_unit_plan(defaults),
                "code_units": [],
                "code_unit_map": [],
                "generation_notes": notes,
                "quality_issues": issues,
            }
            score = self._score_candidate(normalized_code, issues)
            if best_score is None or score < best_score:
                best_candidate = candidate
                best_score = score

            if not issues:
                return candidate

            feedback = issues
            errors.extend(issues[:3])

        if best_candidate is None:
            raw_candidate = self._attempt_raw_lua_fallback(defaults=defaults, feedback=feedback or errors)
            if raw_candidate is not None:
                raw_candidate["code_unit_plan"] = self._build_code_unit_plan(defaults)
                raw_candidate["code_units"] = []
                raw_candidate["code_unit_map"] = []
                raw_candidate["quality_issues"] = self._assess_generated_code(
                    defaults=defaults,
                    code=raw_candidate["current_code"],
                )
                return raw_candidate

        if best_candidate is not None:
            if errors:
                best_candidate["generation_notes"] = [
                    *best_candidate["generation_notes"],
                    "Monolithic fallback completed after internal retries: "
                    + " | ".join(dict.fromkeys(errors[:6])),
                ]
            return best_candidate

        raise ValueError("The generator could not produce any Lua code candidate.")

    def _attempt_raw_lua_fallback(self, *, defaults: dict, feedback: list[str]) -> dict | None:
        feedback_block = ""
        if feedback:
            feedback_block = (
                "Fix these known issues in the returned Lua source:\n"
                f"{self._serialize_for_prompt(feedback[:8], max_chars=900)}\n\n"
            )

        response_text = self.ask_text(
            system_prompt=(
                "You are Code Generator Agent in a local multi-agent pipeline for generating and validating Lua code.\n"
                "The structured JSON response format failed on previous attempts.\n"
                "Return only one fenced Lua code block and nothing else.\n"
                "The Lua code must be complete, standard Lua, and runnable.\n"
                "Do not return JSON, prose, bullet lists, or explanations."
            ),
            user_prompt=(
                "Generate the Lua source directly.\n\n"
                f"Raw user prompt excerpt:\n{self.clip_text(str(defaults['user_prompt']), max_chars=1400)}\n\n"
                f"Generation contract:\n{self._serialize_for_prompt(self._build_generation_contract(defaults), max_chars=2200)}\n\n"
                f"{feedback_block}"
                "Return only ```lua ... ```."
            ),
            temperature=0.2,
        )
        current_code = self.extract_code_block(response_text, language="lua")
        if not current_code or not self.looks_like_lua_source(current_code):
            return None

        normalized_code = self.normalize_lua_code(current_code)
        issues = self._assess_generated_code(defaults=defaults, code=normalized_code)
        notes = [
            "Structured JSON generation fallback was used because the model did not return a safe parseable JSON payload.",
        ]
        if issues:
            notes.append(
                "Raw-Lua fallback still left some quality issues before execution: "
                + " | ".join(issues[:6])
            )
        return {
            "current_code": normalized_code,
            "generation_notes": notes,
        }

    @staticmethod
    def _build_generation_system_prompt() -> str:
        return (
            "You are Code Generator Agent in a local multi-agent pipeline for generating and validating Lua code.\n"
            "Your role: generate the initial Lua solution from the structured task specification and implementation plan.\n"
            "You do not execute code, do not test code, do not repair code, and do not finalize artifacts.\n"
            "Return only structured JSON in this exact shape:\n"
            "{\n"
            '  "current_code": "",\n'
            '  "code_versions": [\n'
            "    {\n"
            '      "version": 1,\n'
            '      "source": "generator",\n'
            '      "notes": []\n'
            "    }\n"
            "  ],\n"
            '  "generation_notes": [],\n'
            '  "status": "CODE_GENERATED"\n'
            "}\n"
            "Important:\n"
            "- current_code must be valid Lua code as a string.\n"
            "- Generate a complete runnable implementation, not fragments.\n"
            "- Do not return explanations instead of code.\n"
            "- Do not add external dependencies unless explicitly required.\n"
            "- Use only standard Lua syntax. Do not use pseudo-keywords like global/public/private/protected/export.\n"
            "- Do not use Luau, Teal, or typed-Lua syntax such as type aliases, ': number', or typed local declarations.\n"
            "- Do not use Python-style syntax or pseudocode such as class/def/lambda/try/except/self/append.\n"
            "- Do not leave TODOs, placeholders, or stub sections.\n"
            "- If the task is architecture-heavy, implement the named subsystems explicitly instead of returning a simplified toy example.\n"
            "- If the script is interactive, keep the entry path test-safe with a run_mode/__test__ guard."
        )

    def _build_generation_user_prompt(
        self,
        *,
        defaults: dict,
        attempt_index: int,
        feedback: list[str],
    ) -> str:
        feedback_block = ""
        if feedback:
            feedback_block = (
                "Previous candidate issues that must be fixed now:\n"
                f"{self._serialize_for_prompt(feedback[:8], max_chars=900)}\n\n"
            )

        contract = self._build_generation_contract(defaults)

        return (
            "Generate the initial Lua implementation from the following planning data.\n\n"
            f"Attempt: {attempt_index}\n\n"
            f"Raw user prompt excerpt:\n{self.clip_text(str(defaults['user_prompt']), max_chars=1400)}\n\n"
            f"Generation contract:\n{self._serialize_for_prompt(contract, max_chars=2200)}\n\n"
            f"{feedback_block}"
            "Requirements:\n"
            "- Follow the named components and critical requirements exactly.\n"
            "- Include basic error handling where appropriate.\n"
            "- Produce complete Lua code that can be executed later by downstream agents.\n"
            "- Prefer production-style structure and explicit subsystems over compressed toy logic.\n"
            "- Keep names clear and interfaces coherent.\n"
            "- Return only the JSON object."
        )

    def _build_generation_contract(self, defaults: dict) -> dict:
        parsed_spec = defaults["parsed_spec"] if isinstance(defaults["parsed_spec"], dict) else {}
        implementation_plan = (
            defaults["implementation_plan"]
            if isinstance(defaults["implementation_plan"], dict)
            else {}
        )
        goal = str(parsed_spec.get("goal") or implementation_plan.get("goal") or "").strip()
        constraints = [
            str(item).strip()
            for item in parsed_spec.get("constraints", [])[:8]
            if str(item).strip()
        ]
        assumptions = [
            str(item).strip()
            for item in parsed_spec.get("assumptions", [])[:4]
            if str(item).strip()
        ]
        components = [
            str(item).strip()
            for item in implementation_plan.get("components", [])[:12]
            if str(item).strip()
        ]
        steps = [
            str(item).strip()
            for item in implementation_plan.get("steps", [])[:6]
            if str(item).strip()
        ]
        success_criteria = [
            str(item).strip()
            for item in parsed_spec.get("success_criteria", [])[:6]
            if str(item).strip()
        ]
        validation_focus = [
            str(item).strip()
            for item in defaults.get("validation_plan", {}).get("correctness_checks", [])[:6]
            if str(item).strip()
        ]
        must_have_markers = self._collect_must_have_markers(str(defaults["user_prompt"]))

        contract = {
            "goal": goal,
            "constraints": constraints,
            "assumptions": assumptions,
            "components": components,
            "implementation_steps": steps,
            "success_criteria": success_criteria,
            "validation_focus": validation_focus,
            "must_have_markers": must_have_markers,
        }

        minimum_lines = self.extract_minimum_line_count(str(defaults["user_prompt"]))
        if minimum_lines is not None:
            contract["minimum_line_count"] = minimum_lines

        return contract

    def _collect_must_have_markers(self, user_prompt: str) -> list[str]:
        lowered = user_prompt.lower()
        markers: list[str] = []
        rules = [
            (("priority queue", "приоритет"), "priority queue"),
            (("scheduler", "task scheduler", "планировщик"), "scheduler"),
            (("event bus", "event-driven", "событ"), "event bus"),
            (("retry", "backoff", "повтор"), "retry/backoff"),
            (("timer", "таймер", "delayed", "periodic"), "timer management"),
            (("logging", "logger", "лог"), "logging subsystem"),
            (("config", "конфиг", "configuration"), "config table"),
            (("demo", "демо", "demonstration"), "demo section"),
            (("coroutine", "coroutine-based", "корутин"), "coroutine-based jobs"),
            (("metatable", "metatables"), "metatable-based design"),
            (("pcall", "xpcall"), "protected execution with pcall/xpcall"),
        ]
        for prompt_markers, label in rules:
            if self._contains_requirement(lowered, prompt_markers) and label not in markers:
                markers.append(label)
        return markers

    def _assess_generated_code(self, *, defaults: dict, code: str) -> list[str]:
        issues: list[str] = []
        code_lower = code.lower()
        requirement_text = self._build_requirement_text(defaults)
        line_count = len(code.splitlines())
        minimum_lines = self.extract_minimum_line_count(str(defaults["user_prompt"]))

        if minimum_lines is not None and line_count < minimum_lines:
            issues.append(
                f"The code is too short: observed {line_count} lines, required at least {minimum_lines}."
            )

        placeholder_patterns = (
            r"(?im)\btodo\b",
            r"(?im)\bpseudocode\b",
            r"(?im)\bplaceholder\b",
            r"(?im)\bstub\b",
            r"(?im)--\s*add\b.+\bhere\b",
            r"(?im)--\s*implement\b.+\bhere\b",
        )
        if any(re.search(pattern, code) for pattern in placeholder_patterns):
            issues.append("The code still contains placeholder or unfinished implementation markers.")

        banned_syntax_patterns = (
            r"(?m)^\s*(global|public|private|protected|export)\s+function\b",
            r"(?m)^\s*type\s+[A-Za-z_][A-Za-z0-9_]*\s*=",
            r"(?m)^\s*local\s+[A-Za-z_][A-Za-z0-9_]*\s*:\s*",
            r"(?m)^\s*class\s+[A-Za-z_][A-Za-z0-9_]*\b",
            r"(?m)^\s*def\s+[A-Za-z_][A-Za-z0-9_]*\b",
            r"(?<![A-Za-z0-9_])lambda(?![A-Za-z0-9_])",
            r"(?m)^\s*try\s*:\s*$",
            r"(?m)^\s*except\b",
            r"(?<![A-Za-z0-9_])self\.",
            r"(?<![A-Za-z0-9_])None(?![A-Za-z0-9_])",
            r"(?<![A-Za-z0-9_])append\s*\(",
            r"\+=",
            r"(?m)^\s*del\s+",
        )
        if any(re.search(pattern, code) for pattern in banned_syntax_patterns):
            issues.append("The code still contains non-Lua or Python-like syntax.")

        if self._contains_requirement(
            requirement_text,
            ("без сторонних библиотек", "without external libraries", "no external libraries", "use only standard lua"),
        ):
            requires = re.findall(r"\brequire\s*\(\s*['\"]([^'\"]+)['\"]\s*\)", code_lower)
            if requires:
                issues.append(
                    "The code references external modules even though the task asks for standard standalone Lua."
                )

        architecture_issues = self._check_architecture_markers(requirement_text, code_lower)
        issues.extend(architecture_issues)

        return issues

    def _assess_unit_fragment(self, *, unit_spec: dict, code: str) -> list[str]:
        issues: list[str] = []
        if not code.strip():
            return ["The unit response is empty."]

        banned_syntax_patterns = (
            r"(?m)^\s*(global|public|private|protected|export)\s+function\b",
            r"(?m)^\s*type\s+[A-Za-z_][A-Za-z0-9_]*\s*=",
            r"(?m)^\s*local\s+[A-Za-z_][A-Za-z0-9_]*\s*:\s*",
            r"(?m)^\s*class\s+[A-Za-z_][A-Za-z0-9_]*\b",
            r"(?m)^\s*def\s+[A-Za-z_][A-Za-z0-9_]*\b",
            r"(?<![A-Za-z0-9_])lambda(?![A-Za-z0-9_])",
            r"(?m)^\s*try\s*:\s*$",
        )
        if any(re.search(pattern, code) for pattern in banned_syntax_patterns):
            issues.append("The unit still contains non-Lua syntax.")

        lower_code = code.lower()
        for marker in unit_spec.get("required_markers", [])[:4]:
            if marker.lower() not in lower_code:
                issues.append(f"The unit is missing an expected marker: {marker}.")

        if re.search(r"(?im)\btodo\b|\bpseudocode\b|\bstub\b", code):
            issues.append("The unit still contains placeholder text.")

        return issues

    def _build_requirement_text(self, defaults: dict) -> str:
        parsed_spec = defaults["parsed_spec"] if isinstance(defaults["parsed_spec"], dict) else {}
        implementation_plan = (
            defaults["implementation_plan"]
            if isinstance(defaults["implementation_plan"], dict)
            else {}
        )
        parts = [str(defaults["user_prompt"])]
        for key in ("goal",):
            parts.append(str(parsed_spec.get(key, "")))
            parts.append(str(implementation_plan.get(key, "")))
        for key in ("inputs", "outputs", "constraints", "assumptions", "success_criteria"):
            value = parsed_spec.get(key, [])
            if isinstance(value, list):
                parts.extend(str(item) for item in value)
        for key in ("steps", "components", "constraints", "assumptions"):
            value = implementation_plan.get(key, [])
            if isinstance(value, list):
                parts.extend(str(item) for item in value)
        return " ".join(part.lower() for part in parts if part)

    def _check_architecture_markers(self, requirement_text: str, code_lower: str) -> list[str]:
        issues: list[str] = []
        rules = [
            ("Priority queue subsystem is missing", ("priority queue", "приоритет"), ("priorityqueue", "priority_queue", "heap", "enqueue", "dequeue")),
            ("Scheduler subsystem is missing", ("scheduler", "task scheduler", "планировщик"), ("scheduler", "schedule", "dispatch", "tick", "run_loop")),
            ("Event bus subsystem is missing", ("event bus", "event-driven", "событ"), ("eventbus", "event_bus", "subscribe", "publish", "emit")),
            ("Retry/backoff subsystem is missing", ("retry", "backoff", "повтор", "backoff strategy"), ("retry", "backoff", "attempt", "max_attempt")),
            ("Timer subsystem is missing", ("timer", "таймер", "delayed task", "periodic"), ("timer", "delay", "interval", "next_run", "periodic")),
            ("Logging subsystem is missing", ("logging", "logger", "лог"), ("logger", "log", "debug", "info", "error")),
            ("Config subsystem is missing", ("config", "конфиг", "configuration"), ("config",)),
            ("Coroutine-based execution is missing", ("coroutine", "coroutine-based", "корутин"), ("coroutine.create", "coroutine.resume", "coroutine.yield", "coroutine.status")),
            ("Metatable-based design is missing", ("metatable", "metatables"), ("setmetatable",)),
            ("Demo section is missing", ("demo", "демо", "demonstration"), ("demo", "run_demo")),
        ]

        for issue, prompt_markers, code_markers in rules:
            if self._contains_requirement(requirement_text, prompt_markers) and not self._contains_any(code_lower, code_markers):
                issues.append(issue)

        return issues

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

    @staticmethod
    def _score_candidate(code: str, issues: list[str]) -> tuple[int, int, int]:
        critical_count = sum(
            1
            for issue in issues
            if any(
                marker in issue.lower()
                for marker in ("too short", "placeholder", "non-lua", "non-standard lua syntax", "missing")
            )
        )
        return (critical_count, len(issues), -len(code.splitlines()))

    @staticmethod
    def _validate_inputs(parsed_spec, implementation_plan) -> str | None:
        if not isinstance(parsed_spec, dict):
            return "Code Generator Agent requires parsed_spec to be a JSON object."
        if not str(parsed_spec.get("goal", "")).strip():
            return "Code Generator Agent requires parsed_spec.goal to be present and non-empty."
        if not isinstance(implementation_plan, dict):
            return "Code Generator Agent requires implementation_plan to be a JSON object."
        if not str(implementation_plan.get("goal", "")).strip():
            return "Code Generator Agent requires implementation_plan.goal to be present and non-empty."
        steps = implementation_plan.get("steps")
        if not isinstance(steps, list) or not steps:
            return "Code Generator Agent requires implementation_plan.steps to contain at least one planning step."
        return None

    def _parse_generation_response(self, response_text: str) -> dict:
        try:
            payload = self.parse_json_response(response_text)
            if not isinstance(payload, dict):
                raise ValueError("Model did not return a JSON object.")

            current_code = str(payload.get("current_code", "")).strip()
            generation_notes = self._normalize_notes(payload.get("generation_notes"))
            if not current_code:
                raise ValueError("Model did not return current_code.")

            return {
                "current_code": current_code,
                "generation_notes": generation_notes,
            }
        except Exception:
            if self.looks_like_json_payload(response_text):
                raise ValueError(
                    "Model returned a malformed JSON-like payload instead of valid structured JSON or Lua code."
                )
            current_code = self.extract_code_block(response_text, language="lua")
            if not current_code or not self.looks_like_lua_source(current_code):
                raise ValueError("Model did not return valid structured JSON or a Lua code block.")
            return {
                "current_code": current_code,
                "generation_notes": [
                    "The model returned a Lua code block instead of the requested structured JSON, and the response was normalized by the agent.",
                ],
            }

    def _parse_unit_generation_response(self, response_text: str) -> str:
        code = self.extract_code_block(response_text, language="lua")
        if code and (self.looks_like_lua_source(code) or "function M.main" in code):
            return code

        stripped = response_text.strip()
        if not self.looks_like_json_payload(stripped) and (self.looks_like_lua_source(stripped) or "function M.main" in stripped):
            return stripped

        raise ValueError("Unit generator did not return a valid Lua code block.")

    @staticmethod
    def _summarize_units_for_prompt(code_units: list[dict]) -> list[dict]:
        summary: list[dict] = []
        for unit in code_units[-6:]:
            summary.append(
                {
                    "name": unit.get("name", ""),
                    "purpose": unit.get("purpose", ""),
                    "dependencies": list(unit.get("dependencies", []))[:4],
                }
            )
        return summary

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
            "source": "generator",
            "notes": notes,
        }

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

    def _serialize_for_prompt(self, payload, *, max_chars: int) -> str:
        return self.clip_text(self.to_prompt_json(payload), max_chars=max_chars)


AGENT_CLASS = GenerateCodeAgentV1
