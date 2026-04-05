import json
import re
from abc import ABC, abstractmethod
from typing import Any

from config import RuntimeConfig
from llm_client import LocalChatModel
from lua_runtime import LuaToolchain
from state import AgentState, ensure_state_defaults


class BaseAgent(ABC):
    role: str
    version: str

    def __init__(
        self,
        *,
        model_client: LocalChatModel,
        lua_toolchain: LuaToolchain,
        runtime_config: RuntimeConfig,
    ) -> None:
        self.model_client = model_client
        self.lua_toolchain = lua_toolchain
        self.runtime_config = runtime_config

    @abstractmethod
    def run(self, state: AgentState) -> AgentState:
        raise NotImplementedError

    def with_defaults(self, state: AgentState) -> AgentState:
        return ensure_state_defaults(state)

    def ask_text(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        temperature: float | None = None,
    ) -> str:
        return self.model_client.chat(
            [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            temperature=temperature,
        )

    def ask_json(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        temperature: float | None = None,
    ) -> Any:
        response_text = self.ask_text(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            temperature=temperature,
        )
        return self.parse_json_response(response_text)

    @staticmethod
    def parse_json_response(text: str) -> Any:
        cleaned = text.strip()
        if cleaned.startswith("```"):
            cleaned = cleaned.split("\n", 1)[-1]
            if cleaned.endswith("```"):
                cleaned = cleaned[:-3].strip()

        try:
            return json.loads(cleaned)
        except json.JSONDecodeError:
            candidate = BaseAgent._extract_balanced_json(cleaned)
            return json.loads(candidate)

    @staticmethod
    def extract_code_block(text: str, language: str = "lua") -> str:
        fence = f"```{language}"
        if fence in text:
            tail = text.split(fence, 1)[1]
            return tail.split("```", 1)[0].strip()
        if "```" in text:
            tail = text.split("```", 1)[1]
            return tail.split("```", 1)[0].strip()
        return text.strip()

    @staticmethod
    def looks_like_json_payload(text: str) -> bool:
        cleaned = text.strip().lower()
        return (
            cleaned.startswith("{")
            or cleaned.startswith("[")
            or cleaned.startswith("```json")
            or cleaned.startswith("json\n{")
            or '"current_code"' in cleaned
            or '"parsed_spec"' in cleaned
            or '"implementation_plan"' in cleaned
        )

    @staticmethod
    def looks_like_lua_source(text: str) -> bool:
        lowered = text.strip().lower()
        lua_markers = (
            "function ",
            "local ",
            "return ",
            "end",
            "setmetatable",
            "coroutine.",
            "pcall(",
            "xpcall(",
            "io.read",
            "io.write",
            "print(",
        )
        return any(marker in lowered for marker in lua_markers)

    @staticmethod
    def to_prompt_json(data: Any) -> str:
        return json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True)

    @staticmethod
    def clip_text(text: str, *, max_chars: int) -> str:
        normalized = text.strip()
        if len(normalized) <= max_chars:
            return normalized
        keep_head = max_chars // 2
        keep_tail = max_chars - keep_head - len("\n...\n")
        return f"{normalized[:keep_head].rstrip()}\n...\n{normalized[-keep_tail:].lstrip()}"

    @classmethod
    def summarize_spec_for_prompt(
        cls,
        spec: dict[str, Any],
        *,
        max_chars: int = 3200,
    ) -> str:
        if any(key in spec for key in ("goal", "inputs", "outputs", "constraints", "assumptions", "success_criteria")):
            compact = {
                "goal": spec.get("goal", ""),
                "inputs": list(spec.get("inputs", []))[:8],
                "outputs": list(spec.get("outputs", []))[:8],
                "constraints": list(spec.get("constraints", []))[:10],
                "assumptions": list(spec.get("assumptions", []))[:8],
                "success_criteria": list(spec.get("success_criteria", []))[:10],
            }
        else:
            compact = {
                "task_summary": spec.get("task_summary", ""),
                "requested_behavior": list(spec.get("requested_behavior", []))[:10],
                "constraints": list(spec.get("constraints", []))[:8],
                "acceptance_criteria": list(spec.get("acceptance_criteria", []))[:8],
            }
        return cls.clip_text(cls.to_prompt_json(compact), max_chars=max_chars)

    @classmethod
    def summarize_plan_for_prompt(
        cls,
        plan: dict[str, Any],
        *,
        max_chars: int = 2200,
    ) -> str:
        if any(key in plan for key in ("goal", "steps", "components", "constraints", "assumptions")):
            compact = {
                "goal": plan.get("goal", ""),
                "steps": list(plan.get("steps", []))[:8],
                "components": list(plan.get("components", []))[:10],
                "constraints": list(plan.get("constraints", []))[:8],
                "assumptions": list(plan.get("assumptions", []))[:8],
            }
        else:
            compact = {
                "implementation_steps": list(plan.get("implementation_steps", []))[:8],
                "testing_steps": list(plan.get("testing_steps", []))[:6],
                "repair_strategy": list(plan.get("repair_strategy", []))[:6],
                "done_definition": list(plan.get("done_definition", []))[:8],
            }
        return cls.clip_text(cls.to_prompt_json(compact), max_chars=max_chars)

    @staticmethod
    def extract_minimum_line_count(text: str) -> int | None:
        patterns = [
            r"не\s+менее\s+(\d+)\s+строк",
            r"минимум\s+(\d+)\s+строк",
            r"at\s+least\s+(\d+)\s+lines",
            r"minimum\s+(\d+)\s+lines",
        ]
        for pattern in patterns:
            match = re.search(pattern, text, flags=re.IGNORECASE)
            if match:
                return int(match.group(1))
        return None

    @staticmethod
    def suggest_api_from_prompt(text: str) -> list[str]:
        lowered = text.lower()
        api = ["M.main"]
        suggestions = [
            (["инвентар", "inventory"], ["M.inventory", "M.add_item", "M.use_item"]),
            (["магазин", "shop", "торгов"], ["M.open_shop", "M.buy_item", "M.sell_item"]),
            (["бой", "battle", "enemy", "враг"], ["M.run_battle", "M.attack_enemy", "M.defend_turn"]),
            (["квест", "quest", "задан"], ["M.quest_log", "M.update_quests"]),
            (["локац", "location", "лес", "пещер", "деревн", "руин", "лагер"], ["M.locations", "M.visit_location", "M.generate_location_event"]),
            (["сохран", "save"], ["M.save_game"]),
            (["загруз", "load"], ["M.load_game"]),
            (["уров", "level", "опыт", "experience"], ["M.player", "M.apply_level_up"]),
            (
                ["scheduler", "task scheduler", "event-driven", "корутин", "coroutine", "event bus", "timer", "retry", "backoff", "priority queue"],
                [
                    "M.scheduler",
                    "M.run_demo",
                    "Scheduler.new",
                    "PriorityQueue.new",
                    "EventBus.new",
                    "Logger.new",
                ],
            ),
        ]
        for markers, exported_names in suggestions:
            if any(marker in lowered for marker in markers):
                api.extend(exported_names)
        return list(dict.fromkeys(api))

    @staticmethod
    def clean_lua_response(code: str) -> str:
        cleaned = code.replace("\r\n", "\n").strip()
        if not cleaned:
            return cleaned

        if cleaned.startswith("lua\n"):
            cleaned = cleaned[4:].lstrip()

        stop_markers = (
            "repair summary",
            "repair notes",
            "design notes",
            "summary:",
            "notes:",
            "explanation:",
            "пояснение:",
            "комментарий:",
            "комментарии:",
            "исправления:",
        )
        trimmed_lines: list[str] = []
        for line in cleaned.splitlines():
            if line.strip().lower().startswith(stop_markers):
                break
            trimmed_lines.append(line)

        cleaned = "\n".join(trimmed_lines).strip()
        lines = cleaned.splitlines()
        return_indices = [
            index for index, line in enumerate(lines)
            if re.match(r"^\s*return\s+M\s*$", line)
        ]
        if return_indices:
            cleaned = "\n".join(lines[: return_indices[-1] + 1]).strip()

        return cleaned

    @staticmethod
    def normalize_lua_code(code: str) -> str:
        normalized = BaseAgent.clean_lua_response(code)
        if not normalized:
            return normalized

        normalized = re.sub(
            r"(?m)^(\s*)(global|public|private|protected|export)\s+local\s+function\b",
            r"\1local function",
            normalized,
        )
        normalized = re.sub(
            r"(?m)^(\s*)(global|public|private|protected|export)\s+function\b",
            r"\1function",
            normalized,
        )
        normalized = re.sub(
            r"(?m)^\s*type\s+[A-Za-z_][A-Za-z0-9_]*\s*=.*(?:\n|$)",
            "",
            normalized,
        )
        normalized = re.sub(
            r"(?m)^(\s*local\s+[A-Za-z_][A-Za-z0-9_]*)\s*:\s*[^=\n]+(\s*=)",
            r"\1\2",
            normalized,
        )
        normalized = re.sub(
            r"(?m)^(\s*local\s+[A-Za-z_][A-Za-z0-9_]*)\s*:\s*[^\n]+$",
            r"\1",
            normalized,
        )
        normalized = re.sub(
            r"([,(]\s*[A-Za-z_][A-Za-z0-9_]*)\s*:\s*[^,)=]+",
            r"\1",
            normalized,
        )
        normalized = re.sub(
            r"\)\s*:\s*[A-Za-z_][A-Za-z0-9_%.<>{}:, |]*",
            ")",
            normalized,
        )

        normalized = re.sub(
            r"io\.read\(\s*(['\"])(n|l|a)\1\s*\)",
            lambda match: f"io.read({match.group(1)}*{match.group(2)}{match.group(1)})",
            normalized,
            flags=re.IGNORECASE,
        )

        uses_module = bool(
            re.search(r"\bM\.", normalized) or "return M" in normalized
        )
        defines_module_main = bool(re.search(r"function\s+M\.main\s*\(", normalized))
        defines_plain_main = bool(
            re.search(r"(?m)^\s*(?:local\s+)?function\s+main\s*\(", normalized)
        )
        uses_main = defines_module_main or defines_plain_main
        uses_run_mode = "__test__" in normalized or "run_mode" in normalized or uses_main

        prefixes: list[str] = []
        if uses_module and not re.search(r"(^|\n)\s*(local\s+)?M\s*=\s*\{\s*\}", normalized):
            prefixes.append("local M = {}")

        first_code_line = ""
        for line in normalized.splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("--"):
                continue
            first_code_line = stripped
            break

        if uses_run_mode and first_code_line != "local run_mode = ...":
            prefixes.append("local run_mode = ...")

        if prefixes:
            normalized = "\n".join(prefixes) + "\n\n" + normalized

        normalized = re.sub(
            r"(?m)^\s*if\s+\.\.\.\s*~=\s*(['\"])__test__\1\s*then\s*$",
            "if run_mode ~= '__test__' then",
            normalized,
        )

        deduplicated_lines: list[str] = []
        run_mode_seen = False
        for line in normalized.splitlines():
            if re.match(r"^\s*local\s+run_mode\s*=\s*\.\.\.\s*$", line):
                if run_mode_seen:
                    continue
                run_mode_seen = True
            deduplicated_lines.append(line)
        normalized = "\n".join(deduplicated_lines)

        has_any_test_guard = bool(
            re.search(
                r"if\s+(?:run_mode|\.\.\.)\s*~=\s*['\"]__test__['\"]",
                normalized,
                flags=re.DOTALL,
            )
        )

        if uses_main and not has_any_test_guard:
            normalized = re.sub(
                r"(?m)^\s*M\.main\s*\(\s*\)\s*$",
                "if run_mode ~= '__test__' then\n    M.main()\nend",
                normalized,
            )
            normalized = re.sub(
                r"(?m)^\s*main\s*\(\s*\)\s*$",
                "if run_mode ~= '__test__' then\n    main()\nend",
                normalized,
            )

        has_entrypoint_guard = bool(
            re.search(
                r"if\s+run_mode\s*~=\s*['\"]__test__['\"]\s*then\s*(?:M\.)?main\s*\(\s*\)\s*end",
                normalized,
                flags=re.DOTALL,
            )
        )
        if uses_main and not has_entrypoint_guard:
            entry_call = "M.main()" if defines_module_main else "main()"
            entrypoint = (
                "if run_mode ~= '__test__' then\n"
                f"    {entry_call}\n"
                "end"
            )
            if re.search(r"(?m)^\s*return\s+M\s*$", normalized):
                normalized = re.sub(
                    r"(?m)^\s*return\s+M\s*$",
                    f"{entrypoint}\n\nreturn M",
                    normalized,
                    count=1,
                )
            else:
                normalized += f"\n\n{entrypoint}"

        if uses_module and not re.search(r"\breturn\s+M\b", normalized):
            normalized += "\n\nreturn M"

        return normalized.strip() + "\n"

    @staticmethod
    def slugify_identifier(text: str, *, default: str = "unit") -> str:
        slug = re.sub(r"[^a-z0-9_]+", "_", text.lower())
        slug = re.sub(r"_+", "_", slug).strip("_")
        if not slug:
            slug = default
        if re.match(r"^\d", slug):
            slug = f"{default}_{slug}"
        return slug

    @classmethod
    def sanitize_lua_unit_fragment(cls, code: str) -> str:
        cleaned = cls.clean_lua_response(code)
        if not cleaned:
            return ""

        cleaned = re.sub(
            r"(?ms)^\s*if\s+(?:run_mode|\.\.\.)\s*~=\s*['\"]__test__['\"]\s*then\s*(?:M\.)?main\s*\(\s*\)\s*end\s*",
            "",
            cleaned,
        )
        cleaned = re.sub(
            r"(?m)^\s*local\s+run_mode\s*=\s*\.\.\.\s*$",
            "",
            cleaned,
        )
        cleaned = re.sub(
            r"(?m)^\s*(?:local\s+)?M\s*=\s*\{\s*\}\s*$",
            "",
            cleaned,
        )
        cleaned = re.sub(
            r"(?m)^\s*return\s+M\s*$",
            "",
            cleaned,
        )
        cleaned = re.sub(
            r"(?m)^\s*(?:M\.)?main\s*\(\s*\)\s*$",
            "",
            cleaned,
        )
        return cleaned.strip()

    @classmethod
    def assemble_lua_program_from_units(
        cls,
        code_units: list[dict[str, Any]],
    ) -> tuple[str, list[dict[str, Any]], list[dict[str, Any]]]:
        prelude = [
            "local run_mode = ...",
            "",
            "local M = {}",
            "",
        ]
        lines = list(prelude)
        normalized_units: list[dict[str, Any]] = []
        code_unit_map: list[dict[str, Any]] = []

        for index, raw_unit in enumerate(code_units, start=1):
            if not isinstance(raw_unit, dict):
                continue

            unit_name = cls.slugify_identifier(
                str(raw_unit.get("name", "")).strip() or f"unit_{index}",
                default=f"unit_{index}",
            )
            purpose = str(raw_unit.get("purpose", "")).strip()
            dependencies = raw_unit.get("dependencies", [])
            if not isinstance(dependencies, list):
                dependencies = [dependencies]
            dependencies = [str(item).strip() for item in dependencies if str(item).strip()]

            unit_code = cls.sanitize_lua_unit_fragment(str(raw_unit.get("code", "")))
            if not unit_code:
                continue

            start_line = len(lines) + 1
            lines.extend(unit_code.splitlines())
            end_line = len(lines)
            lines.append("")

            normalized_unit = {
                "name": unit_name,
                "purpose": purpose,
                "dependencies": dependencies,
                "code": unit_code,
            }
            normalized_units.append(normalized_unit)
            code_unit_map.append(
                {
                    "name": unit_name,
                    "purpose": purpose,
                    "start_line": start_line,
                    "end_line": end_line,
                }
            )

        assembled_body = "\n".join(lines).rstrip()
        has_module_main = any(
            re.search(r"(?m)^\s*function\s+M\.main\s*\(", unit["code"])
            for unit in normalized_units
        )
        has_plain_main = any(
            re.search(r"(?m)^\s*(?:local\s+)?function\s+main\s*\(", unit["code"])
            for unit in normalized_units
        )

        footer_lines: list[str]
        if has_module_main:
            footer_lines = [
                "",
                "if run_mode ~= '__test__' then",
                "    M.main()",
                "end",
                "",
                "return M",
            ]
        elif has_plain_main:
            footer_lines = [
                "",
                "if run_mode ~= '__test__' then",
                "    main()",
                "end",
                "",
                "return M",
            ]
        else:
            footer_lines = [
                "",
                "return M",
            ]

        assembled_code = assembled_body + "\n" + "\n".join(footer_lines)
        return cls.normalize_lua_code(assembled_code), code_unit_map, normalized_units

    @staticmethod
    def _extract_balanced_json(text: str) -> str:
        for start_index, character in enumerate(text):
            if character not in "{[":
                continue

            stack = ["}" if character == "{" else "]"]
            in_string = False
            escaped = False

            for end_index in range(start_index + 1, len(text)):
                current = text[end_index]

                if in_string:
                    if escaped:
                        escaped = False
                    elif current == "\\":
                        escaped = True
                    elif current == '"':
                        in_string = False
                    continue

                if current == '"':
                    in_string = True
                    continue

                if current == "{":
                    stack.append("}")
                elif current == "[":
                    stack.append("]")
                elif current in "}]":
                    if not stack or current != stack[-1]:
                        break
                    stack.pop()
                    if not stack:
                        return text[start_index : end_index + 1]

        raise ValueError("Could not extract a valid JSON payload from the model response.")
