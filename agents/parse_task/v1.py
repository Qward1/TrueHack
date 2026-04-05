import re

from agents.base import BaseAgent
from llm_client import LocalModelError
from state import STATUS_FAILED, STATUS_NEW_TASK, STATUS_PARSED, build_failure_result


class ParseTaskAgentV1(BaseAgent):
    role = "parse_task"
    version = "v1"

    def run(self, state):
        defaults = self.with_defaults(state)
        user_prompt = str(defaults["user_prompt"]).strip()
        current_status = str(defaults["status"])

        if current_status != STATUS_NEW_TASK:
            failure = build_failure_result(
                state,
                f"Parse-Task Agent expects status={STATUS_NEW_TASK}, got {current_status or '<empty>'}.",
            )
            return {
                **failure,
                "parsing_notes": [
                    "Parsing aborted because the state precondition was not satisfied.",
                ],
                "status": STATUS_FAILED,
            }

        if not user_prompt:
            failure = build_failure_result(
                state,
                "Parse-Task Agent received an empty user_prompt.",
            )
            return {
                **failure,
                "parsing_notes": [
                    "Parsing failed because user_prompt is empty.",
                ],
                "status": STATUS_FAILED,
            }

        if not self._looks_interpretable(user_prompt):
            failure = build_failure_result(
                state,
                "Parse-Task Agent could not interpret the user_prompt.",
            )
            return {
                **failure,
                "parsing_notes": [
                    "Parsing failed because the prompt does not contain enough interpretable task information.",
                ],
                "status": STATUS_FAILED,
            }

        try:
            payload = self.ask_json(
                system_prompt=(
                    "You are Parse-Task Agent in a local multi-agent pipeline for generating and validating Lua code.\n"
                    "Your role: parse and normalize the raw user request.\n"
                    "You do not plan implementation, do not write code, do not execute, do not test, do not repair, and do not finalize artifacts.\n"
                    "Return only JSON in this exact shape:\n"
                    "{\n"
                    '  "parsed_spec": {\n'
                    '    "goal": "",\n'
                    '    "inputs": [],\n'
                    '    "outputs": [],\n'
                    '    "constraints": [],\n'
                    '    "assumptions": [],\n'
                    '    "success_criteria": []\n'
                    "  },\n"
                    '  "parsing_notes": [],\n'
                    '  "status": "PARSED"\n'
                    "}\n"
                    "Be precise, minimal, and deterministic."
                ),
                user_prompt=(
                    "Parse the raw user request into a structured specification for downstream planning.\n\n"
                    "Requirements:\n"
                    "- Identify the main goal of the requested Lua solution.\n"
                    "- Extract expected inputs, outputs, constraints, assumptions, and success criteria.\n"
                    "- Normalize ambiguity into explicit assumptions.\n"
                    "- Separate required behavior from optional preferences by keeping optional preferences only in parsing_notes.\n"
                    "- Do not generate code.\n"
                    "- Do not create an implementation plan.\n"
                    "- Do not add unnecessary requirements.\n\n"
                    f"Raw user prompt:\n{user_prompt}"
                ),
                temperature=0.0,
            )
            parsed_spec = self._normalize_parsed_spec(
                payload.get("parsed_spec"),
                user_prompt=user_prompt,
            )
            parsing_notes = self._normalize_notes(payload.get("parsing_notes"))
        except (LocalModelError, ValueError, TypeError, AttributeError) as exc:
            parsed_spec, parsing_notes = self._build_fallback_result(
                user_prompt=user_prompt,
                error=exc,
            )

        if not parsed_spec["goal"]:
            failure = build_failure_result(
                state,
                "Parse-Task Agent produced an empty goal.",
            )
            return {
                **failure,
                "parsing_notes": [
                    *parsing_notes,
                    "Parsing failed because no main goal could be derived from the prompt.",
                ],
                "status": STATUS_FAILED,
            }

        return {
            "parsed_spec": parsed_spec,
            "parsing_notes": parsing_notes,
            "status": STATUS_PARSED,
        }

    def _normalize_parsed_spec(self, payload, *, user_prompt: str) -> dict:
        if not isinstance(payload, dict):
            raise ValueError("parsed_spec must be a JSON object.")

        spec = {
            "goal": self._normalize_text(payload.get("goal")),
            "inputs": self._normalize_list(payload.get("inputs")),
            "outputs": self._normalize_list(payload.get("outputs")),
            "constraints": self._normalize_list(payload.get("constraints")),
            "assumptions": self._normalize_list(payload.get("assumptions")),
            "success_criteria": self._normalize_list(payload.get("success_criteria")),
        }

        if not spec["goal"]:
            spec["goal"] = self._derive_goal(user_prompt)

        if not self._prompt_requires_user_input(user_prompt):
            spec["inputs"] = [
                item for item in spec["inputs"]
                if "console interface" not in item.lower()
                and "user input" not in item.lower()
            ]

        self._append_missing_inferences(spec, user_prompt=user_prompt)
        return spec

    def _build_fallback_result(self, *, user_prompt: str, error: Exception) -> tuple[dict, list[str]]:
        spec = {
            "goal": self._derive_goal(user_prompt),
            "inputs": self._derive_inputs(user_prompt),
            "outputs": self._derive_outputs(user_prompt),
            "constraints": self._derive_constraints(user_prompt),
            "assumptions": self._derive_assumptions(user_prompt),
            "success_criteria": self._derive_success_criteria(user_prompt),
        }
        parsing_notes = [
            "Structured parsing used deterministic fallback because the local model response was unavailable or invalid.",
            f"Fallback reason: {error}",
        ]
        parsing_notes.extend(self._derive_optional_preferences(user_prompt))
        return spec, parsing_notes

    def _append_missing_inferences(self, spec: dict, *, user_prompt: str) -> None:
        if not spec["inputs"]:
            spec["inputs"] = self._derive_inputs(user_prompt)
        if not spec["outputs"]:
            spec["outputs"] = self._derive_outputs(user_prompt)
        if not spec["constraints"]:
            spec["constraints"] = self._derive_constraints(user_prompt)
        if not spec["assumptions"]:
            spec["assumptions"] = self._derive_assumptions(user_prompt)
        if not spec["success_criteria"]:
            spec["success_criteria"] = self._derive_success_criteria(user_prompt)

    @staticmethod
    def _normalize_notes(value) -> list[str]:
        if value is None:
            return []
        if not isinstance(value, list):
            value = [value]
        notes: list[str] = []
        for item in value:
            text = str(item).strip()
            if text:
                notes.append(text)
        return notes

    @staticmethod
    def _normalize_text(value) -> str:
        return str(value or "").strip()

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
    def _looks_interpretable(user_prompt: str) -> bool:
        if len(user_prompt.strip()) < 6:
            return False
        return bool(re.search(r"[A-Za-zА-Яа-я0-9]", user_prompt))

    def _derive_goal(self, user_prompt: str) -> str:
        normalized = " ".join(user_prompt.split())
        if len(normalized) <= 220:
            return normalized
        return normalized[:217].rstrip() + "..."

    def _derive_inputs(self, user_prompt: str) -> list[str]:
        lowered = user_prompt.lower()
        inputs: list[str] = []
        if self._prompt_requires_user_input(user_prompt):
            inputs.append("User input through the console interface.")
        if self._has_persistence_requirement(lowered):
            inputs.append("File-based state or save data when loading or saving is requested.")
        return inputs

    def _derive_outputs(self, user_prompt: str) -> list[str]:
        lowered = user_prompt.lower()
        outputs = ["A Lua solution that fulfills the user request."]
        if any(marker in lowered for marker in ["консоль", "console", "print", "вывод"]):
            outputs.append("Console-visible output for the end user.")
        if self._has_persistence_requirement(lowered):
            outputs.append("A save file or persisted state artifact if saving is part of the task.")
        return outputs

    def _derive_constraints(self, user_prompt: str) -> list[str]:
        lowered = user_prompt.lower()
        constraints: list[str] = []
        minimum_lines = self.extract_minimum_line_count(user_prompt)
        if minimum_lines is not None:
            constraints.append(f"The Lua file must contain at least {minimum_lines} lines.")
        if "без сторонних библиотек" in lowered or "without external libraries" in lowered:
            constraints.append("Use only standard Lua facilities and no external libraries.")
        if "автоном" in lowered or "standalone" in lowered or "fully autonomous" in lowered:
            constraints.append("The solution must be standalone.")
        if "консоль" in lowered or "console" in lowered:
            constraints.append("The solution must run in a console environment.")
        if self._requires_comments(lowered):
            constraints.append("The code should include clear comments for major blocks.")
        return constraints

    def _derive_assumptions(self, user_prompt: str) -> list[str]:
        lowered = user_prompt.lower()
        assumptions: list[str] = []
        if self._has_persistence_requirement(lowered) and "json" not in lowered:
            assumptions.append("If no save format is specified, a simple text-based or Lua-readable file format may be used.")
        if any(marker in lowered for marker in ["консоль", "console", "ввод", "input"]):
            assumptions.append("If no UI flow is specified, a simple text menu or command-driven console interaction is acceptable.")
        if any(marker in lowered for marker in ["квест", "quest"]) and not re.search(r"\b\d+\b", lowered):
            assumptions.append("If the exact quest structure is unspecified, a small set of simple quests is acceptable.")
        if not assumptions:
            assumptions.append("Any unspecified details should be kept minimal and consistent with the raw prompt.")
        return assumptions

    def _derive_success_criteria(self, user_prompt: str) -> list[str]:
        criteria = [
            "The parsed specification preserves the original task goal without rewriting it into code or an implementation plan.",
            "The specification is concrete enough for the planning agent to continue without reinterpreting the raw prompt.",
        ]
        minimum_lines = self.extract_minimum_line_count(user_prompt)
        if minimum_lines is not None:
            criteria.append(f"The specification explicitly preserves the minimum size requirement of {minimum_lines} lines.")
        return criteria

    def _derive_optional_preferences(self, user_prompt: str) -> list[str]:
        lowered = user_prompt.lower()
        notes: list[str] = []
        preference_markers = [
            ("желательно", "Optional preference marker detected in the prompt."),
            ("предпочт", "Optional preference marker detected in the prompt."),
            ("would be nice", "Optional preference marker detected in the prompt."),
            ("nice to have", "Optional preference marker detected in the prompt."),
        ]
        for marker, note in preference_markers:
            if marker in lowered and note not in notes:
                notes.append(note)
        return notes

    @staticmethod
    def _requires_comments(text: str) -> bool:
        positive_markers = (
            "добавь комментар",
            "добавить комментар",
            "с комментар",
            "include comment",
            "add comment",
            "with comment",
        )
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
        return any(marker in text for marker in positive_markers)

    @staticmethod
    def _has_persistence_requirement(text: str) -> bool:
        markers = (
            "save",
            "load",
            "persist",
            "serialization",
            "serialize",
            "deserialize",
            "сохран",
            "загруз",
        )
        return any(marker in text for marker in markers)

    @staticmethod
    def _prompt_requires_user_input(text: str) -> bool:
        lowered = text.lower()
        markers = (
            "ввод",
            "input",
            "io.read",
            "user input",
            "введите",
            "выберите",
            "enter ",
            "type ",
            "read from stdin",
            "stdin",
        )
        return any(marker in lowered for marker in markers)


AGENT_CLASS = ParseTaskAgentV1
