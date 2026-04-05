from agents.base import BaseAgent
from state import (
    STATUS_FAILED,
    STATUS_PARSED,
    STATUS_PLANNED,
    build_failure_result,
)


class PlanTaskAgentV1(BaseAgent):
    role = "plan_task"
    version = "v1"

    def run(self, state):
        defaults = self.with_defaults(state)
        current_status = str(defaults["status"])
        parsed_spec = defaults["parsed_spec"]
        user_prompt = str(defaults["user_prompt"]).strip()

        if current_status != STATUS_PARSED:
            failure = build_failure_result(
                state,
                f"Plan-Task Agent expects status={STATUS_PARSED}, got {current_status or '<empty>'}.",
            )
            return {
                **failure,
                "planning_notes": [
                    "Planning aborted because the state precondition was not satisfied.",
                ],
                "status": STATUS_FAILED,
            }

        error = self._validate_parsed_spec(parsed_spec)
        if error:
            failure = build_failure_result(state, error)
            return {
                **failure,
                "implementation_plan": {},
                "validation_plan": {
                    "execution_checks": [],
                    "correctness_checks": [],
                    "safety_checks": [],
                },
                "test_plan_outline": {
                    "normal_cases": [],
                    "edge_cases": [],
                    "invalid_input_cases": [],
                },
                "planning_notes": [
                    "Planning failed because parsed_spec is missing or invalid.",
                    error,
                ],
                "status": STATUS_FAILED,
            }

        normalized_spec = self._normalize_parsed_spec(parsed_spec)
        combined_text = self._build_combined_text(user_prompt, normalized_spec)

        implementation_plan = {
            "goal": normalized_spec["goal"],
            "steps": self._build_steps(normalized_spec, combined_text),
            "components": self._build_components(normalized_spec, combined_text),
            "constraints": normalized_spec["constraints"],
            "assumptions": self._build_assumptions(normalized_spec, combined_text),
        }
        validation_plan = self._build_validation_plan(normalized_spec, combined_text)
        test_plan_outline = self._build_test_plan_outline(normalized_spec, combined_text)
        planning_notes = self._build_planning_notes(normalized_spec, implementation_plan)

        return {
            "implementation_plan": implementation_plan,
            "validation_plan": validation_plan,
            "test_plan_outline": test_plan_outline,
            "planning_notes": planning_notes,
            "status": STATUS_PLANNED,
        }

    @staticmethod
    def _validate_parsed_spec(parsed_spec) -> str | None:
        if not isinstance(parsed_spec, dict):
            return "Plan-Task Agent requires parsed_spec to be a JSON object."
        goal = str(parsed_spec.get("goal", "")).strip()
        if not goal:
            return "Plan-Task Agent requires parsed_spec.goal to be present and non-empty."
        return None

    def _normalize_parsed_spec(self, parsed_spec: dict) -> dict:
        return {
            "goal": str(parsed_spec.get("goal", "")).strip(),
            "inputs": self._normalize_list(parsed_spec.get("inputs")),
            "outputs": self._normalize_list(parsed_spec.get("outputs")),
            "constraints": self._normalize_list(parsed_spec.get("constraints")),
            "assumptions": self._normalize_list(parsed_spec.get("assumptions")),
            "success_criteria": self._normalize_list(parsed_spec.get("success_criteria")),
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

    @staticmethod
    def _build_combined_text(user_prompt: str, parsed_spec: dict) -> str:
        parts = [user_prompt, parsed_spec.get("goal", "")]
        parts.extend(parsed_spec.get("inputs", []))
        parts.extend(parsed_spec.get("outputs", []))
        parts.extend(parsed_spec.get("constraints", []))
        parts.extend(parsed_spec.get("assumptions", []))
        parts.extend(parsed_spec.get("success_criteria", []))
        return " ".join(part.lower() for part in parts if part)

    def _build_steps(self, parsed_spec: dict, combined_text: str) -> list[str]:
        steps = [
            "Define the Lua file structure, entry point, and main data containers required to satisfy the task goal.",
            "Implement input handling and normalization for every expected input path described in parsed_spec.",
            "Implement the core domain logic in small, testable functions that match the required behavior.",
            "Implement output generation and any required user-facing flow so the script produces the expected results.",
        ]

        if self._has_persistence_requirement(combined_text):
            steps.append(
                "Implement file persistence and recovery flow for the required save/load behavior."
            )
        if self._contains_any(combined_text, ["console", "консол", "input", "ввод", "menu", "меню"]):
            steps.append(
                "Wire the console interaction flow so valid commands reach the corresponding Lua functions cleanly."
            )
        if self._contains_any(combined_text, ["inventory", "инвентар", "shop", "магаз", "quest", "квест", "battle", "бой", "enemy", "враг"]):
            steps.append(
                "Implement the task-specific subsystems and keep their state transitions explicit and isolated."
            )
        if self._contains_any(
            combined_text,
            [
                "scheduler",
                "task scheduler",
                "priority queue",
                "event bus",
                "event-driven",
                "retry",
                "backoff",
                "timer",
                "logging",
                "logger",
                "config",
                "coroutine",
                "demo",
                "планировщик",
                "приоритет",
                "событ",
                "таймер",
                "лог",
                "конфиг",
                "корутин",
                "демо",
            ],
        ):
            steps.append(
                "Implement the requested infrastructure subsystems explicitly and integrate them through clear interfaces instead of collapsing them into one monolithic loop."
            )

        steps.extend(
            [
                "Apply the explicit constraints from parsed_spec and keep all assumptions visible in the code structure.",
                "Prepare the implementation so downstream execution and testing agents can validate it without reinterpreting the original prompt.",
            ]
        )
        return steps

    def _build_components(self, parsed_spec: dict, combined_text: str) -> list[str]:
        components = [
            "Main Lua script or module entry point",
            "Core state/data tables",
            "Input handling functions",
            "Output/rendering functions",
            "Core processing functions",
        ]

        conditional_components = [
            (["save", "load", "persist", "serialization", "serialize", "deserialize", "сохран", "загруз"], "File persistence component"),
            (["console", "консол", "menu", "меню"], "Console interaction flow component"),
            (["inventory", "инвентар"], "Inventory management component"),
            (["shop", "магаз", "торгов"], "Trading or shop component"),
            (["quest", "квест", "задан"], "Quest tracking component"),
            (["battle", "бой", "enemy", "враг"], "Combat or encounter component"),
            (["location", "локац", "forest", "cave", "village", "ruins", "camp", "лес", "пещер", "деревн", "руин", "лагер"], "Location and event generation component"),
            (["level", "опыт", "experience", "уров"], "Progression or leveling component"),
            (["priority queue", "приоритет"], "Priority queue component"),
            (["scheduler", "task scheduler", "планировщик"], "Scheduler core component"),
            (["event bus", "event-driven", "событ"], "Event bus component"),
            (["retry", "backoff", "повтор"], "Retry and backoff component"),
            (["timer", "таймер", "delayed", "periodic"], "Timer management component"),
            (["logging", "logger", "лог"], "Logging subsystem"),
            (["config", "конфиг", "configuration"], "Configuration component"),
            (["demo", "демо", "demonstration"], "Demo scenario component"),
        ]
        for markers, component in conditional_components:
            if self._contains_any(combined_text, markers):
                self._append_unique(components, component)

        if parsed_spec["outputs"]:
            self._append_unique(components, "Result formatting/output contract component")
        return components

    def _build_assumptions(self, parsed_spec: dict, combined_text: str) -> list[str]:
        assumptions = list(parsed_spec["assumptions"])
        inferred_assumptions = [
            (
                self._has_persistence_requirement(combined_text) and
                not self._contains_any(combined_text, ["json", "csv", "sqlite"]),
                "If no persistence format is specified, use a simple file format that is easy to read and validate locally.",
            ),
            (
                self._contains_any(combined_text, ["console", "консол", "menu", "меню", "input", "ввод"]),
                "If the exact interaction flow is unspecified, use a simple text-based control flow with explicit commands or menu choices.",
            ),
            (
                self._contains_any(combined_text, ["quest", "квест"]) and
                not self._contains_any(combined_text, ["api", "network", "http"]),
                "If the task mentions quests but not a specific quest data source, implement them as local in-script data.",
            ),
        ]
        for should_add, assumption in inferred_assumptions:
            if should_add:
                self._append_unique(assumptions, assumption)

        if not assumptions:
            assumptions.append(
                "Unspecified implementation details should remain minimal and consistent with parsed_spec."
            )
        return assumptions

    def _build_validation_plan(self, parsed_spec: dict, combined_text: str) -> dict:
        execution_checks = [
            "Load the Lua script with the configured runtime and verify that it has no syntax errors.",
            "Execute the main script/module entry path and verify that startup does not raise runtime errors.",
        ]
        if self._has_persistence_requirement(combined_text):
            execution_checks.append(
                "Verify that file-based save/load paths execute without crashing in the local working directory."
            )
        if self._contains_any(combined_text, ["console", "консол", "input", "ввод", "menu", "меню"]):
            execution_checks.append(
                "Verify that the console interaction path accepts representative commands without crashing."
            )

        correctness_checks = [
            "Verify that the implementation matches parsed_spec.goal.",
        ]
        for item in parsed_spec["outputs"]:
            correctness_checks.append(f"Verify the required output behavior: {item}")
        for item in parsed_spec["success_criteria"]:
            correctness_checks.append(f"Verify success criterion: {item}")
        for item in parsed_spec["constraints"]:
            correctness_checks.append(f"Verify constraint: {item}")

        safety_checks = []
        if parsed_spec["inputs"]:
            safety_checks.append("Verify that empty or missing input is handled without an uncontrolled crash.")
            safety_checks.append("Verify that malformed or unsupported input is handled predictably.")
        if self._has_persistence_requirement(combined_text):
            safety_checks.append("Verify behavior when the expected save file is missing, unreadable, or malformed.")
        if not safety_checks:
            safety_checks.append("Verify that unexpected runtime states are handled without an uncontrolled crash.")

        return {
            "execution_checks": execution_checks,
            "correctness_checks": correctness_checks,
            "safety_checks": safety_checks,
        }

    def _build_test_plan_outline(self, parsed_spec: dict, combined_text: str) -> dict:
        normal_cases = [
            "Run the primary expected flow with valid input and verify the expected output/state transition.",
        ]
        if self._has_persistence_requirement(combined_text):
            normal_cases.append(
                "Run a save followed by a load and verify that the restored state matches the saved state."
            )
        if self._contains_any(combined_text, ["console", "консол", "menu", "меню"]):
            normal_cases.append(
                "Execute the main console flow through representative valid commands."
            )

        edge_cases = []
        if self._contains_any(combined_text, ["inventory", "инвентар"]):
            edge_cases.append("Handle empty inventory or unavailable item cases correctly.")
        if self._contains_any(combined_text, ["shop", "магаз", "торгов"]):
            edge_cases.append("Handle insufficient resources or unavailable purchase options correctly.")
        if self._contains_any(combined_text, ["battle", "бой", "enemy", "враг"]):
            edge_cases.append("Handle low-health, escape, or no-target combat states correctly.")
        if self._contains_any(combined_text, ["location", "локац", "forest", "cave", "village", "ruins", "camp", "лес", "пещер", "деревн", "руин", "лагер"]):
            edge_cases.append("Handle repeated or empty location events without invalid state transitions.")
        if self._contains_any(combined_text, ["priority queue", "приоритет", "scheduler", "task scheduler", "планировщик"]):
            edge_cases.append("Handle empty queues, repeated scheduling, and priority conflicts without invalid state transitions.")
        if self._contains_any(combined_text, ["event bus", "event-driven", "событ", "callback"]):
            edge_cases.append("Handle callback failures without breaking the dispatcher or losing unrelated work.")
        if self._contains_any(combined_text, ["timer", "таймер", "delayed", "periodic", "retry", "backoff"]):
            edge_cases.append("Handle delayed retries, periodic rescheduling, and long-running tasks without runaway loops.")
        if self._has_persistence_requirement(combined_text):
            edge_cases.append("Handle missing or partial save data safely.")
        if not edge_cases:
            edge_cases.append("Handle minimal valid input and boundary-state transitions correctly.")

        invalid_input_cases = []
        if parsed_spec["inputs"]:
            invalid_input_cases.append("Empty input.")
            invalid_input_cases.append("Malformed input type or command.")
            invalid_input_cases.append("Unsupported command or out-of-range choice.")
        if self._has_persistence_requirement(combined_text):
            invalid_input_cases.append("Corrupted or unreadable save file content.")
        if not invalid_input_cases:
            invalid_input_cases.append("Unexpected runtime parameters or missing optional data.")

        return {
            "normal_cases": normal_cases,
            "edge_cases": edge_cases,
            "invalid_input_cases": invalid_input_cases,
        }

    @staticmethod
    def _build_planning_notes(parsed_spec: dict, implementation_plan: dict) -> list[str]:
        notes = [
            "Planning was derived directly from parsed_spec and kept implementation-oriented.",
            "Explicit constraints were copied into the implementation plan so downstream agents do not need to re-read the raw prompt.",
            "Assumptions were preserved or made explicit only where missing details could affect implementation.",
        ]
        if parsed_spec["inputs"]:
            notes.append("The plan includes dedicated input-handling work because parsed_spec defines explicit inputs.")
        if len(implementation_plan["components"]) > 5:
            notes.append("The task was decomposed into multiple components to reduce ambiguity for the code generation stage.")
        return notes

    @staticmethod
    def _contains_any(text: str, markers: list[str]) -> bool:
        return any(marker in text for marker in markers)

    @staticmethod
    def _has_persistence_requirement(text: str) -> bool:
        return PlanTaskAgentV1._contains_any(
            text,
            ["save", "load", "persist", "serialization", "serialize", "deserialize", "сохран", "загруз"],
        )

    @staticmethod
    def _append_unique(items: list[str], value: str) -> None:
        if value not in items:
            items.append(value)


AGENT_CLASS = PlanTaskAgentV1
