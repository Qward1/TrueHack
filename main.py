#!/usr/bin/env python3
import argparse
import json
import os
import re
import sys
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone

from auto_fix_lua import (
    DEFAULT_LUA_BIN,
    DEFAULT_LUACHECK_BIN,
    DEFAULT_MAX_ATTEMPTS,
    DEFAULT_REQUEST_TIMEOUT,
    DEFAULT_STARTUP_TIMEOUT,
    DEFAULT_TEMPERATURE,
    infer_program_mode,
    is_tooling_problem,
    repair_mojibake,
    request_fixed_code,
    run_diagnostics,
)
from console_utils import configure_console_utf8
from generate import (
    DEFAULT_SYSTEM_PROMPT as GENERATE_SYSTEM_PROMPT,
    analyze_lua_response,
    build_strict_system_prompt,
    build_payload,
    normalize_lua_code,
    request_lua_code,
    save_lua_code,
)
from lmstudio_client import DEFAULT_MODEL, DEFAULT_URL, request_chat_completion
from prompt_verifier import verify_prompt_requirements


DEFAULT_OUTPUT = "solution.lua"
EDIT_SYSTEM_PROMPT = (
    "You update an existing Lua program according to the user's latest change request. "
    "Return only the full updated Lua code without markdown fences, explanations, or extra text. "
    "Preserve working parts unless the request requires changes. "
    "For Windows console apps, prefer ASCII-only UI text unless Unicode support is configured explicitly."
)
PATH_CANDIDATE_PATTERN = re.compile(
    r'"((?:[A-Za-z]:[\\/]|\.{1,2}[\\/]|[\\/]|(?:[A-Za-z0-9_.-]+[\\/])+).+?)"|'
    r"'((?:[A-Za-z]:[\\/]|\.{1,2}[\\/]|[\\/]|(?:[A-Za-z0-9_.-]+[\\/])+).+?)'|"
    r"((?:[A-Za-z]:[\\/]|\.{1,2}[\\/]|[\\/]|(?:[A-Za-z0-9_.-]+[\\/])+)[^\s,;]+)"
)
PATH_CONTEXT_KEYWORDS = (
    "path",
    "folder",
    "directory",
    "save to",
    "save in",
    "sohrani v",
    "sohranit v",
    "po puti",
    "v papke",
    "v papku",
    "v direktorii",
    "v direktoriyu",
    "v kataloge",
    "v katalog",
    "katalog",
    "direktoriya",
    "put ",
    "put:",
    "puti ",
)
DIRECT_PATH_CONTEXT_SUFFIXES = (
    " v",
    " vo",
    " in",
    " to",
    " at",
)
SLUG_STOP_WORDS = {
    "a",
    "an",
    "and",
    "app",
    "application",
    "build",
    "code",
    "create",
    "directory",
    "file",
    "folder",
    "for",
    "generate",
    "in",
    "lua",
    "make",
    "new",
    "path",
    "please",
    "program",
    "project",
    "save",
    "script",
    "the",
    "to",
    "write",
    "v",
    "vo",
    "na",
    "po",
    "dlya",
    "dla",
    "ili",
    "nuzhno",
    "nuzhna",
    "nuzhen",
    "pozhaluista",
    "sozdat",
    "sozdai",
    "sozdaj",
    "sozday",
    "sdelay",
    "sdelai",
    "sdelat",
    "napishi",
    "napisat",
    "sohrani",
    "sohranit",
    "fayl",
    "fail",
    "papka",
    "papke",
    "papku",
    "direktoriya",
    "direktorii",
    "direktoriyu",
    "katalog",
    "put",
    "puti",
    "skript",
    "kod",
    "programmu",
    "programma",
    "proekt",
    "prilozhenie",
    "igra",
    "igru",
    "zadachu",
    "zadanie",
    "nahoditsya",
    "rasskazhi",
    "obyasni",
    "opishi",
    "chto",
    "on",
    "ona",
    "delaet",
    "etot",
    "eta",
    "ispolzuy",
    "isprav",
    "izmeni",
    "dobav",
    "obnovi",
    "peredelay",
    "redaktiruy",
    "faylu",
    "fayla",
    "faila",
    "papke",
    "papki",
    "direktorii",
    "foldere",
    "workdir",
}
TITLE_STOP_WORDS = {
    "а", "и", "или", "в", "во", "на", "по", "под", "над", "у", "к", "ко", "с", "со",
    "для", "из", "от", "до", "про", "это", "этот", "эта", "эти", "того", "там",
    "что", "он", "она", "оно", "они", "который", "которая", "которые",
    "создай", "сделай", "напиши", "сгенерируй", "исправь", "измени", "добавь", "обнови",
    "переделай", "доработай", "внеси", "используй", "работай", "расскажи", "объясни",
    "опиши", "разбери", "проанализируй", "находится", "файл", "файле", "файла",
    "папке", "папка", "директории", "директория", "каталоге", "каталог",
    "lua", "скрипт", "скрипта", "код", "программа", "приложение", "проект",
    "the", "a", "an", "and", "or", "to", "in", "on", "for", "with", "use",
    "create", "build", "generate", "fix", "edit", "update", "modify", "review",
    "describe", "explain", "what", "does", "file", "folder", "directory", "script",
}
CYRILLIC_TO_LATIN = {
    "\u0430": "a",
    "\u0431": "b",
    "\u0432": "v",
    "\u0433": "g",
    "\u0434": "d",
    "\u0435": "e",
    "\u0451": "yo",
    "\u0436": "zh",
    "\u0437": "z",
    "\u0438": "i",
    "\u0439": "y",
    "\u043a": "k",
    "\u043b": "l",
    "\u043c": "m",
    "\u043d": "n",
    "\u043e": "o",
    "\u043f": "p",
    "\u0440": "r",
    "\u0441": "s",
    "\u0442": "t",
    "\u0443": "u",
    "\u0444": "f",
    "\u0445": "h",
    "\u0446": "ts",
    "\u0447": "ch",
    "\u0448": "sh",
    "\u0449": "sch",
    "\u044a": "",
    "\u044b": "y",
    "\u044c": "",
    "\u044d": "e",
    "\u044e": "yu",
    "\u044f": "ya",
}
CONTEXT_FILE_NAME = ".lua_console_chat.json"
MAX_CONTEXT_CHANGE_ITEMS = 8
MAX_CONTEXT_CHANGE_LENGTH = 240
MAX_CONTEXT_FILE_SUMMARIES = 12
MAX_CONTEXT_FILE_SUMMARY_LENGTH = 220
MAX_DISCOVERED_LUA_FILES = 64
SOFT_VERIFICATION_PASS_SCORE = 70
MAX_REQUIREMENT_FIX_ATTEMPTS = 2
MIN_REQUIREMENT_SCORE_IMPROVEMENT = 5
SCAN_SKIP_DIRS = {".git", "__pycache__", ".venv", "venv"}
TEXT_ARTIFACT_EXTENSIONS = {".lua", ".md", ".markdown", ".json", ".txt"}
TEXT_SCAN_EXTENSION_HINTS = {
    ".cfg",
    ".conf",
    ".csv",
    ".env",
    ".ini",
    ".log",
    ".lua",
    ".md",
    ".markdown",
    ".json",
    ".toml",
    ".txt",
    ".xml",
    ".yaml",
    ".yml",
}
GENERIC_TEXT_FILE_NAMES = {"readme", "notes", "todo", "changelog"}
MAX_TEXT_SNIFF_BYTES = 4096
CREATE_INTENT_MARKERS = (
    "создай",
    "сделай",
    "напиши",
    "сгенерируй",
    "sozday",
    "sozdai",
    "napishi",
    "sgeneriruy",
    "generate",
    "create",
    "build",
    "new file",
    "новый файл",
    "новый скрипт",
)
CHANGE_INTENT_MARKERS = (
    "измени",
    "исправь",
    "добавь",
    "обнови",
    "переделай",
    "доработай",
    "внеси",
    "редактируй",
    "используй",
    "работай с",
    "izmeni",
    "isprav",
    "dobav",
    "obnovi",
    "peredelay",
    "dorabotay",
    "vnesi",
    "redaktiruy",
    "ispolzuy",
    "rabotay s",
    "fix",
    "edit",
    "update",
    "modify",
    "improve",
    "use this file",
)
INSPECT_INTENT_MARKERS = (
    "расскажи",
    "объясни",
    "опиши",
    "разбери",
    "проанализируй",
    "что делает",
    "что делает файл",
    "что он делает",
    "rasskazhi",
    "obyasni",
    "opishi",
    "razberi",
    "proanaliziruy",
    "chto delaet",
    "chto on delaet",
    "review",
    "ревью",
    "анализ",
)
README_INTENT_MARKERS = (
    "readme",
    "read me",
    "documentation",
    "docs",
    "manual",
    "guide",
    "instruction",
    "opisanie proekta",
    "dokumentac",
    "rukovodstv",
    "instrukc",
)
EXPLAIN_SYSTEM_PROMPT = (
    "You explain what an existing Lua file does. "
    "Reply in Russian. "
    "Be concrete and accurate. "
    "Describe the program purpose, the main flow, the key functions/data, user input/output, "
    "and any obvious risks or quality issues visible in the code. "
    "Do not invent behavior that is not present in the file."
)
DOCUMENT_GENERATE_SYSTEM_PROMPT = (
    "You write project documentation files such as README.md. "
    "Return only the full final Markdown document without markdown fences, explanations, or extra text outside the document. "
    "Keep it concrete and aligned with the real project behavior visible in the provided context."
)
DOCUMENT_EDIT_SYSTEM_PROMPT = (
    "You update an existing README.md or project documentation file. "
    "Return only the full updated Markdown document without markdown fences, explanations, or extra text outside the document. "
    "Preserve correct sections unless the user's request requires changes."
)
DOCUMENT_EXPLAIN_SYSTEM_PROMPT = (
    "You explain what an existing project document or README file contains. "
    "Reply in Russian. "
    "Summarize the purpose of the document, its structure, the key sections, and any obvious gaps or inaccuracies. "
    "Do not invent content that is not present in the file."
)
TEXT_ARTIFACT_GENERATE_SYSTEM_PROMPT = (
    "You write a single text file for a local project workspace. "
    "The target can be README, Markdown, JSON, TXT, INI, YAML, TOML, LOG, or another text file. "
    "Return only the full final file content without markdown fences, explanations, or extra text outside the file."
)
TEXT_ARTIFACT_EDIT_SYSTEM_PROMPT = (
    "You update one existing text file in a local project workspace. "
    "Preserve correct structure for the file type. "
    "Return only the full updated file content without markdown fences, explanations, or extra text."
)
TEXT_ARTIFACT_EXPLAIN_SYSTEM_PROMPT = (
    "You explain what an existing text file contains. "
    "Reply in Russian. "
    "Summarize the file purpose, the main sections or keys, and any obvious gaps or risks visible in the content. "
    "Do not invent content that is not present in the file."
)
REQUEST_ROUTE_SYSTEM_PROMPT = (
    "You classify the user's request for a local coding assistant. "
    "Return only one JSON object and nothing else. "
    "Valid intent values: create, change, inspect. "
    "Valid artifact_type values: lua, readme. "
    "Set expects_existing_target to true only when the user clearly refers to an existing file, folder, or project that should be inspected or modified. "
    "Use artifact_type=readme when the main deliverable is README or documentation. "
    "Use intent=inspect when the user wants an explanation, review, or analysis. "
    "If the chat already has an active project and the new message is a follow-up modification, prefer intent=change. "
    "If the user asks to write or update README or documentation, prefer artifact_type=readme and preferred_filename=README.md. "
    "Required JSON keys: intent, artifact_type, expects_existing_target, preferred_filename, reason."
)
REQUEST_SEMANTICS_SYSTEM_PROMPT = (
    "You parse a user's request for a local workspace assistant. "
    "Return only one JSON object and nothing else. "
    "Do not resolve filesystem state. Only extract the user's semantic intent. "
    "Valid intent values: create, change, inspect. "
    "Valid requested_entity_type values: file, directory, project, unknown. "
    "Valid requested_artifact_kind values: lua, readme, json, txt, markdown, generic_file, directory, unknown. "
    "Set follow_active_context=true when the request is a follow-up that should continue working with the current active target or directory. "
    "Set expects_existing_target=true when the request logically refers to an existing artifact that should be inspected or changed. "
    "Set create_if_missing=true only when it is acceptable to create a new target if none exists. "
    "If the request names README, prefer requested_artifact_kind=readme and requested_entity_type=file. "
    "If the request names a generic file like settings.ini or schema.yaml, use requested_artifact_kind=generic_file. "
    "If the request is about creating a directory/folder, use requested_entity_type=directory and requested_artifact_kind=directory. "
    "If the user asks to create a program, script, calculator, implementation, README, config, or file in a folder/path, "
    "that folder is target_directory scope, not the deliverable itself. "
    "Example: 'Напиши инженерный калькулятор в папке C:\\\\Test' means create a file/project inside C:\\\\Test, not create the folder as the main target. "
    "Example: 'Создай папку C:\\\\Test123' means requested_entity_type=directory. "
    "Required JSON keys: intent, requested_entity_type, requested_artifact_kind, target_directory, follow_active_context, expects_existing_target, create_if_missing, reason."
)
LUA_PATH_CANDIDATE_PATTERN = re.compile(
    r'"([^"\r\n]+?\.lua)"|'
    r"'([^'\r\n]+?\.lua)'|"
    r"((?:[A-Za-z]:[\\/]|\.{1,2}[\\/]|[\\/]|(?:[A-Za-z0-9_.-]+[\\/])+)[^\s,;]+?\.lua)"
)
LUA_FILE_NAME_PATTERN = re.compile(r"(?<![A-Za-z0-9_.-])([A-Za-z0-9_.-]+\.lua)(?![A-Za-z0-9_.-])")
MARKDOWN_PATH_CANDIDATE_PATTERN = re.compile(
    r'"([^"\r\n]+?\.md)"|'
    r"'([^'\r\n]+?\.md)'|"
    r"((?:[A-Za-z]:[\\/]|\.{1,2}[\\/]|[\\/]|(?:[A-Za-z0-9_.-]+[\\/])+)[^\s,;]+?\.md)"
)
MARKDOWN_FILE_NAME_PATTERN = re.compile(r"(?<![A-Za-z0-9_.-])([A-Za-z0-9_.-]+\.md)(?![A-Za-z0-9_.-])")
GENERIC_FILE_NAME_PATTERN = re.compile(r"(?<![A-Za-z0-9_.-])([A-Za-z0-9_.-]+\.[A-Za-z0-9]{1,10})(?![A-Za-z0-9_.-])")
EXTENSION_PATTERN = re.compile(r"(?<![A-Za-z0-9_])(\.[A-Za-z0-9]{1,10})(?![A-Za-z0-9_])")
MAX_DOCUMENT_CODE_CONTEXT = 4000


@dataclass
class SessionReport:
    success: bool
    action: str
    attempts: int
    output_path: str
    working_path: str
    saved_output: bool
    diagnostics: dict
    verification: dict | None = None
    message: str = ""


@dataclass
class SessionState:
    workspace_root: str
    context_path: str
    output_path: str
    working_path: str
    chat_id: str = ""
    base_prompt: str = ""
    change_requests: list[str] = field(default_factory=list)
    artifacts: list["ArtifactRef"] = field(default_factory=list)
    active_target_id: str = ""
    active_directory_id: str = ""
    current_content: str = ""
    current_code: str = ""
    last_report: SessionReport | None = None
    last_resolution: "ResolutionResult | None" = None
    managed_files: list[str] = field(default_factory=list)
    current_target_path: str = ""
    artifact_type: str = "lua"
    last_route_intent: str = "create"

    def has_project(self) -> bool:
        return bool(self.base_prompt.strip())

    def effective_prompt(self) -> str:
        if not self.base_prompt.strip():
            return ""

        if not self.change_requests:
            return self.base_prompt

        lines = ["Original user request:", self.base_prompt, "", "Additional change requests:"]
        for index, item in enumerate(self.change_requests, start=1):
            lines.append(f"{index}. {item}")
        return "\n".join(lines)


@dataclass
class TargetSelection:
    output_path: str
    working_path: str
    current_code: str
    explicit: bool
    exists: bool
    source: str
    entity_type: str = "file"
    artifact_kind: str = "unknown"
    restored_from_context: bool = False
    artifact_id: str = ""
    directory_id: str = ""
    confidence: float = 0.0
    reasons: list[str] = field(default_factory=list)
    requested_artifact_kind: str = "unknown"


@dataclass
class RequestRoute:
    intent: str = "create"
    artifact_type: str = "lua"
    expects_existing_target: bool = False
    preferred_filename: str = ""
    reason: str = ""


@dataclass
class ParsedRequestSemantics:
    intent: str = "create"
    requested_entity_type: str = "unknown"
    requested_artifact_kind: str = "unknown"
    explicit_path: str = ""
    explicit_filename: str = ""
    explicit_extension: str = ""
    target_directory: str = ""
    follow_active_context: bool = False
    expects_existing_target: bool = False
    create_if_missing: bool = False
    reason: str = ""


@dataclass
class ArtifactRef:
    id: str
    entity_type: str
    artifact_kind: str
    path: str
    exists: bool
    role: str
    extension: str
    summary: str = ""
    pinned: bool = False


@dataclass
class WorkspaceInventory:
    workspace_root: str
    artifacts: list[ArtifactRef] = field(default_factory=list)
    scanned_at: str = ""


@dataclass
class ResolutionResult:
    target_id: str = ""
    target_path: str = ""
    intent: str = "create"
    confidence: float = 0.0
    source: str = "fallback"
    reasons: list[str] = field(default_factory=list)
    requested_artifact_kind: str = "unknown"


def output_argument_was_provided(argv: list[str] | None = None) -> bool:
    arguments = sys.argv[1:] if argv is None else argv
    return any(argument == "--output" or argument.startswith("--output=") for argument in arguments)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Interactive console for Lua generation, validation, automatic fixing, "
            "and iterative edits through LM Studio."
        )
    )
    parser.add_argument(
        "prompt",
        nargs="?",
        help="Optional initial prompt. If omitted, the console starts and waits for chat input.",
    )
    parser.add_argument(
        "--output",
        default=DEFAULT_OUTPUT,
        help="Final Lua file path that is written after successful checks.",
    )
    parser.add_argument(
        "--model",
        default=os.getenv("LMSTUDIO_MODEL", DEFAULT_MODEL),
        help="Model name loaded in LM Studio.",
    )
    parser.add_argument(
        "--verify-model",
        default="",
        help="Model name for the requirements-check step. Defaults to --model.",
    )
    parser.add_argument(
        "--url",
        default=os.getenv("LMSTUDIO_URL", DEFAULT_URL),
        help="LM Studio chat completions endpoint.",
    )
    parser.add_argument(
        "--temperature",
        type=float,
        default=DEFAULT_TEMPERATURE,
        help="Sampling temperature for generation, edits, and fixes.",
    )
    parser.add_argument(
        "--lua-bin",
        default=DEFAULT_LUA_BIN,
        help="Lua interpreter executable name or path.",
    )
    parser.add_argument(
        "--luacheck-bin",
        default=DEFAULT_LUACHECK_BIN,
        help="luacheck executable name or path.",
    )
    parser.add_argument(
        "--max-attempts",
        type=int,
        default=DEFAULT_MAX_ATTEMPTS,
        help="Maximum validation/fix iterations per user request.",
    )
    parser.add_argument(
        "--startup-timeout",
        type=float,
        default=DEFAULT_STARTUP_TIMEOUT,
        help="Seconds to wait for Lua startup before treating an active console app as started.",
    )
    parser.add_argument(
        "--request-timeout",
        type=float,
        default=DEFAULT_REQUEST_TIMEOUT,
        help="Seconds to wait for each LM Studio request.",
    )
    parser.add_argument(
        "--skip-verification",
        action="store_true",
        help="Skip the LLM requirements-check step.",
    )
    return parser.parse_args()


def build_working_path(output_path: str) -> str:
    base, extension = os.path.splitext(output_path)
    if extension:
        return f"{base}.working{extension}"
    return f"{output_path}.working.lua"


def cleanup_legacy_working_file(output_path: str) -> None:
    legacy_path = build_working_path(output_path)
    if os.path.exists(legacy_path):
        try:
            os.remove(legacy_path)
        except OSError:
            pass


def clean_path_candidate(candidate: str) -> str:
    return candidate.strip().strip("\"'").rstrip(".,;:!?)]}").strip()


def resolve_prompt_path(candidate: str, base_dir: str) -> str:
    expanded = os.path.expandvars(os.path.expanduser(candidate))
    if os.path.isabs(expanded):
        return os.path.abspath(expanded)
    return os.path.abspath(os.path.join(base_dir, expanded))


def artifact_kind_from_extension(extension: str) -> str:
    normalized = extension.lower().strip()
    if not normalized:
        return "unknown"
    if not normalized.startswith("."):
        normalized = f".{normalized}"
    if normalized == ".lua":
        return "lua"
    if normalized == ".md":
        return "markdown"
    if normalized == ".markdown":
        return "markdown"
    if normalized == ".json":
        return "json"
    if normalized == ".txt":
        return "txt"
    return "generic_file"


def normalize_requested_artifact_kind(kind: str, explicit_filename: str = "", explicit_extension: str = "") -> str:
    normalized = str(kind or "").strip().lower()
    if explicit_filename:
        file_name = os.path.basename(explicit_filename).lower()
        if file_name == "readme.md":
            return "readme"
    if normalized in {"lua", "readme", "json", "txt", "markdown", "generic_file", "directory"}:
        return normalized
    if explicit_extension:
        inferred = artifact_kind_from_extension(explicit_extension)
        if inferred == "markdown" and explicit_filename and os.path.basename(explicit_filename).lower() == "readme.md":
            return "readme"
        return inferred
    return "unknown"


def normalize_requested_entity_type(entity_type: str, artifact_kind: str = "") -> str:
    normalized = str(entity_type or "").strip().lower()
    if normalized in {"file", "directory", "project"}:
        return normalized
    if artifact_kind == "directory":
        return "directory"
    if artifact_kind in {"lua", "readme", "json", "txt", "markdown", "generic_file"}:
        return "file"
    return "unknown"


def is_text_artifact_kind(artifact_kind: str) -> bool:
    return artifact_kind in {"readme", "markdown", "json", "txt", "generic_file"}


def is_probably_text_file(path: str) -> bool:
    if not path or not os.path.isfile(path):
        return False
    extension = os.path.splitext(path)[1].lower()
    if extension in TEXT_SCAN_EXTENSION_HINTS:
        return True
    try:
        with open(path, "rb") as file:
            chunk = file.read(MAX_TEXT_SNIFF_BYTES)
    except OSError:
        return False
    if b"\x00" in chunk:
        return False
    if not chunk:
        return True
    for encoding in ("utf-8", "cp1251", "latin-1"):
        try:
            chunk.decode(encoding)
            return True
        except UnicodeDecodeError:
            continue
    return False


def extract_explicit_path(prompt: str, workspace_root: str) -> str:
    requested_matches = iter_requested_paths(prompt)
    if requested_matches:
        return resolve_prompt_path(requested_matches[0][2], workspace_root)
    raw_matches = iter_path_candidates(prompt)
    if len(raw_matches) == 1:
        return resolve_prompt_path(raw_matches[0][2], workspace_root)
    return ""


def extract_explicit_filename(prompt: str) -> str:
    candidates: list[str] = []
    explicit_path = extract_explicit_lua_path_candidate(prompt)
    if explicit_path:
        candidates.append(os.path.basename(explicit_path))
    markdown_path = extract_explicit_markdown_path_candidate(prompt)
    if markdown_path:
        candidates.append(os.path.basename(markdown_path))
    for match in GENERIC_FILE_NAME_PATTERN.finditer(prompt):
        candidates.append(clean_path_candidate(match.group(1)))
    seen: set[str] = set()
    for candidate in candidates:
        cleaned = os.path.basename(candidate)
        if not cleaned or cleaned.lower().endswith(".working.lua"):
            continue
        key = cleaned.lower()
        if key in seen:
            continue
        seen.add(key)
        return cleaned
    return ""


def extract_explicit_extension(prompt: str, explicit_filename: str = "", explicit_path: str = "") -> str:
    if explicit_filename:
        extension = os.path.splitext(explicit_filename)[1].lower()
        if extension:
            return extension
    if explicit_path:
        extension = os.path.splitext(explicit_path)[1].lower()
        if extension:
            return extension
    match = EXTENSION_PATTERN.search(prompt)
    if match:
        return match.group(1).lower()
    return ""


def extract_explicit_lua_path_candidate(prompt: str) -> str | None:
    for match in LUA_PATH_CANDIDATE_PATTERN.finditer(prompt):
        raw_path = next((group for group in match.groups() if group), "")
        candidate = clean_path_candidate(raw_path)
        if candidate:
            return candidate
    return None


def iter_path_candidates(prompt: str) -> list[tuple[int, int, str]]:
    matches: list[tuple[int, int, str]] = []
    for match in PATH_CANDIDATE_PATTERN.finditer(prompt):
        raw_path = next((group for group in match.groups() if group), "")
        candidate = clean_path_candidate(raw_path)
        if candidate:
            matches.append((match.start(), match.end(), candidate))
    return matches


def looks_like_direct_location_reference(context: str, normalized_context: str) -> bool:
    stripped_context = context.rstrip().lower()
    stripped_normalized = normalized_context.rstrip().lower()
    if stripped_context.endswith((" в", " во", " in", " to", " at")):
        return True
    return any(stripped_normalized.endswith(suffix) for suffix in DIRECT_PATH_CONTEXT_SUFFIXES)


def iter_requested_paths(prompt: str) -> list[tuple[int, int, str]]:
    matches: list[tuple[int, int, str]] = []
    for start, end, candidate in iter_path_candidates(prompt):
        raw_path = candidate
        match_start = start
        match_end = end
        context = prompt[max(0, match_start - 80):match_start].lower()
        normalized_context = transliterate_for_slug(context)
        has_context_keyword = any(
            keyword in context or keyword in normalized_context
            for keyword in PATH_CONTEXT_KEYWORDS
        )
        if not has_context_keyword and not looks_like_direct_location_reference(context, normalized_context):
            continue

        if candidate:
            matches.append((match_start, match_end, candidate))
    return matches


def strip_requested_paths(prompt: str) -> str:
    cleaned_prompt = prompt
    for start, end, _ in reversed(iter_path_candidates(prompt)):
        cleaned_prompt = f"{cleaned_prompt[:start]} {cleaned_prompt[end:]}"
    return cleaned_prompt


def normalize_prompt_for_naming(prompt: str) -> str:
    cleaned_prompt = strip_requested_paths(prompt)
    cleaned_prompt = re.sub(r"[\"'`“”«»]", " ", cleaned_prompt)
    cleaned_prompt = re.sub(r"\s+", " ", cleaned_prompt)
    return cleaned_prompt.strip()


def transliterate_for_slug(text: str) -> str:
    parts: list[str] = []
    for char in text.lower():
        if char in CYRILLIC_TO_LATIN:
            parts.append(CYRILLIC_TO_LATIN[char])
        elif char.isascii():
            parts.append(char)
        else:
            parts.append(" ")
    return "".join(parts)


def extract_original_title_words(text: str, max_words: int = 5) -> list[str]:
    tokens = re.findall(r"[A-Za-zА-Яа-яЁё0-9]+", text.lower())
    words = [token for token in tokens if len(token) > 1 and token not in TITLE_STOP_WORDS]
    if not words:
        words = [token for token in tokens if len(token) > 1]
    return words[:max_words]


def build_task_slug(prompt: str) -> str:
    cleaned_prompt = normalize_prompt_for_naming(prompt)
    transliterated = transliterate_for_slug(cleaned_prompt)
    tokens = re.findall(r"[a-z0-9]+", transliterated)
    filtered = [
        token
        for token in tokens
        if token.isdigit() or (len(token) > 1 and token not in SLUG_STOP_WORDS)
    ]

    if not filtered:
        filtered = [token for token in tokens if token != "lua"]
    if not filtered:
        return "lua_project"

    slug = "_".join(filtered[:2]).strip("_")
    return slug[:80].rstrip("_") or "lua_project"


def build_chat_title_from_prompt(prompt: str, fallback: str = "Новый чат") -> str:
    cleaned_prompt = normalize_prompt_for_naming(prompt)
    words = extract_original_title_words(cleaned_prompt, max_words=5)
    if not words and cleaned_prompt:
        words = re.findall(r"[A-Za-zА-Яа-яЁё0-9]+", cleaned_prompt)[:5]
    if not words:
        return fallback

    if len(words) == 1 and len(cleaned_prompt.split()) > 1:
        extra_words = [word for word in re.findall(r"[A-Za-zА-Яа-яЁё0-9]+", cleaned_prompt.lower()) if len(word) > 1]
        for word in extra_words:
            if word not in words:
                words.append(word)
            if len(words) >= 2:
                break

    return " ".join(word.capitalize() for word in words[:5]).strip() or fallback

def looks_like_file_path(path: str) -> bool:
    if not path:
        return False
    file_name = os.path.basename(path.rstrip("\\/"))
    root, extension = os.path.splitext(file_name)
    known_extensions = {".lua", ".md", ".txt", ".json", ".yaml", ".yml", ".toml", ".ini", ".cfg"}
    return bool(root and extension.lower() in known_extensions)


def extract_requested_output_directory(prompt: str) -> str | None:
    requested_matches = iter_requested_paths(prompt)
    if not requested_matches:
        raw_matches = iter_path_candidates(prompt)
        if len(raw_matches) == 1:
            requested_matches = raw_matches

    for _, _, candidate in requested_matches:
        expanded = os.path.expandvars(os.path.expanduser(candidate))
        if looks_like_file_path(expanded):
            expanded = os.path.dirname(expanded)
        if expanded:
            return os.path.abspath(expanded)
    return None


def prompt_mentions_lua_location(prompt: str) -> bool:
    return bool(extract_explicit_lua_path_candidate(prompt) or extract_requested_output_directory(prompt))


def classify_request_intent(prompt: str) -> str:
    normalized = " ".join(prompt.lower().split())
    if not normalized:
        return "create"

    inspect_hints = ("объясн", "обьясн", "расскажи", "что делает", "разбери", "опиши", "анализ", "review")
    change_hints = ("измени", "исправ", "добав", "обнов", "передел", "доработ", "внеси", "редакт", "используй", "работай с")
    create_hints = ("создай", "сделай", "напиши", "сгенерируй", "create", "build", "generate")

    has_inspect = any(marker in normalized for marker in INSPECT_INTENT_MARKERS) or any(marker in normalized for marker in inspect_hints)
    has_change = any(marker in normalized for marker in CHANGE_INTENT_MARKERS) or any(marker in normalized for marker in change_hints)
    has_create = any(marker in normalized for marker in CREATE_INTENT_MARKERS) or any(marker in normalized for marker in create_hints)

    if has_inspect and not has_change and not has_create:
        return "inspect"
    if has_change:
        return "change"
    if has_create:
        return "create"
    if has_inspect:
        return "inspect"
    return "create"


def expects_existing_target(prompt: str) -> bool:
    return classify_request_intent(prompt) in {"inspect", "change"} and prompt_mentions_lua_location(prompt)


def prompt_mentions_readme_location(prompt: str) -> bool:
    normalized = " ".join(prompt.lower().split())
    return bool(
        extract_explicit_markdown_path_candidate(prompt)
        or MARKDOWN_FILE_NAME_PATTERN.search(prompt)
        or extract_requested_output_directory(prompt)
        or "readme.md" in normalized
    )


def classify_artifact_type(prompt: str) -> str:
    normalized = " ".join(prompt.lower().split())
    if not normalized:
        return "lua"
    readme_hints = ("readme", "документац", "документа", "руководств", "инструкц", "описание проекта")
    if any(marker in normalized for marker in README_INTENT_MARKERS) or any(marker in normalized for marker in readme_hints):
        return "readme"
    if extract_explicit_markdown_target(prompt):
        return "readme"
    return "lua"


def expects_existing_target_for_route(prompt: str, intent: str, artifact_type: str) -> bool:
    if intent not in {"inspect", "change"}:
        return False
    if artifact_type == "readme":
        if intent == "inspect":
            return prompt_mentions_readme_location(prompt) or "readme" in prompt.lower()
        return prompt_mentions_readme_location(prompt)
    return prompt_mentions_lua_location(prompt)


def parse_first_json_object(text: str) -> dict | None:
    if not text:
        return None
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        payload = json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, dict) else None


def build_route_context_summary(state: SessionState) -> str:
    lines = [
        f"Active chat has project: {'yes' if state.has_project() else 'no'}",
        f"Current artifact type: {state.artifact_type}",
    ]
    if state.current_target_path:
        lines.append(f"Current target path: {relative_display_path(state.current_target_path, state.workspace_root)}")
    if state.base_prompt.strip():
        lines.append(f"Base prompt summary: {truncate_text(state.base_prompt, 220)}")
    if state.change_requests:
        lines.append(f"Recent change request: {truncate_text(state.change_requests[-1], 220)}")
    return "\n".join(lines)


def build_semantic_parser_context_summary(state: SessionState) -> str:
    rebuild_state_inventory(state)
    active_target = get_active_target_artifact(state)
    active_directory = get_active_directory_artifact(state)
    lines = [
        f"Workspace root: {state.workspace_root}",
        f"Chat has project: {'yes' if state.has_project() else 'no'}",
    ]
    if active_target:
        lines.append(
            "Active target: "
            f"{relative_display_path(active_target.path, state.workspace_root)} "
            f"| entity={active_target.entity_type} kind={active_target.artifact_kind} role={active_target.role}"
        )
    else:
        lines.append("Active target: none")
    if active_directory and active_directory.path:
        lines.append(f"Active directory: {relative_display_path(active_directory.path, state.workspace_root)}")
    lines.append("Known artifacts:")
    for artifact in state.artifacts[:MAX_CONTEXT_FILE_SUMMARIES]:
        if artifact.entity_type == "virtual":
            continue
        lines.append(
            f"- {relative_display_path(artifact.path, state.workspace_root)} "
            f"| entity={artifact.entity_type} kind={artifact.artifact_kind} role={artifact.role}"
        )
    return "\n".join(lines)


def build_semantics_fallback(prompt: str, state: SessionState) -> ParsedRequestSemantics:
    explicit_path = extract_explicit_path(prompt, state.workspace_root)
    explicit_filename = extract_explicit_filename(prompt)
    explicit_extension = extract_explicit_extension(prompt, explicit_filename, explicit_path)
    fallback_intent = classify_request_intent(prompt)
    fallback_artifact = classify_artifact_type(prompt)
    directory_hint = any(marker in prompt.lower() for marker in ("папк", "директор", "каталог", "folder", "directory"))
    requested_artifact_kind = normalize_requested_artifact_kind(fallback_artifact, explicit_filename, explicit_extension)
    if directory_hint and not explicit_extension and fallback_intent == "create":
        requested_artifact_kind = "directory"
    requested_entity_type = "directory" if requested_artifact_kind == "directory" else "file"
    if explicit_path and not explicit_extension and not looks_like_file_path(explicit_path):
        target_directory = explicit_path
    else:
        target_directory = extract_requested_output_directory(prompt) or (
            os.path.dirname(explicit_path) if explicit_path and looks_like_file_path(explicit_path) else ""
        )
    expects_existing = expects_existing_target_for_route(prompt, fallback_intent, fallback_artifact)
    if requested_entity_type == "directory" and fallback_intent == "create":
        expects_existing = False
    return ParsedRequestSemantics(
        intent=fallback_intent,
        requested_entity_type=requested_entity_type,
        requested_artifact_kind=requested_artifact_kind,
        explicit_path=explicit_path,
        explicit_filename=explicit_filename,
        explicit_extension=explicit_extension,
        target_directory=target_directory,
        follow_active_context=bool(state.active_target_id),
        expects_existing_target=expects_existing,
        create_if_missing=fallback_intent == "create",
        reason="fallback heuristic",
    )


def parse_request_semantics(
    args: argparse.Namespace,
    state: SessionState,
    prompt: str,
) -> ParsedRequestSemantics:
    fallback = build_semantics_fallback(prompt, state)
    normalized_prompt = prompt.strip()
    if not normalized_prompt:
        return fallback

    payload = {
        "model": args.model,
        "temperature": 0.0,
        "messages": [
            {"role": "system", "content": REQUEST_SEMANTICS_SYSTEM_PROMPT},
            {
                "role": "user",
                "content": (
                    f"Chat context:\n{build_semantic_parser_context_summary(state)}\n\n"
                    f"User request:\n{normalized_prompt}"
                ),
            },
        ],
    }

    try:
        raw_response = request_chat_completion(args.url, payload, args.request_timeout)
        semantics_data = parse_first_json_object(raw_response)
    except RuntimeError:
        semantics_data = None

    if not semantics_data:
        return fallback

    intent = str(semantics_data.get("intent", fallback.intent)).strip().lower()
    if intent not in {"create", "change", "inspect"}:
        intent = fallback.intent

    requested_artifact_kind = normalize_requested_artifact_kind(
        str(semantics_data.get("requested_artifact_kind", fallback.requested_artifact_kind)),
        fallback.explicit_filename,
        fallback.explicit_extension,
    )
    requested_entity_type = normalize_requested_entity_type(
        str(semantics_data.get("requested_entity_type", fallback.requested_entity_type)),
        requested_artifact_kind,
    )

    follow_active_context = semantics_data.get("follow_active_context")
    if not isinstance(follow_active_context, bool):
        follow_active_context = fallback.follow_active_context

    expects_existing_target = semantics_data.get("expects_existing_target")
    if not isinstance(expects_existing_target, bool):
        expects_existing_target = fallback.expects_existing_target

    create_if_missing = semantics_data.get("create_if_missing")
    if not isinstance(create_if_missing, bool):
        create_if_missing = intent == "create"

    target_directory = str(semantics_data.get("target_directory", "") or "").strip()
    if target_directory:
        target_directory = resolve_prompt_path(target_directory, state.workspace_root)
    if not target_directory:
        target_directory = fallback.target_directory

    reason = str(semantics_data.get("reason", "") or "").strip() or "semantic parser"
    if requested_entity_type == "directory":
        requested_artifact_kind = "directory"
    if requested_entity_type == "unknown" and requested_artifact_kind in {"lua", "readme", "json", "txt", "markdown", "generic_file"}:
        requested_entity_type = "file"

    if (
        requested_entity_type == "directory"
        and fallback.requested_entity_type == "file"
        and fallback.requested_artifact_kind in {"lua", "readme", "json", "txt", "markdown", "generic_file"}
        and fallback.target_directory
    ):
        requested_entity_type = fallback.requested_entity_type
        requested_artifact_kind = fallback.requested_artifact_kind
        target_directory = fallback.target_directory
        reason = f"{reason}; corrected to file deliverable inside requested directory scope"

    explicit_path = fallback.explicit_path
    if (
        requested_entity_type == "file"
        and explicit_path
        and target_directory
        and paths_equal(target_directory, explicit_path)
        and not fallback.explicit_extension
    ):
        explicit_path = ""

    return ParsedRequestSemantics(
        intent=intent,
        requested_entity_type=requested_entity_type,
        requested_artifact_kind=requested_artifact_kind,
        explicit_path=explicit_path,
        explicit_filename=fallback.explicit_filename,
        explicit_extension=fallback.explicit_extension,
        target_directory=target_directory,
        follow_active_context=follow_active_context,
        expects_existing_target=expects_existing_target,
        create_if_missing=create_if_missing,
        reason=reason,
    )


def classify_request_route(
    args: argparse.Namespace,
    state: SessionState,
    prompt: str,
) -> RequestRoute:
    semantics = parse_request_semantics(args, state, prompt)
    artifact_type = semantics.requested_artifact_kind
    if artifact_type not in {"lua", "readme"}:
        artifact_type = "readme" if artifact_type in {"markdown", "json", "txt", "generic_file"} else "lua"
    preferred_filename = "README.md" if semantics.requested_artifact_kind == "readme" else semantics.explicit_filename
    return RequestRoute(
        intent=semantics.intent,
        artifact_type=artifact_type,
        expects_existing_target=semantics.expects_existing_target,
        preferred_filename=preferred_filename,
        reason=semantics.reason,
    )


def resolve_output_paths(args: argparse.Namespace, prompt: str) -> tuple[str, str]:
    if getattr(args, "output_explicit", False):
        output_path = os.path.abspath(args.output)
        return output_path, os.path.abspath(build_working_path(output_path))

    task_slug = build_task_slug(prompt)
    requested_directory = extract_requested_output_directory(prompt)
    if requested_directory:
        output_directory = os.path.join(requested_directory, task_slug)
        output_path = os.path.join(output_directory, f"{task_slug}.lua")
    else:
        output_path = os.path.abspath(f"{task_slug}.lua")

    return output_path, os.path.abspath(build_working_path(output_path))


def build_context_path(workspace_root: str) -> str:
    return os.path.join(os.path.abspath(workspace_root), CONTEXT_FILE_NAME)


def new_chat_id() -> str:
    return datetime.now(timezone.utc).strftime("chat-%Y%m%d-%H%M%S")


def truncate_text(text: str, max_length: int) -> str:
    cleaned = " ".join(text.split())
    if len(cleaned) <= max_length:
        return cleaned
    if max_length <= 3:
        return cleaned[:max_length]
    return f"{cleaned[: max_length - 3].rstrip()}..."


def relative_display_path(path: str, workspace_root: str) -> str:
    absolute_path = os.path.abspath(path)
    try:
        relative_path = os.path.relpath(absolute_path, workspace_root)
    except ValueError:
        return absolute_path

    if relative_path.startswith(".."):
        return absolute_path
    return relative_path


def path_is_within(path: str, parent: str) -> bool:
    if not path or not parent:
        return False
    try:
        relative_path = os.path.relpath(os.path.abspath(path), os.path.abspath(parent))
    except ValueError:
        return False
    return relative_path == "." or not relative_path.startswith("..")


def read_text_if_exists(path: str) -> str:
    if not path or not os.path.exists(path) or not os.path.isfile(path):
        return ""

    with open(path, "r", encoding="utf-8", errors="replace") as file:
        return file.read().replace("\r\n", "\n")


def load_preferred_code(output_path: str, working_path: str, fallback_code: str = "") -> str:
    if working_path and os.path.exists(working_path):
        code = read_text_if_exists(working_path).strip()
        if code:
            return code

    if output_path and os.path.exists(output_path):
        code = read_text_if_exists(output_path).strip()
        if code:
            return code

    return fallback_code.strip()


def path_has_lua_artifact(path: str) -> bool:
    if not path:
        return False
    absolute_path = os.path.abspath(path)
    return os.path.exists(absolute_path)


def normalize_file_list(paths: list[str]) -> list[str]:
    normalized: list[str] = []
    seen: set[str] = set()
    for path in paths:
        if not path:
            continue
        absolute_path = os.path.abspath(path)
        key = absolute_path.lower()
        if key in seen:
            continue
        seen.add(key)
        normalized.append(absolute_path)
    return normalized


def serialize_report(report: SessionReport | None) -> dict | None:
    if report is None:
        return None

    return {
        "success": report.success,
        "action": report.action,
        "attempts": report.attempts,
        "output_path": report.output_path,
        "working_path": report.working_path,
        "saved_output": report.saved_output,
        "diagnostics": report.diagnostics,
        "verification": report.verification,
        "message": report.message,
    }


def deserialize_report(data: dict | None) -> SessionReport | None:
    if not isinstance(data, dict):
        return None

    return SessionReport(
        success=bool(data.get("success", False)),
        action=str(data.get("action", "")),
        attempts=int(data.get("attempts", 0)),
        output_path=os.path.abspath(data.get("output_path", "")) if data.get("output_path") else "",
        working_path=os.path.abspath(data.get("working_path", "")) if data.get("working_path") else "",
        saved_output=bool(data.get("saved_output", False)),
        diagnostics=data.get("diagnostics", empty_diagnostics()),
        verification=data.get("verification"),
        message=str(data.get("message", "")),
    )


def artifact_id_from_path(entity_type: str, path: str, artifact_kind: str = "") -> str:
    normalized_path = os.path.abspath(path).lower() if path else ""
    if entity_type == "virtual":
        return f"virtual:{artifact_kind or 'node'}:{normalized_path or 'workspace'}"
    return f"{entity_type}:{normalized_path}"


def infer_artifact_kind(path: str, entity_type: str = "file") -> str:
    if entity_type == "virtual":
        return "project"
    if entity_type == "directory":
        return "directory"

    file_name = os.path.basename(path).lower()
    extension = os.path.splitext(file_name)[1].lower()
    if extension == ".lua":
        return "lua"
    if file_name == "readme.md":
        return "readme"
    if extension == ".md":
        return "markdown"
    if extension == ".json":
        return "json"
    if extension == ".txt":
        return "txt"
    if extension:
        return "generic_file"
    return "unknown"


def infer_artifact_role(workspace_root: str, path: str, entity_type: str, artifact_kind: str) -> str:
    if entity_type == "virtual":
        return "generic"
    if entity_type == "directory":
        if path and paths_equal(path, workspace_root):
            return "workspace_root"
        return "generic"

    file_name = os.path.basename(path).lower()
    if artifact_kind == "readme":
        return "documentation"
    if artifact_kind == "lua":
        if file_name in {"main.lua", "init.lua", "app.lua"}:
            return "entrypoint"
        if "config" in file_name:
            return "config"
        return "entrypoint" if path and os.path.dirname(path) == workspace_root else "generic"
    if artifact_kind == "json":
        return "config"
    if artifact_kind == "txt":
        return "notes" if file_name in {"notes.txt", "todo.txt"} else "generic"
    return "generic"


def summarize_text_content(content: str) -> str:
    if not content.strip():
        return "empty file"
    for line in content.splitlines():
        stripped = line.strip()
        if stripped:
            return truncate_text(stripped, MAX_CONTEXT_FILE_SUMMARY_LENGTH)
    return "non-empty file"


def summarize_artifact_path(path: str, artifact_kind: str, entity_type: str) -> str:
    if entity_type == "virtual":
        return "logical project node"
    if entity_type == "directory":
        if not path:
            return "directory"
        if os.path.isdir(path):
            try:
                child_count = len(os.listdir(path))
            except OSError:
                child_count = 0
            return f"directory with {child_count} item(s)"
        return "directory"
    if not path or not os.path.exists(path):
        return "missing file"
    if artifact_kind == "lua":
        return summarize_lua_file(path)
    return summarize_text_content(read_text_if_exists(path))


def artifact_kind_display_name(artifact_kind: str, path: str = "") -> str:
    if artifact_kind == "lua":
        return "Lua file"
    if artifact_kind == "readme":
        return "README file"
    if artifact_kind == "markdown":
        return "Markdown file"
    if artifact_kind == "json":
        return "JSON file"
    if artifact_kind == "txt":
        return "text file"
    if artifact_kind == "directory":
        return "directory"
    if path:
        extension = os.path.splitext(path)[1].lower()
        if extension:
            return f"{extension} file"
    return "file"


def default_extension_for_artifact_kind(artifact_kind: str) -> str:
    if artifact_kind == "lua":
        return ".lua"
    if artifact_kind in {"readme", "markdown"}:
        return ".md"
    if artifact_kind == "json":
        return ".json"
    if artifact_kind == "txt":
        return ".txt"
    return ".txt"


def default_filename_for_artifact_kind(artifact_kind: str, prompt: str, explicit_extension: str = "") -> str:
    if artifact_kind == "readme":
        return "README.md"
    if artifact_kind == "json":
        return "config.json"
    if artifact_kind == "txt":
        return "notes.txt"
    extension = explicit_extension or default_extension_for_artifact_kind(artifact_kind)
    slug = build_task_slug(prompt) or "document"
    return f"{slug}{extension}"


def build_artifact_ref(
    workspace_root: str,
    path: str,
    entity_type: str = "file",
    artifact_kind: str | None = None,
    role: str | None = None,
    summary: str | None = None,
    pinned: bool = False,
    exists: bool | None = None,
) -> ArtifactRef:
    normalized_path = os.path.abspath(path) if path else ""
    effective_kind = artifact_kind or infer_artifact_kind(normalized_path, entity_type)
    effective_role = role or infer_artifact_role(workspace_root, normalized_path, entity_type, effective_kind)
    effective_exists = (os.path.exists(normalized_path) if normalized_path else False) if exists is None else bool(exists)
    extension = "" if entity_type != "file" else os.path.splitext(normalized_path)[1].lower()
    effective_summary = summary if summary is not None else summarize_artifact_path(normalized_path, effective_kind, entity_type)
    return ArtifactRef(
        id=artifact_id_from_path(entity_type, normalized_path, effective_kind),
        entity_type=entity_type,
        artifact_kind=effective_kind,
        path=normalized_path,
        exists=effective_exists,
        role=effective_role,
        extension=extension,
        summary=effective_summary,
        pinned=bool(pinned),
    )


def build_virtual_project_artifact(workspace_root: str) -> ArtifactRef:
    normalized_root = os.path.abspath(workspace_root)
    return ArtifactRef(
        id=artifact_id_from_path("virtual", normalized_root, "project"),
        entity_type="virtual",
        artifact_kind="project",
        path=normalized_root,
        exists=os.path.isdir(normalized_root),
        role="workspace_root",
        extension="",
        summary="project workspace",
        pinned=True,
    )


def serialize_artifact_ref(artifact: ArtifactRef) -> dict:
    return {
        "id": artifact.id,
        "entity_type": artifact.entity_type,
        "artifact_kind": artifact.artifact_kind,
        "path": artifact.path,
        "exists": artifact.exists,
        "role": artifact.role,
        "extension": artifact.extension,
        "summary": artifact.summary,
        "pinned": artifact.pinned,
    }


def deserialize_artifact_ref(data: dict, workspace_root: str) -> ArtifactRef | None:
    if not isinstance(data, dict):
        return None
    entity_type = str(data.get("entity_type", "file") or "file")
    path = os.path.abspath(str(data.get("path", ""))) if data.get("path") else ""
    artifact_kind = str(data.get("artifact_kind", "") or infer_artifact_kind(path, entity_type))
    role = str(data.get("role", "") or infer_artifact_role(workspace_root, path, entity_type, artifact_kind))
    extension = str(data.get("extension", "") or (os.path.splitext(path)[1].lower() if entity_type == "file" else ""))
    artifact_id = str(data.get("id", "") or artifact_id_from_path(entity_type, path, artifact_kind))
    return ArtifactRef(
        id=artifact_id,
        entity_type=entity_type,
        artifact_kind=artifact_kind,
        path=path,
        exists=bool(data.get("exists", os.path.exists(path) if path else False)),
        role=role,
        extension=extension,
        summary=str(data.get("summary", "")),
        pinned=bool(data.get("pinned", False)),
    )


def serialize_resolution_result(resolution: ResolutionResult | None) -> dict | None:
    if resolution is None:
        return None
    return {
        "target_id": resolution.target_id,
        "target_path": resolution.target_path,
        "intent": resolution.intent,
        "confidence": resolution.confidence,
        "source": resolution.source,
        "reasons": list(resolution.reasons),
        "requested_artifact_kind": resolution.requested_artifact_kind,
    }


def deserialize_resolution_result(data: dict | None) -> ResolutionResult | None:
    if not isinstance(data, dict):
        return None
    try:
        confidence = float(data.get("confidence", 0.0))
    except (TypeError, ValueError):
        confidence = 0.0
    return ResolutionResult(
        target_id=str(data.get("target_id", "")),
        target_path=os.path.abspath(str(data.get("target_path", ""))) if data.get("target_path") else "",
        intent=str(data.get("intent", "create") or "create"),
        confidence=confidence,
        source=str(data.get("source", "fallback") or "fallback"),
        reasons=[str(item) for item in data.get("reasons", []) if str(item).strip()],
        requested_artifact_kind=str(data.get("requested_artifact_kind", "unknown") or "unknown"),
    )


def get_artifact_by_id(state: SessionState, artifact_id: str) -> ArtifactRef | None:
    if not artifact_id:
        return None
    for artifact in state.artifacts:
        if artifact.id == artifact_id:
            return artifact
    return None


def get_artifact_by_path(state: SessionState, path: str, entity_type: str | None = None) -> ArtifactRef | None:
    if not path:
        return None
    normalized_path = os.path.abspath(path)
    for artifact in state.artifacts:
        if entity_type and artifact.entity_type != entity_type:
            continue
        if artifact.path and paths_equal(artifact.path, normalized_path):
            return artifact
    return None


def upsert_artifact(state: SessionState, artifact: ArtifactRef) -> ArtifactRef:
    artifacts: list[ArtifactRef] = []
    inserted = False
    for existing in state.artifacts:
        if existing.id == artifact.id:
            artifacts.append(artifact)
            inserted = True
        else:
            artifacts.append(existing)
    if not inserted:
        artifacts.append(artifact)
    state.artifacts = artifacts
    return artifact


def list_workspace_candidate_paths(workspace_root: str) -> list[str]:
    if not os.path.isdir(workspace_root):
        return []

    discovered: list[str] = []
    for root, dirs, files in os.walk(workspace_root):
        dirs[:] = [
            directory
            for directory in dirs
            if directory not in SCAN_SKIP_DIRS and not directory.startswith(".")
        ]
        for filename in files:
            lower_name = filename.lower()
            extension = os.path.splitext(lower_name)[1]
            if lower_name.endswith(".working.lua"):
                continue
            absolute_path = os.path.abspath(os.path.join(root, filename))
            if (
                extension not in TEXT_ARTIFACT_EXTENSIONS
                and os.path.splitext(lower_name)[0] not in GENERIC_TEXT_FILE_NAMES
                and not is_probably_text_file(absolute_path)
            ):
                continue
            discovered.append(absolute_path)
            if len(discovered) >= MAX_DISCOVERED_LUA_FILES:
                return sorted(discovered)
    return sorted(discovered)


def merge_inventory_artifacts(
    workspace_root: str,
    existing_artifacts: list[ArtifactRef],
    candidate_paths: list[str],
) -> list[ArtifactRef]:
    existing_by_id = {artifact.id: artifact for artifact in existing_artifacts}
    merged: list[ArtifactRef] = [build_virtual_project_artifact(workspace_root)]
    seen_ids = {merged[0].id}

    directory_paths: set[str] = {os.path.abspath(workspace_root)}
    for path in candidate_paths:
        if path:
            absolute_path = os.path.abspath(path)
            if os.path.isdir(absolute_path):
                directory_paths.add(absolute_path)
            else:
                directory_paths.add(os.path.dirname(absolute_path) or os.path.abspath(workspace_root))

    for directory_path in sorted(directory_paths):
        artifact = build_artifact_ref(
            workspace_root,
            directory_path,
            entity_type="directory",
            pinned=bool(existing_by_id.get(artifact_id_from_path("directory", directory_path)).pinned) if artifact_id_from_path("directory", directory_path) in existing_by_id else paths_equal(directory_path, workspace_root),
        )
        if artifact.id not in seen_ids:
            merged.append(artifact)
            seen_ids.add(artifact.id)

    for path in candidate_paths:
        absolute_path = os.path.abspath(path)
        if os.path.isdir(absolute_path):
            continue
        artifact = build_artifact_ref(workspace_root, absolute_path, entity_type="file")
        previous = existing_by_id.get(artifact.id)
        if previous:
            artifact.summary = previous.summary or artifact.summary
            artifact.pinned = previous.pinned
        if artifact.id not in seen_ids:
            merged.append(artifact)
            seen_ids.add(artifact.id)

    for artifact in existing_artifacts:
        if artifact.id in seen_ids:
            continue
        if artifact.entity_type == "virtual":
            continue
        if artifact.path and artifact.path.startswith(os.path.abspath(workspace_root)):
            missing_artifact = build_artifact_ref(
                workspace_root,
                artifact.path,
                entity_type=artifact.entity_type,
                artifact_kind=artifact.artifact_kind,
                role=artifact.role,
                summary=artifact.summary or summarize_artifact_path(artifact.path, artifact.artifact_kind, artifact.entity_type),
                pinned=artifact.pinned,
                exists=os.path.exists(artifact.path),
            )
            merged.append(missing_artifact)
            seen_ids.add(missing_artifact.id)

    return merged


def build_workspace_inventory(
    workspace_root: str,
    existing_artifacts: list[ArtifactRef] | None = None,
    extra_paths: list[str] | None = None,
) -> WorkspaceInventory:
    existing = existing_artifacts or []
    candidate_paths = list_workspace_candidate_paths(workspace_root)
    if extra_paths:
        candidate_paths.extend(os.path.abspath(path) for path in extra_paths if path)
    candidate_paths = normalize_file_list(candidate_paths)
    artifacts = merge_inventory_artifacts(workspace_root, existing, candidate_paths)
    return WorkspaceInventory(
        workspace_root=os.path.abspath(workspace_root),
        artifacts=artifacts,
        scanned_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
    )


def select_default_active_directory_id(state: SessionState) -> str:
    active_target = get_artifact_by_id(state, state.active_target_id)
    if active_target and active_target.path:
        directory_path = active_target.path if active_target.entity_type == "directory" else os.path.dirname(active_target.path)
        directory_artifact = get_artifact_by_path(state, directory_path, "directory")
        if directory_artifact:
            return directory_artifact.id
    workspace_directory = get_artifact_by_path(state, state.workspace_root, "directory")
    return workspace_directory.id if workspace_directory else ""


def sync_state_compatibility(state: SessionState) -> None:
    if state.current_code.strip() and not state.current_content.strip():
        state.current_content = state.current_code

    active_target = get_artifact_by_id(state, state.active_target_id)
    if not active_target and state.current_target_path:
        active_target = get_artifact_by_path(state, state.current_target_path)
    if not active_target and state.output_path:
        active_target = get_artifact_by_path(state, state.output_path)

    if not active_target and state.output_path:
        active_target = upsert_artifact(
            state,
            build_artifact_ref(
                state.workspace_root,
                state.output_path,
                entity_type="file",
                pinned=True,
                summary="active target",
                exists=os.path.exists(state.output_path),
            ),
        )

    if active_target:
        state.active_target_id = active_target.id
        state.current_target_path = active_target.path
        state.output_path = active_target.path
        state.artifact_type = active_target.artifact_kind
        state.working_path = (
            os.path.abspath(build_working_path(active_target.path))
            if active_target.path and active_target.entity_type == "file"
            else ""
        )
        if active_target.entity_type != "file":
            state.current_content = ""
            state.current_code = ""
        elif not state.current_content and active_target.path and active_target.exists:
            state.current_content = load_preferred_code(active_target.path, build_working_path(active_target.path), state.current_code or "")
    else:
        state.active_target_id = ""
        state.current_target_path = ""

    if not state.active_directory_id:
        state.active_directory_id = select_default_active_directory_id(state)

    if state.workspace_root:
        workspace_directory = get_artifact_by_path(state, state.workspace_root, "directory")
        if workspace_directory:
            if not state.active_directory_id:
                state.active_directory_id = workspace_directory.id

    if state.current_content.strip() or not state.current_code.strip():
        state.current_code = state.current_content


def rebuild_state_inventory(state: SessionState, extra_paths: list[str] | None = None) -> None:
    inventory = build_workspace_inventory(state.workspace_root, state.artifacts, extra_paths)
    state.artifacts = inventory.artifacts
    sync_state_compatibility(state)


def get_active_target_artifact(state: SessionState) -> ArtifactRef | None:
    sync_state_compatibility(state)
    return get_artifact_by_id(state, state.active_target_id)


def get_active_directory_artifact(state: SessionState) -> ArtifactRef | None:
    sync_state_compatibility(state)
    return get_artifact_by_id(state, state.active_directory_id)


def migrate_resolution_from_legacy(
    target_path: str,
    intent: str,
    artifact_kind: str,
    source: str = "fallback",
) -> ResolutionResult | None:
    if not target_path:
        return None
    return ResolutionResult(
        target_id=artifact_id_from_path("file", target_path, artifact_kind),
        target_path=os.path.abspath(target_path),
        intent=intent or "create",
        confidence=0.5,
        source=source,
        reasons=["Migrated from legacy path-based state."],
        requested_artifact_kind=artifact_kind or "unknown",
    )


def load_context_data(context_path: str) -> dict:
    if not os.path.exists(context_path):
        return {}

    try:
        with open(context_path, "r", encoding="utf-8") as file:
            data = json.load(file)
    except (OSError, json.JSONDecodeError):
        return {}

    return data if isinstance(data, dict) else {}


def resolve_workspace_root(args: argparse.Namespace, prompt: str = "") -> str:
    cwd = os.path.abspath(os.getcwd())
    explicit_path = extract_explicit_path(prompt, cwd)
    if explicit_path:
        if looks_like_file_path(explicit_path):
            return os.path.dirname(explicit_path) or cwd
        return explicit_path

    requested_directory = extract_requested_output_directory(prompt)
    if requested_directory:
        return requested_directory

    if getattr(args, "output_explicit", False):
        return os.path.dirname(os.path.abspath(args.output)) or cwd

    return cwd


def load_session_state(
    workspace_root: str,
    fallback_output_path: str,
    fallback_working_path: str,
) -> SessionState:
    workspace_root = os.path.abspath(workspace_root)
    context_path = build_context_path(workspace_root)
    data = load_context_data(context_path)
    restored_managed_files = normalize_file_list([str(item) for item in data.get("managed_files", []) if str(item).strip()])
    serialized_artifacts = [
        artifact
        for artifact in (
            deserialize_artifact_ref(item, workspace_root)
            for item in data.get("artifacts", [])
        )
        if artifact is not None
    ]
    legacy_paths = restored_managed_files + [
        os.path.abspath(str(data.get("output_path", fallback_output_path))) if (data.get("output_path") or fallback_output_path) else "",
        os.path.abspath(str(data.get("current_target_path", ""))) if data.get("current_target_path") else "",
        os.path.abspath(fallback_output_path) if fallback_output_path else "",
    ]

    state = SessionState(
        workspace_root=workspace_root,
        context_path=context_path,
        output_path=os.path.abspath(fallback_output_path),
        working_path=os.path.abspath(fallback_working_path),
        chat_id=str(data.get("chat_id", "")),
        base_prompt=str(data.get("base_prompt", "")),
        change_requests=[str(item) for item in data.get("change_requests", []) if str(item).strip()],
        artifacts=serialized_artifacts,
        active_target_id=str(data.get("active_target_id", "")),
        active_directory_id=str(data.get("active_directory_id", "")),
        current_content=str(data.get("current_content", data.get("current_code", ""))),
        current_code=str(data.get("current_code", data.get("current_content", ""))),
        last_report=deserialize_report(data.get("last_report")),
        last_resolution=deserialize_resolution_result(data.get("last_resolution")),
        managed_files=restored_managed_files,
        current_target_path=os.path.abspath(str(data.get("current_target_path", ""))) if data.get("current_target_path") else "",
        artifact_type=str(data.get("artifact_type", "lua") or "lua"),
        last_route_intent=str(data.get("last_route_intent", "create") or "create"),
    )

    if not state.chat_id:
        state.chat_id = new_chat_id()

    rebuild_state_inventory(state, legacy_paths)

    if not state.active_target_id:
        legacy_target_path = state.current_target_path or str(data.get("output_path", "") or "")
        if legacy_target_path:
            legacy_target = get_artifact_by_path(state, legacy_target_path) or get_artifact_by_path(state, legacy_target_path, "file")
            if legacy_target:
                state.active_target_id = legacy_target.id
    if not state.active_directory_id:
        state.active_directory_id = select_default_active_directory_id(state)
    if state.last_resolution is None:
        legacy_resolution_path = state.current_target_path or state.output_path
        state.last_resolution = migrate_resolution_from_legacy(
            legacy_resolution_path,
            state.last_route_intent,
            state.artifact_type,
            source="fallback",
        )

    sync_state_compatibility(state)

    if state.output_path:
        cleanup_legacy_working_file(state.output_path)
    for path in state.managed_files:
        cleanup_legacy_working_file(path)

    preferred_code = load_preferred_code(
        state.current_target_path or state.output_path,
        build_working_path(state.current_target_path or state.output_path),
        state.current_content or state.current_code,
    )
    if preferred_code:
        state.current_content = preferred_code
        state.current_code = preferred_code

    return state


def save_session_state(state: SessionState) -> None:
    os.makedirs(state.workspace_root, exist_ok=True)
    sync_state_compatibility(state)
    payload = {
        "version": 2,
        "chat_id": state.chat_id or new_chat_id(),
        "workspace_root": state.workspace_root,
        "output_path": state.output_path,
        "working_path": state.working_path,
        "current_target_path": state.current_target_path or state.output_path,
        "active_target_id": state.active_target_id,
        "active_directory_id": state.active_directory_id,
        "base_prompt": state.base_prompt,
        "change_requests": state.change_requests,
        "artifacts": [serialize_artifact_ref(artifact) for artifact in state.artifacts],
        "current_content": state.current_content,
        "current_code": state.current_code,
        "managed_files": normalize_file_list(state.managed_files),
        "last_report": serialize_report(state.last_report),
        "last_resolution": serialize_resolution_result(state.last_resolution),
        "artifact_type": state.artifact_type,
        "last_route_intent": state.last_route_intent,
        "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }

    with open(state.context_path, "w", encoding="utf-8", newline="\n") as file:
        json.dump(payload, file, ensure_ascii=False, indent=2)
        file.write("\n")


def switch_workspace(
    state: SessionState,
    workspace_root: str,
    fallback_output_path: str,
    fallback_working_path: str,
) -> None:
    loaded_state = load_session_state(workspace_root, fallback_output_path, fallback_working_path)
    state.workspace_root = loaded_state.workspace_root
    state.context_path = loaded_state.context_path
    state.output_path = loaded_state.output_path
    state.working_path = loaded_state.working_path
    state.chat_id = loaded_state.chat_id
    state.base_prompt = loaded_state.base_prompt
    state.change_requests = loaded_state.change_requests
    state.artifacts = loaded_state.artifacts
    state.active_target_id = loaded_state.active_target_id
    state.active_directory_id = loaded_state.active_directory_id
    state.current_content = loaded_state.current_content
    state.current_code = loaded_state.current_code
    state.last_report = loaded_state.last_report
    state.last_resolution = loaded_state.last_resolution
    state.managed_files = loaded_state.managed_files
    state.current_target_path = loaded_state.current_target_path
    state.artifact_type = loaded_state.artifact_type
    state.last_route_intent = loaded_state.last_route_intent
    sync_state_compatibility(state)


def sync_current_code_from_active_target(state: SessionState) -> None:
    sync_state_compatibility(state)
    target_path = state.current_target_path or state.output_path
    if not target_path:
        state.current_content = ""
        state.current_code = ""
        return
    working_path = build_working_path(target_path)
    loaded_content = load_preferred_code(target_path, working_path, state.current_content or state.current_code)
    state.current_content = loaded_content
    state.current_code = loaded_content


def register_managed_file(state: SessionState, path: str) -> None:
    normalized = normalize_file_list(state.managed_files + [path])
    state.managed_files = normalized
    if path:
        artifact = build_artifact_ref(state.workspace_root, path, entity_type="file", pinned=True)
        upsert_artifact(state, artifact)
        rebuild_state_inventory(state, [path])


def discover_workspace_lua_files(workspace_root: str) -> list[str]:
    if not os.path.isdir(workspace_root):
        return []

    discovered: list[str] = []
    for root, dirs, files in os.walk(workspace_root):
        dirs[:] = [
            directory
            for directory in dirs
            if directory not in SCAN_SKIP_DIRS and not directory.startswith(".")
        ]
        for filename in files:
            if not filename.lower().endswith(".lua"):
                continue
            if filename.lower().endswith(".working.lua"):
                continue
            discovered.append(os.path.abspath(os.path.join(root, filename)))
            if len(discovered) >= MAX_DISCOVERED_LUA_FILES:
                return sorted(discovered)
    return sorted(discovered)


def discover_workspace_readme_files(workspace_root: str) -> list[str]:
    if not os.path.isdir(workspace_root):
        return []

    discovered: list[str] = []
    for root, dirs, files in os.walk(workspace_root):
        dirs[:] = [
            directory
            for directory in dirs
            if directory not in SCAN_SKIP_DIRS and not directory.startswith(".")
        ]
        for filename in files:
            if filename.lower() != "readme.md":
                continue
            discovered.append(os.path.abspath(os.path.join(root, filename)))
            if len(discovered) >= MAX_DISCOVERED_LUA_FILES:
                return sorted(discovered)
    return sorted(discovered)


def discover_workspace_context_files(workspace_root: str) -> list[str]:
    if not os.path.isdir(workspace_root):
        return []

    discovered: list[str] = []
    for root, dirs, files in os.walk(workspace_root):
        dirs[:] = [
            directory
            for directory in dirs
            if directory not in SCAN_SKIP_DIRS and not directory.startswith(".")
        ]
        for filename in files:
            if filename != CONTEXT_FILE_NAME:
                continue
            discovered.append(os.path.abspath(os.path.join(root, filename)))
            if len(discovered) >= MAX_DISCOVERED_LUA_FILES:
                return sorted(discovered)
    return sorted(discovered)


def select_target_from_context_file(context_path: str, state: SessionState) -> TargetSelection | None:
    data = load_context_data(context_path)
    if not data:
        return None

    output_path = os.path.abspath(str(data.get("current_target_path") or data.get("output_path") or ""))
    if not output_path:
        return None

    current_code = str(data.get("current_code", "")).strip()
    restored_managed_files = normalize_file_list([str(item) for item in data.get("managed_files", [])])
    if not current_code and not any(path_has_lua_artifact(path) for path in restored_managed_files + [output_path]):
        return None

    selection = build_target_selection_for_path(
        state,
        output_path,
        explicit=True,
        source="fallback",
        requested_artifact_kind=infer_artifact_kind(output_path, "file"),
        confidence=0.66,
        reasons=["Target restored from saved workspace context."],
        restored_from_context=bool(current_code) and not os.path.exists(output_path),
    )
    if current_code:
        selection.current_code = current_code
        selection.exists = True
    return selection


def summarize_lua_file(path: str) -> str:
    content = read_text_if_exists(path)
    if not content.strip():
        return "empty file"

    function_names: list[str] = []
    for pattern in (
        r"(?:local\s+)?function\s+([A-Za-z0-9_:.]+)",
        r"([A-Za-z0-9_:.]+)\s*=\s*function\b",
    ):
        for match in re.finditer(pattern, content):
            name = match.group(1)
            if name not in function_names:
                function_names.append(name)
            if len(function_names) >= 5:
                break
        if len(function_names) >= 5:
            break

    if function_names:
        return truncate_text(f"functions: {', '.join(function_names)}", MAX_CONTEXT_FILE_SUMMARY_LENGTH)

    for line in content.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        return truncate_text(stripped, MAX_CONTEXT_FILE_SUMMARY_LENGTH)

    return "non-empty file"


def paths_equal(left: str, right: str) -> bool:
    if not left or not right:
        return False
    return os.path.abspath(left).lower() == os.path.abspath(right).lower()


def get_target_code(state: SessionState, output_path: str, working_path: str) -> str:
    fallback_code = state.current_code if paths_equal(state.current_target_path or state.output_path, output_path) else ""
    return load_preferred_code(output_path, working_path, fallback_code)


def resolve_directory_artifact_id(state: SessionState, path: str) -> str:
    if not path:
        return ""
    directory_path = path if os.path.isdir(path) else os.path.dirname(os.path.abspath(path))
    if not directory_path:
        directory_path = state.workspace_root
    directory_artifact = get_artifact_by_path(state, directory_path, "directory")
    if not directory_artifact:
        directory_artifact = upsert_artifact(
            state,
            build_artifact_ref(state.workspace_root, directory_path, entity_type="directory", pinned=paths_equal(directory_path, state.workspace_root)),
        )
    return directory_artifact.id


def build_target_selection_for_path(
    state: SessionState,
    output_path: str,
    explicit: bool,
    source: str,
    requested_artifact_kind: str,
    confidence: float,
    reasons: list[str],
    restored_from_context: bool = False,
    entity_type: str = "file",
) -> TargetSelection:
    normalized_output = os.path.abspath(output_path)
    working_path = os.path.abspath(build_working_path(normalized_output)) if entity_type == "file" else ""
    artifact = get_artifact_by_path(state, normalized_output, entity_type) or upsert_artifact(
        state,
        build_artifact_ref(
            state.workspace_root,
            normalized_output,
            entity_type=entity_type,
            pinned=explicit or source in {"managed", "carryover", "explicit"},
            exists=os.path.exists(normalized_output),
        ),
    )
    return TargetSelection(
        output_path=normalized_output,
        working_path=working_path,
        current_code=get_target_code(state, normalized_output, working_path) if entity_type == "file" else "",
        explicit=explicit,
        exists=os.path.isdir(normalized_output) if entity_type == "directory" else (os.path.exists(normalized_output) or os.path.exists(working_path)),
        source=source,
        entity_type=entity_type,
        artifact_kind=artifact.artifact_kind,
        restored_from_context=restored_from_context,
        artifact_id=artifact.id,
        directory_id=resolve_directory_artifact_id(state, normalized_output),
        confidence=confidence,
        reasons=reasons,
        requested_artifact_kind=requested_artifact_kind,
    )


def build_directory_selection_for_path(
    state: SessionState,
    directory_path: str,
    explicit: bool,
    source: str,
    confidence: float,
    reasons: list[str],
) -> TargetSelection:
    return build_target_selection_for_path(
        state,
        directory_path,
        explicit=explicit,
        source=source,
        requested_artifact_kind="directory",
        confidence=confidence,
        reasons=reasons,
        entity_type="directory",
    )


def collect_inventory_candidates(
    state: SessionState,
    semantics: ParsedRequestSemantics,
) -> list[ArtifactRef]:
    candidates: list[ArtifactRef] = []
    for artifact in state.artifacts:
        if artifact.entity_type == "virtual":
            continue
        if semantics.requested_entity_type == "directory" and artifact.entity_type != "directory":
            continue
        if semantics.requested_entity_type == "file" and artifact.entity_type != "file":
            continue
        candidates.append(artifact)
    return candidates


def score_target_candidate(
    state: SessionState,
    semantics: ParsedRequestSemantics,
    artifact: ArtifactRef,
) -> tuple[int, list[str], str]:
    score = 0
    reasons: list[str] = []
    source = "semantic_resolver"
    active_target = get_active_target_artifact(state)
    active_directory = get_active_directory_artifact(state)
    target_directory = semantics.target_directory
    expected_entity_type = normalize_requested_entity_type(semantics.requested_entity_type, semantics.requested_artifact_kind)
    requested_kind = semantics.requested_artifact_kind
    file_name = os.path.basename(artifact.path).lower() if artifact.path else ""

    if expected_entity_type != "unknown":
        if artifact.entity_type == expected_entity_type:
            score += 24
            reasons.append(f"entity type matches requested {expected_entity_type}")
        else:
            score -= 40
            reasons.append("entity type does not match the request")

    if requested_kind != "unknown":
        if artifact.artifact_kind == requested_kind:
            score += 26
            reasons.append(f"artifact kind matches {requested_kind}")
        elif requested_kind == "markdown" and artifact.artifact_kind == "readme":
            score += 20
            reasons.append("README satisfies the requested Markdown artifact")
        elif requested_kind == "generic_file" and artifact.entity_type == "file":
            score += 10
            reasons.append("generic file request allows this text file candidate")
        elif requested_kind == "directory" and artifact.entity_type == "directory":
            score += 16
        else:
            score -= 14

    if semantics.explicit_path:
        normalized_explicit = os.path.abspath(semantics.explicit_path)
        if artifact.path and paths_equal(artifact.path, normalized_explicit):
            score += 90
            source = "explicit"
            reasons.append("exact explicit path match")
        elif artifact.path and path_is_within(artifact.path, normalized_explicit):
            score += 28
            source = "explicit"
            reasons.append("candidate is inside the explicitly referenced path")

    if semantics.explicit_filename and file_name == semantics.explicit_filename.lower():
        score += 42
        source = "explicit"
        reasons.append("filename matches the explicit filename from the request")

    if semantics.explicit_extension:
        if artifact.extension == semantics.explicit_extension:
            score += 12
            reasons.append("extension matches the explicit extension")
        elif artifact.entity_type == "file":
            score -= 5

    if target_directory and artifact.path and path_is_within(artifact.path, target_directory):
        score += 22
        reasons.append("artifact is inside the requested directory scope")

    if active_target and artifact.id == active_target.id:
        score += 34 if semantics.follow_active_context else 20
        source = "carryover" if source != "explicit" else source
        reasons.append("matches the active target from the current chat")

    if active_directory and active_directory.path and artifact.path and path_is_within(artifact.path, active_directory.path):
        score += 10
        reasons.append("artifact is inside the active directory")

    if artifact.role == "documentation" and requested_kind in {"readme", "markdown"}:
        score += 8
        reasons.append("role matches documentation")
    elif artifact.role == "config" and requested_kind in {"json", "generic_file"}:
        score += 8
        reasons.append("role matches configuration")
    elif artifact.role == "entrypoint" and requested_kind == "lua":
        score += 8
        reasons.append("role matches Lua entrypoint")

    if artifact.pinned:
        score += 4
        reasons.append("artifact is pinned in workspace state")
    if artifact.path and artifact.path.lower() in {path.lower() for path in state.managed_files}:
        score += 6
        reasons.append("artifact was previously managed in this chat")
    if semantics.expects_existing_target:
        if artifact.exists:
            score += 6
        else:
            score -= 24

    return score, reasons[:6], source


def choose_best_inventory_candidate(
    state: SessionState,
    semantics: ParsedRequestSemantics,
) -> tuple[ArtifactRef | None, float, list[str], str]:
    best_artifact: ArtifactRef | None = None
    best_score = -10_000
    best_reasons: list[str] = []
    best_source = "fallback"
    for artifact in collect_inventory_candidates(state, semantics):
        score, reasons, source = score_target_candidate(state, semantics, artifact)
        if score > best_score:
            best_artifact = artifact
            best_score = score
            best_reasons = reasons
            best_source = source
    confidence = max(0.0, min(0.99, best_score / 100.0))
    return best_artifact, confidence, best_reasons, best_source


def derive_creation_directory(
    state: SessionState,
    semantics: ParsedRequestSemantics,
) -> str:
    if semantics.explicit_path and semantics.requested_entity_type == "directory":
        return os.path.abspath(os.path.dirname(semantics.explicit_path) or state.workspace_root)
    if semantics.target_directory:
        return os.path.abspath(semantics.target_directory)
    if semantics.explicit_path and looks_like_file_path(semantics.explicit_path):
        return os.path.abspath(os.path.dirname(semantics.explicit_path) or state.workspace_root)
    active_directory = get_active_directory_artifact(state)
    if active_directory and active_directory.path:
        return os.path.abspath(active_directory.path)
    return os.path.abspath(state.workspace_root)


def derive_creation_path(
    state: SessionState,
    prompt: str,
    semantics: ParsedRequestSemantics,
) -> str:
    if semantics.explicit_path:
        return os.path.abspath(semantics.explicit_path)
    base_directory = derive_creation_directory(state, semantics)
    if semantics.requested_entity_type == "directory":
        directory_name = semantics.explicit_filename or build_task_slug(prompt) or "workspace_item"
        return os.path.abspath(os.path.join(base_directory, directory_name))
    filename = semantics.explicit_filename or default_filename_for_artifact_kind(
        semantics.requested_artifact_kind,
        prompt,
        semantics.explicit_extension,
    )
    return os.path.abspath(os.path.join(base_directory, filename))


def resolve_target(
    args: argparse.Namespace,
    state: SessionState,
    prompt: str,
    semantics: ParsedRequestSemantics,
) -> TargetSelection:
    extra_paths: list[str] = []
    if semantics.explicit_path:
        extra_paths.append(semantics.explicit_path)
        if looks_like_file_path(semantics.explicit_path):
            extra_paths.append(os.path.dirname(semantics.explicit_path))
    if semantics.target_directory:
        extra_paths.append(semantics.target_directory)
    rebuild_state_inventory(state, extra_paths)

    if semantics.explicit_path:
        normalized_explicit = os.path.abspath(semantics.explicit_path)
        explicit_entity_type = (
            "directory"
            if semantics.requested_entity_type == "directory" or (os.path.exists(normalized_explicit) and os.path.isdir(normalized_explicit))
            else "file"
        )
        explicit_artifact = get_artifact_by_path(state, normalized_explicit, explicit_entity_type)
        if explicit_artifact:
            if explicit_entity_type == "directory":
                return build_directory_selection_for_path(
                    state,
                    explicit_artifact.path,
                    explicit=True,
                    source="explicit",
                    confidence=0.99,
                    reasons=["Using the exact directory path explicitly referenced by the user."],
                )
            return build_target_selection_for_path(
                state,
                explicit_artifact.path,
                explicit=True,
                source="explicit",
                requested_artifact_kind=semantics.requested_artifact_kind or explicit_artifact.artifact_kind,
                confidence=0.99,
                reasons=["Using the exact file path explicitly referenced by the user."],
            )
        if semantics.intent == "create" or semantics.create_if_missing:
            if explicit_entity_type == "directory":
                return build_directory_selection_for_path(
                    state,
                    normalized_explicit,
                    explicit=True,
                    source="explicit",
                    confidence=0.95,
                    reasons=["Creating the directory at the explicit path requested by the user."],
                )
            return build_target_selection_for_path(
                state,
                normalized_explicit,
                explicit=True,
                source="explicit",
                requested_artifact_kind=normalize_requested_artifact_kind(
                    semantics.requested_artifact_kind,
                    os.path.basename(normalized_explicit),
                    os.path.splitext(normalized_explicit)[1],
                ),
                confidence=0.95,
                    reasons=["Creating the file at the explicit path requested by the user."],
                )

    if semantics.intent == "create" and semantics.explicit_filename:
        explicit_create_path = derive_creation_path(state, prompt, semantics)
        if not os.path.exists(explicit_create_path):
            return build_target_selection_for_path(
                state,
                explicit_create_path,
                explicit=True,
                source="explicit",
                requested_artifact_kind=normalize_requested_artifact_kind(
                    semantics.requested_artifact_kind,
                    os.path.basename(explicit_create_path),
                    os.path.splitext(explicit_create_path)[1],
                ),
                confidence=0.94,
                reasons=["Creating the explicitly named file requested by the user."],
            )

    best_artifact, confidence, reasons, source = choose_best_inventory_candidate(state, semantics)
    minimum_score_ok = confidence >= 0.45 or source in {"explicit", "carryover"}
    if best_artifact and minimum_score_ok:
        if best_artifact.entity_type == "directory":
            return build_directory_selection_for_path(
                state,
                best_artifact.path,
                explicit=source == "explicit",
                source=source,
                confidence=confidence,
                reasons=reasons or ["Resolved the directory from workspace inventory and active context."],
            )
        return build_target_selection_for_path(
            state,
            best_artifact.path,
            explicit=source == "explicit",
            source=source,
            requested_artifact_kind=semantics.requested_artifact_kind or best_artifact.artifact_kind,
            confidence=confidence,
            reasons=reasons or ["Resolved the file from workspace inventory and active context."],
        )

    creation_path = derive_creation_path(state, prompt, semantics)
    if semantics.requested_entity_type == "directory":
        return build_directory_selection_for_path(
            state,
            creation_path,
            explicit=bool(semantics.explicit_path),
            source="fallback",
            confidence=0.42,
            reasons=["No grounded directory candidate was found, so a conservative directory create path was chosen."],
        )
    requested_kind = normalize_requested_artifact_kind(
        semantics.requested_artifact_kind,
        os.path.basename(creation_path),
        os.path.splitext(creation_path)[1],
    )
    return build_target_selection_for_path(
        state,
        creation_path,
        explicit=bool(semantics.explicit_path or semantics.explicit_filename),
        source="fallback",
        requested_artifact_kind=requested_kind,
        confidence=0.42,
        reasons=["No grounded file candidate was found, so a conservative create target was chosen."],
    )


def collect_known_lua_files(state: SessionState, extra_paths: list[str] | None = None) -> list[str]:
    combined = discover_workspace_lua_files(state.workspace_root)
    combined.extend(
        path
        for path in state.managed_files
        if os.path.exists(path) and path.lower().endswith(".lua")
    )
    if extra_paths:
        combined.extend(path for path in extra_paths if path and path.lower().endswith(".lua"))
    return normalize_file_list(combined)


def select_existing_lua_from_directory(directory: str, state: SessionState) -> str | None:
    if not directory or not os.path.isdir(directory):
        return None

    normalized_directory = os.path.abspath(directory)
    rebuild_state_inventory(state, [normalized_directory])
    candidate_files = [
        artifact.path
        for artifact in state.artifacts
        if artifact.entity_type == "file"
        and artifact.artifact_kind == "lua"
        and artifact.path
        and path_is_within(artifact.path, normalized_directory)
    ]
    if not candidate_files:
        return None

    managed_set = {path.lower() for path in normalize_file_list(state.managed_files)}
    current_target = state.current_target_path
    current_output = state.output_path
    candidate_files.sort(
        key=lambda path: (
            0 if current_target and paths_equal(path, current_target) else 1,
            0 if current_output and paths_equal(path, current_output) else 1,
            0 if path.lower() in managed_set else 1,
            os.path.relpath(path, directory).count(os.sep),
            len(os.path.basename(path)),
            path.lower(),
        )
    )
    return candidate_files[0]


def select_existing_readme_from_directory(directory: str, state: SessionState) -> str | None:
    if not directory or not os.path.isdir(directory):
        return None

    normalized_directory = os.path.abspath(directory)
    rebuild_state_inventory(state, [normalized_directory])
    candidate_files = [
        artifact.path
        for artifact in state.artifacts
        if artifact.entity_type == "file"
        and artifact.artifact_kind == "readme"
        and artifact.path
        and path_is_within(artifact.path, normalized_directory)
    ]
    if not candidate_files:
        return None

    current_target = state.current_target_path
    current_output = state.output_path
    candidate_files.sort(
        key=lambda path: (
            0 if current_target and paths_equal(path, current_target) else 1,
            0 if current_output and paths_equal(path, current_output) else 1,
            os.path.relpath(path, directory).count(os.sep),
            len(os.path.basename(path)),
            path.lower(),
        )
    )
    return candidate_files[0]


def select_existing_target_from_directory(directory: str, state: SessionState) -> TargetSelection | None:
    file_target = select_existing_lua_from_directory(directory, state)
    if file_target:
        return build_target_selection_for_path(
            state,
            file_target,
            explicit=True,
            source="explicit",
            requested_artifact_kind="lua",
            confidence=0.92,
            reasons=["User referenced a directory and a Lua target was found inside it."],
        )

    context_candidates: list[tuple[int, int, str, TargetSelection]] = []
    for context_path in discover_workspace_context_files(directory):
        selection = select_target_from_context_file(context_path, state)
        if not selection:
            continue
        output_path = selection.output_path
        try:
            depth = os.path.relpath(os.path.dirname(output_path), directory).count(os.sep)
        except ValueError:
            depth = 999
        context_candidates.append((
            0 if os.path.dirname(context_path).lower() == os.path.abspath(directory).lower() else 1,
            depth,
            output_path.lower(),
            selection,
        ))

    if not context_candidates:
        return None

    context_candidates.sort(key=lambda item: item[:3])
    return context_candidates[0][3]


def extract_directory_lua_target(prompt: str, state: SessionState) -> str | None:
    requested_directory = extract_requested_output_directory(prompt)
    if not requested_directory:
        return None
    selection = select_existing_target_from_directory(requested_directory, state)
    return selection.output_path if selection else None


def extract_explicit_lua_target(prompt: str, state: SessionState) -> str | None:
    candidates: list[str] = []
    path_candidate = extract_explicit_lua_path_candidate(prompt)
    if path_candidate:
        candidates.append(path_candidate)

    for match in LUA_FILE_NAME_PATTERN.finditer(prompt):
        candidates.append(clean_path_candidate(match.group(1)))

    if not candidates:
        return None

    known_files = collect_known_lua_files(state, [state.output_path] if state.output_path else [])
    managed_set = {path.lower() for path in state.managed_files}
    current_output = state.output_path.lower() if state.output_path else ""
    seen: set[str] = set()

    for candidate in candidates:
        if not candidate or candidate.lower().endswith(".working.lua"):
            continue

        key = candidate.lower()
        if key in seen:
            continue
        seen.add(key)

        if os.path.isabs(candidate) or any(separator in candidate for separator in ("\\", "/")) or candidate.startswith("."):
            return resolve_prompt_path(candidate, state.workspace_root)

        matches = [path for path in known_files if os.path.basename(path).lower() == key]
        if matches:
            matches.sort(
                key=lambda path: (
                    0 if path.lower() in managed_set else 1,
                    0 if path.lower() == current_output else 1,
                    len(path),
                )
            )
            return matches[0]

        return os.path.abspath(os.path.join(state.workspace_root, candidate))

    return None


def extract_explicit_markdown_path_candidate(prompt: str) -> str | None:
    for match in MARKDOWN_PATH_CANDIDATE_PATTERN.finditer(prompt):
        candidate = next((group for group in match.groups() if group), "")
        cleaned = clean_path_candidate(candidate)
        if cleaned:
            return cleaned
    return None


def extract_explicit_markdown_target(prompt: str, state: SessionState | None = None) -> str | None:
    candidates: list[str] = []
    path_candidate = extract_explicit_markdown_path_candidate(prompt)
    if path_candidate:
        candidates.append(path_candidate)

    for match in MARKDOWN_FILE_NAME_PATTERN.finditer(prompt):
        candidates.append(clean_path_candidate(match.group(1)))

    normalized = " ".join(prompt.lower().split())
    if "readme" in normalized and not candidates:
        candidates.append("README.md")

    workspace_root = state.workspace_root if state is not None else os.path.abspath(os.getcwd())
    seen: set[str] = set()
    known_files = discover_workspace_readme_files(workspace_root) if state is not None else []

    for candidate in candidates:
        if not candidate:
            continue
        key = candidate.lower()
        if key in seen:
            continue
        seen.add(key)

        if os.path.isabs(candidate) or any(separator in candidate for separator in ("\\", "/")) or candidate.startswith("."):
            return resolve_prompt_path(candidate, workspace_root)

        for path in known_files:
            if os.path.basename(path).lower() == key:
                return path

        return os.path.abspath(os.path.join(workspace_root, candidate))

    return None


def resolve_readme_output_paths(
    args: argparse.Namespace,
    state: SessionState,
    prompt: str,
    route: RequestRoute,
) -> tuple[str, str]:
    if getattr(args, "output_explicit", False):
        output_path = os.path.abspath(args.output)
        if not looks_like_file_path(output_path):
            output_path = os.path.join(output_path, route.preferred_filename or "README.md")
        return output_path, os.path.abspath(build_working_path(output_path))

    explicit_target = extract_explicit_markdown_target(prompt, state)
    if explicit_target:
        output_path = os.path.abspath(explicit_target)
        return output_path, os.path.abspath(build_working_path(output_path))

    requested_directory = extract_requested_output_directory(prompt) or resolve_workspace_root(args, prompt)
    output_path = os.path.abspath(os.path.join(requested_directory, route.preferred_filename or "README.md"))
    return output_path, os.path.abspath(build_working_path(output_path))


def resolve_route_output_paths(
    args: argparse.Namespace,
    state: SessionState,
    prompt: str,
    route: RequestRoute,
) -> tuple[str, str]:
    if route.artifact_type == "readme":
        return resolve_readme_output_paths(args, state, prompt, route)
    return resolve_output_paths(args, prompt)


def select_readme_target_file(
    args: argparse.Namespace,
    state: SessionState,
    prompt: str,
    is_new_project: bool,
    route: RequestRoute,
) -> TargetSelection:
    explicit_target = extract_explicit_markdown_target(prompt, state)
    if explicit_target:
        return build_target_selection_for_path(
            state,
            explicit_target,
            explicit=True,
            source="explicit",
            requested_artifact_kind="readme",
            confidence=0.98,
            reasons=["User explicitly named a Markdown target."],
        )

    if route.intent in {"inspect", "change"}:
        requested_directory = extract_requested_output_directory(prompt)
        if requested_directory:
            existing_readme = select_existing_readme_from_directory(requested_directory, state)
            if existing_readme:
                return build_target_selection_for_path(
                    state,
                    existing_readme,
                    explicit=True,
                    source="explicit",
                    requested_artifact_kind="readme",
                    confidence=0.9,
                    reasons=["User referenced a directory and README.md was found inside it."],
                )

    preferred_output = ""
    if not is_new_project and (state.current_target_path or state.output_path):
        candidate = state.current_target_path or state.output_path
        if candidate.lower().endswith(".md"):
            preferred_output = candidate

    if not preferred_output:
        preferred_output, _ = resolve_readme_output_paths(args, state, prompt, route)

    return build_target_selection_for_path(
        state,
        preferred_output,
        explicit=False,
        source="carryover" if state.active_target_id else "fallback",
        requested_artifact_kind="readme",
        confidence=0.75 if state.active_target_id else 0.55,
        reasons=["Using the active or default README target for this workspace."],
    )


def select_target_file(
    args: argparse.Namespace,
    state: SessionState,
    prompt: str,
    is_new_project: bool,
    route: RequestRoute,
) -> TargetSelection:
    if route.artifact_type == "readme":
        return select_readme_target_file(args, state, prompt, is_new_project, route)

    explicit_target = extract_explicit_lua_target(prompt, state)
    if explicit_target:
        return build_target_selection_for_path(
            state,
            explicit_target,
            explicit=True,
            source="explicit",
            requested_artifact_kind="lua",
            confidence=0.98,
            reasons=["User explicitly named a Lua file target."],
        )

    if route.intent in {"inspect", "change"}:
        requested_directory = extract_requested_output_directory(prompt)
        if requested_directory:
            directory_selection = select_existing_target_from_directory(requested_directory, state)
            if directory_selection:
                return directory_selection

    if is_new_project:
        output_path, working_path = resolve_route_output_paths(args, state, prompt, route)
        selection = build_target_selection_for_path(
            state,
            output_path,
            explicit=False,
            source="semantic_resolver",
            requested_artifact_kind=route.artifact_type,
            confidence=0.76,
            reasons=["Created a new target based on the semantic resolver and workspace scope."],
        )
        selection.working_path = working_path
        return selection

    active_target = get_artifact_by_id(state, state.active_target_id)
    preferred_output = active_target.path if active_target and active_target.entity_type == "file" else state.output_path
    if not preferred_output:
        existing_managed = [
            path for path in normalize_file_list(state.managed_files)
            if (os.path.exists(path) or os.path.exists(build_working_path(path)))
            and infer_artifact_kind(path, "file") == route.artifact_type
        ]
        if existing_managed:
            preferred_output = existing_managed[-1]

    if not preferred_output:
        preferred_output, _ = resolve_route_output_paths(args, state, prompt, route)

    return build_target_selection_for_path(
        state,
        preferred_output,
        explicit=False,
        source="carryover" if active_target else ("fallback" if not state.managed_files else "semantic_resolver"),
        requested_artifact_kind=route.artifact_type,
        confidence=0.8 if active_target else 0.58,
        reasons=[
            "Reused the active target from the current chat."
            if active_target
            else "Fell back to the best known workspace target."
        ],
    )


def activate_target_selection(state: SessionState, selection: TargetSelection) -> None:
    extra_paths = [selection.output_path]
    if selection.output_path:
        extra_paths.append(os.path.dirname(selection.output_path))
    rebuild_state_inventory(state, extra_paths)
    target_artifact = get_artifact_by_path(state, selection.output_path, selection.entity_type or None) or upsert_artifact(
        state,
        build_artifact_ref(
            state.workspace_root,
            selection.output_path,
            entity_type=selection.entity_type,
            pinned=True,
            exists=selection.exists,
        ),
    )
    target_artifact.pinned = True
    upsert_artifact(state, target_artifact)
    state.active_target_id = target_artifact.id
    state.active_directory_id = (
        target_artifact.id if target_artifact.entity_type == "directory" else (selection.directory_id or resolve_directory_artifact_id(state, selection.output_path))
    )
    state.current_content = selection.current_code if target_artifact.entity_type == "file" else ""
    state.current_code = selection.current_code if target_artifact.entity_type == "file" else ""
    state.last_resolution = ResolutionResult(
        target_id=target_artifact.id,
        target_path=selection.output_path,
        intent=state.last_route_intent or "create",
        confidence=selection.confidence,
        source=selection.source,
        reasons=list(selection.reasons),
        requested_artifact_kind=selection.requested_artifact_kind or selection.artifact_kind,
    )
    sync_state_compatibility(state)
    cleanup_legacy_working_file(selection.output_path)


def build_model_change_summary(state: SessionState, latest_request: str = "") -> str:
    lines: list[str] = []
    if state.base_prompt.strip():
        lines.append("Base chat request:")
        lines.append(state.base_prompt.strip())

    change_items = state.change_requests
    if latest_request and change_items and change_items[-1].strip() == latest_request.strip():
        change_items = change_items[:-1]

    if not change_items:
        return "\n".join(lines).strip()

    recent_items = change_items[-MAX_CONTEXT_CHANGE_ITEMS:]
    skipped = len(change_items) - len(recent_items)
    if skipped > 0:
        lines.append(
            f"Older applied change requests omitted from memory: {skipped}. "
            "Assume the current code already reflects them."
        )

    lines.append("Recent applied change requests:")
    start_index = len(change_items) - len(recent_items) + 1
    for index, item in enumerate(recent_items, start=start_index):
        lines.append(f"{index}. {truncate_text(item, MAX_CONTEXT_CHANGE_LENGTH)}")

    return "\n".join(lines).strip()


def build_file_context_for_model(
    state: SessionState,
    target_path: str,
    explicit_target: bool,
) -> str:
    known_files = collect_known_lua_files(state, [target_path])
    if not known_files:
        return ""

    managed_set = {path.lower() for path in state.managed_files}
    lines = [
        "Lua workspace context:",
        f"Primary target file: {relative_display_path(target_path, state.workspace_root)}",
    ]

    if explicit_target:
        lines.append(
            "The user explicitly named this Lua file. Edit only this file unless the user later names another one."
        )
    elif state.managed_files:
        lines.append(
            "No explicit Lua file was named. Use the program-managed Lua file as the primary target. "
            "Other Lua files are reference-only unless needed."
        )
    else:
        lines.append(
            "No program-managed Lua file exists yet. Create or update the primary target file from the request."
        )

    summary_paths = known_files[:MAX_CONTEXT_FILE_SUMMARIES]
    if summary_paths:
        lines.append("Known Lua files:")
    for path in summary_paths:
        labels: list[str] = []
        if paths_equal(path, target_path):
            labels.append("target")
        elif path.lower() in managed_set:
            labels.append("managed")
        else:
            labels.append("workspace")
        labels_text = ", ".join(labels)
        lines.append(
            f"- [{labels_text}] {relative_display_path(path, state.workspace_root)} | {summarize_lua_file(path)}"
        )

    omitted = len(known_files) - len(summary_paths)
    if omitted > 0:
        lines.append(f"- ... {omitted} more Lua files omitted from the summary.")

    return "\n".join(lines)


def build_generation_prompt(
    state: SessionState,
    user_request: str,
    target_path: str,
    explicit_target: bool,
) -> str:
    sections = [f"User request:\n{user_request.strip()}"]
    chat_memory = build_model_change_summary(state)
    if chat_memory:
        sections.append(chat_memory)
    file_context = build_file_context_for_model(state, target_path, explicit_target)
    if file_context:
        sections.append(file_context)
    sections.append(
        "Generate only the Lua code for the primary target file. Return only Lua code."
    )
    return "\n\n".join(section for section in sections if section.strip())


def build_retry_prompt(state: SessionState, target_path: str, explicit_target: bool) -> str:
    sections: list[str] = []
    chat_memory = build_model_change_summary(state)
    if chat_memory:
        sections.append(chat_memory)
    file_context = build_file_context_for_model(state, target_path, explicit_target)
    if file_context:
        sections.append(file_context)
    sections.append(
        "Re-validate and fix only the primary target Lua file so it still satisfies the active chat requirements. "
        "Return only Lua code."
    )
    return "\n\n".join(section for section in sections if section.strip())


def build_verification_prompt(state: SessionState, target_path: str, explicit_target: bool) -> str:
    prompt = state.effective_prompt().strip()
    if not prompt:
        prompt = "Update the primary Lua file according to the active chat requirements."

    target_description = f"Primary target Lua file: {relative_display_path(target_path, state.workspace_root)}"
    if explicit_target:
        target_description += "\nThe user explicitly requested work in this file."

    return f"{prompt}\n\n{target_description}".strip()


def build_generic_workspace_context(state: SessionState, target_path: str, explicit_target: bool) -> str:
    rebuild_state_inventory(state, [target_path, os.path.dirname(target_path)])
    target_artifact = get_artifact_by_path(state, target_path)
    lines = [
        f"Workspace root: {state.workspace_root}",
        f"Target file: {relative_display_path(target_path, state.workspace_root)}",
    ]
    if target_artifact:
        lines.append(
            f"Target metadata: kind={target_artifact.artifact_kind} role={target_artifact.role} "
            f"exists={'yes' if target_artifact.exists else 'no'}"
        )
    if explicit_target:
        lines.append("The user explicitly named this target.")
    active_target = get_active_target_artifact(state)
    if active_target and active_target.path and not paths_equal(active_target.path, target_path):
        lines.append(
            f"Current active target: {relative_display_path(active_target.path, state.workspace_root)} "
            f"| kind={active_target.artifact_kind}"
        )
    related_artifacts = [
        artifact
        for artifact in state.artifacts
        if artifact.entity_type == "file" and artifact.path and not paths_equal(artifact.path, target_path)
    ][:MAX_CONTEXT_FILE_SUMMARIES]
    if related_artifacts:
        lines.append("Related workspace artifacts:")
        for artifact in related_artifacts:
            labels: list[str] = [artifact.artifact_kind]
            if artifact.path.lower() in {path.lower() for path in state.managed_files}:
                labels.append("managed")
            lines.append(
                f"- [{', '.join(labels)}] {relative_display_path(artifact.path, state.workspace_root)} | {artifact.summary}"
            )
    return "\n".join(lines)


def build_text_artifact_generation_prompt(
    state: SessionState,
    user_request: str,
    target_path: str,
    explicit_target: bool,
    artifact_kind: str,
) -> str:
    sections = [f"User request:\n{user_request.strip()}"]
    chat_memory = build_model_change_summary(state)
    if chat_memory:
        sections.append(chat_memory)
    sections.append(build_generic_workspace_context(state, target_path, explicit_target))
    sections.append(
        "Target file requirements:\n"
        f"- File path: {relative_display_path(target_path, state.workspace_root)}\n"
        f"- Artifact kind: {artifact_kind}\n"
        f"- Return only the full content for this single {artifact_kind_display_name(artifact_kind, target_path)}."
    )
    return "\n\n".join(section for section in sections if section.strip())


def build_text_artifact_verification_prompt(
    state: SessionState,
    target_path: str,
    explicit_target: bool,
    artifact_kind: str,
) -> str:
    prompt = state.effective_prompt().strip()
    if not prompt:
        prompt = f"Update the target {artifact_kind_display_name(artifact_kind, target_path)} according to the active chat requirements."
    target_description = (
        f"Target file: {relative_display_path(target_path, state.workspace_root)}\n"
        f"Artifact kind: {artifact_kind}"
    )
    if explicit_target:
        target_description += "\nThe user explicitly requested this file."
    return f"{prompt}\n\n{target_description}".strip()


def normalize_text_artifact_content(text: str) -> str:
    return normalize_document_text(text)


def request_generated_text_artifact(
    args: argparse.Namespace,
    prompt: str,
    strict_mode: bool = False,
    format_reason: str = "",
) -> str:
    system_prompt = (
        TEXT_ARTIFACT_GENERATE_SYSTEM_PROMPT
        if not strict_mode
        else build_strict_system_prompt(TEXT_ARTIFACT_GENERATE_SYSTEM_PROMPT)
    )
    user_prompt = prompt
    temperature = args.temperature if not strict_mode else min(args.temperature, 0.05)
    if strict_mode and format_reason:
        user_prompt = (
            f"{prompt}\n\n"
            "Previous model response format issue to avoid:\n"
            f"{format_reason}\n\n"
            "Return only the target file content."
        )
    payload = build_payload(
        model=args.model,
        user_prompt=user_prompt,
        system_prompt=system_prompt,
        temperature=temperature,
    )
    response_text = request_chat_completion(args.url, payload, args.request_timeout)
    text = normalize_text_artifact_content(response_text)
    if not text:
        raise RuntimeError("LM Studio returned empty file content.")
    return text


def request_edited_text_artifact(
    args: argparse.Namespace,
    overall_prompt: str,
    change_request: str,
    current_text: str,
    file_context: str,
) -> str:
    payload = {
        "model": args.model,
        "temperature": args.temperature,
        "messages": [
            {"role": "system", "content": TEXT_ARTIFACT_EDIT_SYSTEM_PROMPT},
            {
                "role": "user",
                "content": (
                    f"Current overall project requirements:\n{overall_prompt}\n\n"
                    f"{file_context}\n\n"
                    f"Latest user change request:\n{change_request}\n\n"
                    "Update only the target file. Return the full updated file content only."
                ),
            },
            {"role": "assistant", "content": current_text},
            {
                "role": "user",
                "content": "Apply the requested change above. Return only the complete updated file content.",
            },
        ],
    }
    response_text = request_chat_completion(args.url, payload, args.request_timeout)
    text = normalize_text_artifact_content(response_text)
    if not text:
        raise RuntimeError("LM Studio returned empty file content after the edit request.")
    return text


def request_text_artifact_explanation(
    args: argparse.Namespace,
    state: SessionState,
    user_request: str,
    target_path: str,
    current_text: str,
    explicit_target: bool,
    artifact_kind: str,
) -> str:
    file_context = build_generic_workspace_context(state, target_path, explicit_target)
    fence_tag = os.path.splitext(target_path)[1].lstrip(".") or "text"
    payload = {
        "model": args.model,
        "temperature": min(args.temperature, 0.2),
        "messages": [
            {"role": "system", "content": TEXT_ARTIFACT_EXPLAIN_SYSTEM_PROMPT},
            {
                "role": "user",
                "content": (
                    f"User request:\n{user_request.strip()}\n\n"
                    f"Target file: {relative_display_path(target_path, state.workspace_root)}\n\n"
                    f"{file_context}\n\n"
                    "File content:\n"
                    f"```{fence_tag}\n{current_text.rstrip()}\n```"
                ).strip(),
            },
        ],
    }
    explanation = request_chat_completion(args.url, payload, args.request_timeout).strip()
    if not explanation:
        raise RuntimeError("LM Studio returned an empty explanation.")
    return repair_mojibake(explanation)


def build_document_context_for_model(
    state: SessionState,
    target_path: str,
    explicit_target: bool,
) -> str:
    lines = [
        "Workspace project context for documentation:",
        f"Target documentation file: {relative_display_path(target_path, state.workspace_root)}",
    ]

    if explicit_target:
        lines.append("The user explicitly named this document file.")

    known_lua_files = collect_known_lua_files(state)
    if known_lua_files:
        lines.append("Relevant Lua files:")
        for path in known_lua_files[:MAX_CONTEXT_FILE_SUMMARIES]:
            lines.append(
                f"- {relative_display_path(path, state.workspace_root)} | {summarize_lua_file(path)}"
            )

    primary_lua_path = ""
    if state.current_target_path and state.current_target_path.lower().endswith(".lua"):
        primary_lua_path = state.current_target_path
    elif state.output_path and state.output_path.lower().endswith(".lua"):
        primary_lua_path = state.output_path
    elif state.managed_files:
        for path in reversed(state.managed_files):
            if path.lower().endswith(".lua") and os.path.exists(path):
                primary_lua_path = path
                break

    if primary_lua_path:
        code = read_text_if_exists(primary_lua_path)
        if not code.strip() and paths_equal(primary_lua_path, state.current_target_path or state.output_path):
            code = state.current_code
        snippet = code[:MAX_DOCUMENT_CODE_CONTEXT].strip()
        if snippet:
            if len(code) > MAX_DOCUMENT_CODE_CONTEXT:
                snippet = f"{snippet}\n-- truncated for context --"
            lines.append(
                "Primary Lua file excerpt:\n"
                f"```lua\n{snippet}\n```"
            )

    return "\n".join(line for line in lines if line.strip())


def build_document_generation_prompt(
    state: SessionState,
    user_request: str,
    target_path: str,
    explicit_target: bool,
) -> str:
    sections = [f"User request:\n{user_request.strip()}"]
    chat_memory = build_model_change_summary(state)
    if chat_memory:
        sections.append(chat_memory)
    document_context = build_document_context_for_model(state, target_path, explicit_target)
    if document_context:
        sections.append(document_context)
    sections.append(
        "Generate only the full Markdown content for the target documentation file. Return only Markdown."
    )
    return "\n\n".join(section for section in sections if section.strip())


def build_document_verification_prompt(state: SessionState, target_path: str, explicit_target: bool) -> str:
    prompt = state.effective_prompt().strip()
    if not prompt:
        prompt = "Update the target documentation file according to the active chat requirements."

    target_description = f"Target documentation file: {relative_display_path(target_path, state.workspace_root)}"
    if explicit_target:
        target_description += "\nThe user explicitly requested work in this file."
    return f"{prompt}\n\n{target_description}".strip()


def build_existing_file_base_prompt(
    state: SessionState,
    target_path: str,
    artifact_type: str = "lua",
) -> str:
    relative_path = relative_display_path(target_path, state.workspace_root)
    artifact_label = artifact_kind_display_name(artifact_type, target_path)
    return (
        f"Use the existing {artifact_label} '{relative_path}' as the primary target for this chat. "
        "Preserve its current purpose unless the user explicitly asks to change it."
    )


def build_info_diagnostics() -> dict:
    diagnostics = empty_diagnostics()
    diagnostics["program_mode"] = ""
    diagnostics["failure_kind"] = "none"
    return diagnostics


def request_code_explanation(
    args: argparse.Namespace,
    state: SessionState,
    user_request: str,
    target_path: str,
    current_code: str,
    explicit_target: bool,
) -> str:
    file_context = build_file_context_for_model(state, target_path, explicit_target)
    payload = {
        "model": args.model,
        "temperature": min(args.temperature, 0.2),
        "messages": [
            {"role": "system", "content": EXPLAIN_SYSTEM_PROMPT},
            {
                "role": "user",
                "content": (
                    f"User request:\n{user_request.strip()}\n\n"
                    f"Target Lua file: {relative_display_path(target_path, state.workspace_root)}\n\n"
                    f"{file_context}\n\n"
                    "Lua code:\n"
                    f"```lua\n{current_code.rstrip()}\n```"
                ).strip(),
            },
        ],
    }
    explanation = request_chat_completion(args.url, payload, args.request_timeout).strip()
    if not explanation:
        raise RuntimeError("LM Studio returned an empty explanation.")
    return repair_mojibake(explanation)


def request_document_explanation(
    args: argparse.Namespace,
    state: SessionState,
    user_request: str,
    target_path: str,
    current_text: str,
    explicit_target: bool,
) -> str:
    document_context = build_document_context_for_model(state, target_path, explicit_target)
    payload = {
        "model": args.model,
        "temperature": min(args.temperature, 0.2),
        "messages": [
            {"role": "system", "content": DOCUMENT_EXPLAIN_SYSTEM_PROMPT},
            {
                "role": "user",
                "content": (
                    f"User request:\n{user_request.strip()}\n\n"
                    f"Target document: {relative_display_path(target_path, state.workspace_root)}\n\n"
                    f"{document_context}\n\n"
                    "Document content:\n"
                    f"```md\n{current_text.rstrip()}\n```"
                ).strip(),
            },
        ],
    }
    explanation = request_chat_completion(args.url, payload, args.request_timeout).strip()
    if not explanation:
        raise RuntimeError("LM Studio returned an empty explanation.")
    return repair_mojibake(explanation)


def build_missing_existing_target_report(
    state: SessionState,
    action: str,
    output_path: str,
    message: str,
) -> SessionReport:
    diagnostics = build_info_diagnostics()
    diagnostics["run_error"] = message
    return finalize_report(
        state,
        state.current_code,
        SessionReport(
            success=False,
            action=action,
            attempts=0,
            output_path=output_path,
            working_path="",
            saved_output=False,
            diagnostics=diagnostics,
            message=message,
        ),
    )


def inspect_target_file(
    args: argparse.Namespace,
    state: SessionState,
    prompt: str,
    target: TargetSelection,
) -> SessionReport:
    try:
        explanation = request_code_explanation(
            args,
            state,
            prompt,
            target.output_path,
            target.current_code,
            target.explicit,
        )
    except RuntimeError as exc:
        diagnostics = build_info_diagnostics()
        diagnostics["run_error"] = repair_mojibake(str(exc))
        return finalize_report(
            state,
            target.current_code,
            SessionReport(
                success=False,
                action="inspect",
                attempts=1,
                output_path=target.output_path,
                working_path="",
                saved_output=False,
                diagnostics=diagnostics,
                message="Could not analyze the Lua file.",
            ),
        )

    diagnostics = build_info_diagnostics()
    return finalize_report(
        state,
        target.current_code,
        SessionReport(
            success=True,
            action="inspect",
            attempts=1,
            output_path=target.output_path,
            working_path="",
            saved_output=False,
            diagnostics=diagnostics,
            message=explanation,
        ),
    )


def inspect_document_file(
    args: argparse.Namespace,
    state: SessionState,
    prompt: str,
    target: TargetSelection,
) -> SessionReport:
    try:
        explanation = request_document_explanation(
            args,
            state,
            prompt,
            target.output_path,
            target.current_code,
            target.explicit,
        )
    except RuntimeError as exc:
        diagnostics = build_info_diagnostics()
        diagnostics["run_error"] = repair_mojibake(str(exc))
        return finalize_report(
            state,
            target.current_code,
            SessionReport(
                success=False,
                action="inspect",
                attempts=1,
                output_path=target.output_path,
                working_path="",
                saved_output=False,
                diagnostics=diagnostics,
                message="Could not analyze the document file.",
            ),
        )

    diagnostics = build_info_diagnostics()
    return finalize_report(
        state,
        target.current_code,
        SessionReport(
            success=True,
            action="inspect",
            attempts=1,
            output_path=target.output_path,
            working_path="",
            saved_output=False,
            diagnostics=diagnostics,
            message=explanation,
        ),
    )


def persist_state(state: SessionState) -> None:
    if not state.chat_id:
        state.chat_id = new_chat_id()
    save_session_state(state)


def finalize_report(
    state: SessionState,
    current_code: str,
    report: SessionReport,
) -> SessionReport:
    state.current_content = current_code
    state.current_code = current_code
    state.last_report = report
    if report.output_path:
        state.current_target_path = report.output_path
    cleanup_legacy_working_file(state.output_path)
    sync_state_compatibility(state)
    persist_state(state)
    return report


def empty_diagnostics() -> dict:
    return {
        "success": False,
        "started_ok": False,
        "timed_out": False,
        "failure_kind": "unknown",
        "program_mode": "batch",
        "run_output": "",
        "run_error": "",
        "run_warning": "",
        "luacheck_output": "",
        "luacheck_error": "",
        "luacheck_warning": "",
        "verification_checked": False,
        "verification_passed": False,
        "verification_score": 0,
        "verification_summary": "",
        "verification_missing_requirements": [],
        "verification_warnings": [],
    }


def attach_verification(diagnostics: dict, verification: dict) -> None:
    diagnostics["verification_checked"] = True
    diagnostics["verification_passed"] = verification["passed"]
    diagnostics["verification_score"] = verification["score"]
    diagnostics["verification_summary"] = verification["summary"]
    diagnostics["verification_missing_requirements"] = verification["missing_requirements"]
    diagnostics["verification_warnings"] = verification["warnings"]


def build_verification_fingerprint(verification: dict) -> str:
    parts = [
        verification.get("summary", "").strip().lower(),
        "|".join(item.strip().lower() for item in verification.get("missing_requirements", []) if item.strip()),
        "|".join(item.strip().lower() for item in verification.get("warnings", []) if item.strip()),
    ]
    return "\n".join(parts).strip()


def can_soft_accept_verification(diagnostics: dict, verification: dict) -> bool:
    if not diagnostics.get("success"):
        return False
    if diagnostics.get("run_error") or diagnostics.get("luacheck_error"):
        return False
    return int(verification.get("score", 0)) >= SOFT_VERIFICATION_PASS_SCORE


def should_stop_requirement_retry(
    verification: dict,
    requirement_attempts: int,
    previous_score: int,
    previous_fingerprint: str,
) -> bool:
    if requirement_attempts >= MAX_REQUIREMENT_FIX_ATTEMPTS:
        return True

    current_score = int(verification.get("score", 0))
    current_fingerprint = build_verification_fingerprint(verification)
    if previous_score < 0:
        return False

    improved_enough = current_score >= previous_score + MIN_REQUIREMENT_SCORE_IMPROVEMENT
    same_feedback = bool(current_fingerprint and current_fingerprint == previous_fingerprint)
    return same_feedback and not improved_enough


def request_generated_code(args: argparse.Namespace, prompt: str) -> str:
    return request_generated_code_strict(args, prompt, strict_mode=False)


def request_generated_code_strict(
    args: argparse.Namespace,
    prompt: str,
    strict_mode: bool,
    format_reason: str = "",
) -> str:
    system_prompt = GENERATE_SYSTEM_PROMPT if not strict_mode else build_strict_system_prompt(GENERATE_SYSTEM_PROMPT)
    user_prompt = prompt
    temperature = args.temperature if not strict_mode else min(args.temperature, 0.05)
    if strict_mode and format_reason:
        user_prompt = (
            f"{prompt}\n\n"
            "Previous model response format issue to avoid:\n"
            f"{format_reason}\n\n"
            "Return only the full Lua file."
        )

    payload = build_payload(
        model=args.model,
        user_prompt=user_prompt,
        system_prompt=system_prompt,
        temperature=temperature,
    )
    response_text = request_lua_code(args.url, payload, args.request_timeout)
    code = analyze_lua_response(response_text)["normalized"]
    if not code:
        raise RuntimeError("LM Studio returned empty Lua code.")
    return code


def build_edit_payload(
    model: str,
    temperature: float,
    overall_prompt: str,
    change_request: str,
    current_code: str,
    file_context: str,
) -> dict:
    return {
        "model": model,
        "temperature": temperature,
        "messages": [
            {"role": "system", "content": EDIT_SYSTEM_PROMPT},
            {
                "role": "user",
                "content": (
                    f"Current overall project requirements:\n{overall_prompt}\n\n"
                    f"{file_context}\n\n"
                    f"Latest user change request:\n{change_request}\n\n"
                    "Update only the primary target Lua file while keeping the requirements satisfied. "
                    "Use other Lua files only as reference when needed. "
                    "Return the full updated Lua code only."
                ),
            },
            {"role": "assistant", "content": current_code},
            {
                "role": "user",
                "content": (
                    "Apply the requested change to the program above. "
                    "Return only the complete updated Lua code. "
                    "Do not include explanations, labels, markdown, or any leading prose."
                ),
            },
        ],
    }


def request_edited_code(
    args: argparse.Namespace,
    overall_prompt: str,
    change_request: str,
    current_code: str,
    file_context: str,
) -> str:
    payload = build_edit_payload(
        model=args.model,
        temperature=args.temperature,
        overall_prompt=overall_prompt,
        change_request=change_request,
        current_code=current_code,
        file_context=file_context,
    )
    response_text = request_lua_code(args.url, payload, args.request_timeout)
    code = analyze_lua_response(response_text)["normalized"]
    if not code:
        raise RuntimeError("LM Studio returned empty Lua code after the edit request.")
    return code


def normalize_document_text(text: str) -> str:
    normalized = text.replace("\r\n", "\n").strip()
    if not normalized:
        return ""
    if normalized.startswith("```") and normalized.endswith("```"):
        first_newline = normalized.find("\n")
        if first_newline != -1:
            inner = normalized[first_newline + 1 : -3]
            normalized = inner.strip("\n")
    return normalized.strip()


def request_generated_document(
    args: argparse.Namespace,
    prompt: str,
    strict_mode: bool = False,
    format_reason: str = "",
) -> str:
    system_prompt = (
        DOCUMENT_GENERATE_SYSTEM_PROMPT
        if not strict_mode
        else build_strict_system_prompt(DOCUMENT_GENERATE_SYSTEM_PROMPT)
    )
    user_prompt = prompt
    temperature = args.temperature if not strict_mode else min(args.temperature, 0.05)
    if strict_mode and format_reason:
        user_prompt = (
            f"{prompt}\n\n"
            "Previous model response format issue to avoid:\n"
            f"{format_reason}\n\n"
            "Return only the full Markdown document."
        )

    payload = build_payload(
        model=args.model,
        user_prompt=user_prompt,
        system_prompt=system_prompt,
        temperature=temperature,
    )
    response_text = request_chat_completion(args.url, payload, args.request_timeout)
    document_text = normalize_document_text(response_text)
    if not document_text:
        raise RuntimeError("LM Studio returned empty document content.")
    return document_text


def request_edited_document(
    args: argparse.Namespace,
    overall_prompt: str,
    change_request: str,
    current_text: str,
    document_context: str,
) -> str:
    payload = {
        "model": args.model,
        "temperature": args.temperature,
        "messages": [
            {"role": "system", "content": DOCUMENT_EDIT_SYSTEM_PROMPT},
            {
                "role": "user",
                "content": (
                    f"Current overall project requirements:\n{overall_prompt}\n\n"
                    f"{document_context}\n\n"
                    f"Latest user change request:\n{change_request}\n\n"
                    "Update only the target Markdown document while keeping the requirements satisfied. "
                    "Return the full updated document only."
                ),
            },
            {"role": "assistant", "content": current_text},
            {
                "role": "user",
                "content": (
                    "Apply the requested change to the document above. "
                    "Return only the complete updated Markdown document. "
                    "Do not include explanations or markdown fences around the document."
                ),
            },
        ],
    }
    response_text = request_chat_completion(args.url, payload, args.request_timeout)
    document_text = normalize_document_text(response_text)
    if not document_text:
        raise RuntimeError("LM Studio returned empty document content after the edit request.")
    return document_text


def verify_document(
    args: argparse.Namespace,
    prompt: str,
    document_text: str,
) -> dict:
    verify_model = args.verify_model or args.model
    return verify_prompt_requirements(
        prompt=prompt,
        solution_content=document_text,
        model=verify_model,
        url=args.url,
        timeout_seconds=args.request_timeout,
        extra_context="Target artifact is a Markdown document such as README.md.",
    )


def verify_code(
    args: argparse.Namespace,
    prompt: str,
    code: str,
    diagnostics: dict,
) -> dict:
    verify_model = args.verify_model or args.model
    return verify_prompt_requirements(
        prompt=prompt,
        solution_content=code,
        model=verify_model,
        url=args.url,
        timeout_seconds=args.request_timeout,
        extra_context=(
            f"Runtime output:\n{diagnostics['run_output'] or 'none'}\n\n"
            f"Luacheck output:\n{diagnostics['luacheck_output'] or 'none'}"
        ),
    )


def save_final_output(output_path: str, code: str) -> None:
    save_lua_code(output_path, code)


def save_text_output(output_path: str, text: str) -> None:
    os.makedirs(os.path.dirname(os.path.abspath(output_path)) or ".", exist_ok=True)
    with open(output_path, "w", encoding="utf-8", newline="\n") as file:
        file.write(text.rstrip())
        file.write("\n")


def normalize_best_effort_output(content: str, artifact_type: str) -> str:
    if is_text_artifact_kind(artifact_type):
        return normalize_text_artifact_content(content)

    analysis = analyze_lua_response(content)
    if analysis["valid"]:
        return analysis["normalized"]
    return ""


def save_best_effort_output(output_path: str, content: str, artifact_type: str) -> bool:
    normalized = normalize_best_effort_output(content, artifact_type)
    if not normalized.strip():
        return False

    if is_text_artifact_kind(artifact_type):
        save_text_output(output_path, normalized)
    else:
        save_final_output(output_path, normalized)
    return True


def maybe_save_failed_output(
    state: SessionState,
    report: SessionReport,
    content: str,
    artifact_type: str,
) -> tuple[SessionReport, str]:
    if report.saved_output:
        return report, content

    normalized = normalize_best_effort_output(content, artifact_type)
    if not normalized.strip():
        return report, content

    try:
        if is_text_artifact_kind(artifact_type):
            save_text_output(state.output_path, normalized)
        else:
            save_final_output(state.output_path, normalized)
    except OSError as exc:
        report.diagnostics = dict(report.diagnostics)
        warning = f"Could not save latest generated file after failure: {exc}"
        existing_warning = report.diagnostics.get("run_warning", "").strip()
        report.diagnostics["run_warning"] = f"{existing_warning}\n{warning}".strip() if existing_warning else warning
        return report, content

    report.saved_output = True
    suffix = "Latest generated file was still saved to disk."
    report.message = f"{report.message} {suffix}".strip() if report.message else suffix
    register_managed_file(state, state.output_path)
    return report, normalized


def run_validation_on_temp_file(
    state: SessionState,
    code: str,
    args: argparse.Namespace,
) -> dict:
    temp_path = ""

    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="\n",
            suffix=".lua",
            prefix="__codex_tmp_",
            delete=False,
        ) as temp_file:
            temp_file.write(code)
            if code and not code.endswith("\n"):
                temp_file.write("\n")
            temp_path = temp_file.name

        return run_diagnostics(
            temp_path,
            args.lua_bin,
            args.luacheck_bin,
            args.startup_timeout,
        )
    finally:
        if temp_path and os.path.exists(temp_path):
            try:
                os.remove(temp_path)
            except OSError:
                pass


def build_format_failure_diagnostics(candidate_code: str, analysis: dict) -> dict:
    diagnostics = empty_diagnostics()
    diagnostics["program_mode"] = infer_program_mode(analysis.get("normalized", candidate_code))
    diagnostics["failure_kind"] = "format"
    diagnostics["run_output"] = analysis.get("excerpt", "")
    diagnostics["run_error"] = (
        "Model returned text that is not a standalone Lua file. "
        f"{analysis.get('reason', '').strip()}"
    ).strip()
    return diagnostics


def process_code(
    args: argparse.Namespace,
    state: SessionState,
    action: str,
    model_prompt: str,
    initial_code: str,
    verification_prompt: str,
    repair_seed_code: str = "",
) -> SessionReport:
    current_code = initial_code
    last_diagnostics = empty_diagnostics()
    last_verification = None
    consecutive_format_failures = 0
    last_valid_lua_code = ""
    requirement_attempts = 0
    previous_requirement_score = -1
    previous_requirement_fingerprint = ""

    if repair_seed_code.strip():
        seed_analysis = analyze_lua_response(repair_seed_code)
        if seed_analysis["valid"]:
            last_valid_lua_code = seed_analysis["normalized"]

    for attempt in range(1, args.max_attempts + 1):
        print(f"[{action}] attempt {attempt}/{args.max_attempts}: run and validate")
        response_analysis = analyze_lua_response(current_code)
        if response_analysis["valid"]:
            current_code = response_analysis["normalized"]
            last_valid_lua_code = current_code
            consecutive_format_failures = 0
            try:
                diagnostics = run_validation_on_temp_file(state, current_code, args)
            except OSError as exc:
                last_diagnostics["run_error"] = f"Could not create temporary Lua file for validation: {exc}"
                report = SessionReport(
                    success=False,
                    action=action,
                    attempts=attempt,
                    output_path=state.output_path,
                    working_path="",
                    saved_output=False,
                    diagnostics=last_diagnostics,
                    message="Temporary validation file could not be written.",
                )
                report, final_code = maybe_save_failed_output(state, report, last_valid_lua_code or current_code, "lua")
                return finalize_report(state, final_code, report)
        else:
            diagnostics = build_format_failure_diagnostics(current_code, response_analysis)
            consecutive_format_failures += 1
        last_diagnostics = diagnostics

        if is_tooling_problem(diagnostics):
            report = SessionReport(
                success=False,
                action=action,
                attempts=attempt,
                output_path=state.output_path,
                working_path="",
                saved_output=False,
                diagnostics=diagnostics,
                message="Environment or tooling problem. Automatic code fixes were not attempted further.",
            )
            report, final_code = maybe_save_failed_output(state, report, last_valid_lua_code or current_code, "lua")
            return finalize_report(state, final_code, report)

        if diagnostics["success"] and not args.skip_verification:
            print(f"[{action}] attempt {attempt}/{args.max_attempts}: requirements check")
            try:
                verification = verify_code(args, verification_prompt, current_code, diagnostics)
            except RuntimeError as exc:
                diagnostics = dict(diagnostics)
                diagnostics["verification_checked"] = True
                diagnostics["verification_passed"] = False
                diagnostics["verification_score"] = 0
                diagnostics["verification_summary"] = repair_mojibake(str(exc))
                diagnostics["verification_missing_requirements"] = []
                diagnostics["verification_warnings"] = []
                report = SessionReport(
                    success=False,
                    action=action,
                    attempts=attempt,
                    output_path=state.output_path,
                    working_path="",
                    saved_output=False,
                    diagnostics=diagnostics,
                    message="Requirements check failed to run.",
                )
                report, final_code = maybe_save_failed_output(state, report, last_valid_lua_code or current_code, "lua")
                return finalize_report(state, final_code, report)

            diagnostics = dict(diagnostics)
            attach_verification(diagnostics, verification)
            if not verification["passed"]:
                diagnostics["failure_kind"] = "requirements"
            last_diagnostics = diagnostics
            last_verification = verification
            if not verification["passed"]:
                requirement_attempts += 1
                if can_soft_accept_verification(diagnostics, verification):
                    try:
                        save_final_output(state.output_path, current_code)
                    except OSError as exc:
                        diagnostics = dict(diagnostics)
                        diagnostics["run_error"] = f"Could not save final Lua file: {exc}"
                        report = SessionReport(
                            success=False,
                            action=action,
                            attempts=attempt,
                            output_path=state.output_path,
                            working_path="",
                            saved_output=False,
                            diagnostics=diagnostics,
                            verification=verification,
                            message="Final file could not be written after runtime success.",
                        )
                        return finalize_report(state, current_code, report)

                    register_managed_file(state, state.output_path)
                    report = SessionReport(
                        success=True,
                        action=action,
                        attempts=attempt,
                        output_path=state.output_path,
                        working_path="",
                        saved_output=True,
                        diagnostics=diagnostics,
                        verification=verification,
                        message=(
                            "Project passed runtime checks and was saved. "
                            "Requirements review found remaining gaps, so automatic retries were stopped early."
                        ),
                    )
                    return finalize_report(state, current_code, report)

                if should_stop_requirement_retry(
                    verification,
                    requirement_attempts,
                    previous_requirement_score,
                    previous_requirement_fingerprint,
                ):
                    report = SessionReport(
                        success=False,
                        action=action,
                        attempts=attempt,
                        output_path=state.output_path,
                        working_path="",
                        saved_output=False,
                        diagnostics=diagnostics,
                        verification=verification,
                        message=(
                            "Runtime checks passed, but requirements feedback was not improving. "
                            "Automatic retries were stopped early."
                        ),
                    )
                    report, final_code = maybe_save_failed_output(state, report, last_valid_lua_code or current_code, "lua")
                    return finalize_report(state, final_code, report)

                previous_requirement_score = int(verification.get("score", 0))
                previous_requirement_fingerprint = build_verification_fingerprint(verification)

            if verification["passed"]:
                try:
                    save_final_output(state.output_path, current_code)
                except OSError as exc:
                    diagnostics = dict(diagnostics)
                    diagnostics["run_error"] = f"Could not save final Lua file: {exc}"
                    report = SessionReport(
                        success=False,
                        action=action,
                        attempts=attempt,
                        output_path=state.output_path,
                        working_path="",
                        saved_output=False,
                        diagnostics=diagnostics,
                        verification=verification,
                        message="Final file could not be written after successful validation.",
                    )
                    return finalize_report(state, current_code, report)

                register_managed_file(state, state.output_path)
                report = SessionReport(
                    success=True,
                    action=action,
                    attempts=attempt,
                    output_path=state.output_path,
                    working_path="",
                    saved_output=True,
                    diagnostics=diagnostics,
                    verification=verification,
                    message="Project passed runtime and requirements checks.",
                )
                return finalize_report(state, current_code, report)

        elif diagnostics["success"]:
            try:
                save_final_output(state.output_path, current_code)
            except OSError as exc:
                diagnostics = dict(diagnostics)
                diagnostics["run_error"] = f"Could not save final Lua file: {exc}"
                report = SessionReport(
                    success=False,
                    action=action,
                    attempts=attempt,
                    output_path=state.output_path,
                    working_path="",
                    saved_output=False,
                    diagnostics=diagnostics,
                    message="Final file could not be written after successful runtime checks.",
                )
                return finalize_report(state, current_code, report)

            register_managed_file(state, state.output_path)
            report = SessionReport(
                success=True,
                action=action,
                attempts=attempt,
                output_path=state.output_path,
                working_path="",
                saved_output=True,
                diagnostics=diagnostics,
                message="Project passed runtime checks.",
            )
            return finalize_report(state, current_code, report)

        if attempt == args.max_attempts:
            break

        print(f"[{action}] validation failed, requesting a fix from LM Studio")
        repair_source_code = last_valid_lua_code or response_analysis.get("normalized", current_code) or current_code
        try:
            if (
                last_diagnostics.get("failure_kind") == "format"
                and action in {"new", "retry"}
                and not last_valid_lua_code
            ):
                strict_reason = last_diagnostics["run_error"]
                if consecutive_format_failures > 1:
                    strict_reason = (
                        f"{strict_reason}\n\n"
                        "The model already failed a previous formatting attempt. "
                        "Return raw Lua source immediately."
                    )
                current_code = request_generated_code_strict(
                    args,
                    model_prompt,
                    strict_mode=True,
                    format_reason=strict_reason,
                )
            else:
                current_code = request_fixed_code(
                    args.model,
                    args.url,
                    args.temperature,
                    args.request_timeout,
                    model_prompt,
                    repair_source_code,
                    last_diagnostics,
                    attempt,
                )
        except RuntimeError as exc:
            last_diagnostics = dict(last_diagnostics)
            last_diagnostics["run_error"] = repair_mojibake(str(exc))
            report = SessionReport(
                success=False,
                action=action,
                attempts=attempt,
                output_path=state.output_path,
                working_path="",
                saved_output=False,
                diagnostics=last_diagnostics,
                verification=last_verification,
                message="LM Studio fix request failed.",
            )
            report, final_code = maybe_save_failed_output(state, report, last_valid_lua_code or repair_source_code, "lua")
            return finalize_report(state, final_code, report)

    report = SessionReport(
        success=False,
        action=action,
        attempts=args.max_attempts,
        output_path=state.output_path,
        working_path="",
        saved_output=False,
        diagnostics=last_diagnostics,
        verification=last_verification,
        message="Maximum attempts reached before the project passed all checks.",
    )
    report, final_code = maybe_save_failed_output(state, report, last_valid_lua_code or current_code, "lua")
    return finalize_report(state, final_code, report)


def build_document_fix_feedback(diagnostics: dict, verification: dict | None) -> str:
    lines: list[str] = []
    if diagnostics.get("run_error"):
        lines.append(f"Error to fix: {diagnostics['run_error']}")
    if verification:
        lines.append(f"Requirements summary: {verification['summary']}")
        if verification["missing_requirements"]:
            lines.append("Missing requirements:")
            lines.extend(f"- {item}" for item in verification["missing_requirements"])
        if verification["warnings"]:
            lines.append("Warnings:")
            lines.extend(f"- {item}" for item in verification["warnings"])
    return "\n".join(lines).strip() or "Tighten the document so it satisfies the request exactly."


def request_fixed_document(
    args: argparse.Namespace,
    model_prompt: str,
    current_text: str,
    diagnostics: dict,
    verification: dict | None,
) -> str:
    feedback = build_document_fix_feedback(diagnostics, verification)
    payload = {
        "model": args.model,
        "temperature": min(args.temperature, 0.1),
        "messages": [
            {"role": "system", "content": DOCUMENT_EDIT_SYSTEM_PROMPT},
            {
                "role": "user",
                "content": (
                    f"Current requirements:\n{model_prompt}\n\n"
                    f"Current document:\n{current_text}\n\n"
                    f"Issues to fix:\n{feedback}\n\n"
                    "Return only the full corrected Markdown document."
                ),
            },
        ],
    }
    response_text = request_chat_completion(args.url, payload, args.request_timeout)
    document_text = normalize_document_text(response_text)
    if not document_text:
        raise RuntimeError("LM Studio returned empty document content for the fix request.")
    return document_text


def validate_text_artifact_content(output_path: str, artifact_kind: str, text: str) -> dict:
    diagnostics = build_info_diagnostics()
    normalized = normalize_text_artifact_content(text)
    if not normalized.strip():
        diagnostics["failure_kind"] = "format"
        diagnostics["run_error"] = "Model returned an empty file instead of file content."
        return diagnostics
    if artifact_kind == "json":
        try:
            json.loads(normalized)
        except json.JSONDecodeError as exc:
            diagnostics["failure_kind"] = "syntax"
            diagnostics["run_error"] = f"JSON validation failed: {exc}"
            return diagnostics
    diagnostics["success"] = True
    diagnostics["started_ok"] = True
    diagnostics["program_mode"] = "text"
    diagnostics["run_output"] = f"Prepared {artifact_kind_display_name(artifact_kind, output_path)}."
    return diagnostics


def verify_text_artifact(
    args: argparse.Namespace,
    prompt: str,
    text: str,
    artifact_kind: str,
    output_path: str,
) -> dict:
    verify_model = args.verify_model or args.model
    return verify_prompt_requirements(
        prompt=prompt,
        solution_content=text,
        model=verify_model,
        url=args.url,
        timeout_seconds=args.request_timeout,
        extra_context=(
            f"Target artifact kind: {artifact_kind}\n"
            f"Target path: {output_path}\n"
            "The answer should be judged as the full contents of a single text file."
        ),
    )


def request_fixed_text_artifact(
    args: argparse.Namespace,
    model_prompt: str,
    current_text: str,
    diagnostics: dict,
    verification: dict | None,
) -> str:
    feedback = build_document_fix_feedback(diagnostics, verification)
    payload = {
        "model": args.model,
        "temperature": min(args.temperature, 0.1),
        "messages": [
            {"role": "system", "content": TEXT_ARTIFACT_EDIT_SYSTEM_PROMPT},
            {
                "role": "user",
                "content": (
                    f"Current requirements:\n{model_prompt}\n\n"
                    f"Current file content:\n{current_text}\n\n"
                    f"Issues to fix:\n{feedback}\n\n"
                    "Return only the full corrected file content."
                ),
            },
        ],
    }
    response_text = request_chat_completion(args.url, payload, args.request_timeout)
    text = normalize_text_artifact_content(response_text)
    if not text:
        raise RuntimeError("LM Studio returned empty file content for the fix request.")
    return text


def inspect_text_artifact_file(
    args: argparse.Namespace,
    state: SessionState,
    prompt: str,
    target: TargetSelection,
) -> SessionReport:
    try:
        explanation = request_text_artifact_explanation(
            args,
            state,
            prompt,
            target.output_path,
            target.current_code,
            target.explicit,
            target.artifact_kind or target.requested_artifact_kind,
        )
    except RuntimeError as exc:
        diagnostics = build_info_diagnostics()
        diagnostics["run_error"] = repair_mojibake(str(exc))
        return finalize_report(
            state,
            target.current_code,
            SessionReport(
                success=False,
                action="inspect",
                attempts=0,
                output_path=target.output_path,
                working_path="",
                saved_output=False,
                diagnostics=diagnostics,
                message="Explanation request failed.",
            ),
        )

    diagnostics = build_info_diagnostics()
    diagnostics["run_output"] = explanation
    return finalize_report(
        state,
        target.current_code,
        SessionReport(
            success=True,
            action="inspect",
            attempts=1,
            output_path=target.output_path,
            working_path="",
            saved_output=False,
            diagnostics=diagnostics,
            message=explanation,
        ),
    )


def process_text_artifact(
    args: argparse.Namespace,
    state: SessionState,
    action: str,
    model_prompt: str,
    initial_text: str,
    verification_prompt: str,
    artifact_kind: str,
) -> SessionReport:
    current_text = normalize_text_artifact_content(initial_text)
    last_diagnostics = build_info_diagnostics()
    last_verification = None
    consecutive_format_failures = 0
    requirement_attempts = 0
    previous_requirement_score = -1
    previous_requirement_fingerprint = ""

    for attempt in range(1, args.max_attempts + 1):
        print(f"[{action}] attempt {attempt}/{args.max_attempts}: text validation")
        diagnostics = validate_text_artifact_content(state.output_path, artifact_kind, current_text)
        if diagnostics["success"]:
            consecutive_format_failures = 0
        else:
            consecutive_format_failures += 1
        last_diagnostics = diagnostics

        if diagnostics["success"] and not args.skip_verification:
            print(f"[{action}] attempt {attempt}/{args.max_attempts}: requirements check")
            try:
                verification = verify_text_artifact(
                    args,
                    verification_prompt,
                    current_text,
                    artifact_kind,
                    state.output_path,
                )
            except RuntimeError as exc:
                diagnostics = dict(diagnostics)
                diagnostics["verification_checked"] = True
                diagnostics["verification_passed"] = False
                diagnostics["verification_score"] = 0
                diagnostics["verification_summary"] = repair_mojibake(str(exc))
                diagnostics["verification_missing_requirements"] = []
                diagnostics["verification_warnings"] = []
                report = SessionReport(
                    success=False,
                    action=action,
                    attempts=attempt,
                    output_path=state.output_path,
                    working_path="",
                    saved_output=False,
                    diagnostics=diagnostics,
                    message="Requirements check failed to run.",
                )
                report, final_text = maybe_save_failed_output(state, report, current_text, artifact_kind)
                return finalize_report(state, final_text, report)

            diagnostics = dict(diagnostics)
            attach_verification(diagnostics, verification)
            if not verification["passed"]:
                diagnostics["failure_kind"] = "requirements"
            last_diagnostics = diagnostics
            last_verification = verification
            if verification["passed"]:
                try:
                    save_text_output(state.output_path, current_text)
                except OSError as exc:
                    diagnostics = dict(diagnostics)
                    diagnostics["run_error"] = f"Could not save final file: {exc}"
                    return finalize_report(
                        state,
                        current_text,
                        SessionReport(
                            success=False,
                            action=action,
                            attempts=attempt,
                            output_path=state.output_path,
                            working_path="",
                            saved_output=False,
                            diagnostics=diagnostics,
                            verification=verification,
                            message="Final file could not be written after verification.",
                        ),
                    )
                register_managed_file(state, state.output_path)
                return finalize_report(
                    state,
                    current_text,
                    SessionReport(
                        success=True,
                        action=action,
                        attempts=attempt,
                        output_path=state.output_path,
                        working_path="",
                        saved_output=True,
                        diagnostics=diagnostics,
                        verification=verification,
                        message="File passed validation and requirements checks.",
                    ),
                )

            requirement_attempts += 1
            if can_soft_accept_verification(diagnostics, verification):
                save_text_output(state.output_path, current_text)
                register_managed_file(state, state.output_path)
                return finalize_report(
                    state,
                    current_text,
                    SessionReport(
                        success=True,
                        action=action,
                        attempts=attempt,
                        output_path=state.output_path,
                        working_path="",
                        saved_output=True,
                        diagnostics=diagnostics,
                        verification=verification,
                        message="File was saved after passing structural checks; requirements review still reported minor gaps.",
                    ),
                )

            if should_stop_requirement_retry(
                verification,
                requirement_attempts,
                previous_requirement_score,
                previous_requirement_fingerprint,
            ):
                report = SessionReport(
                    success=False,
                    action=action,
                    attempts=attempt,
                    output_path=state.output_path,
                    working_path="",
                    saved_output=False,
                    diagnostics=diagnostics,
                    verification=verification,
                    message="Text file passed structural checks, but requirements feedback stopped improving.",
                )
                report, final_text = maybe_save_failed_output(state, report, current_text, artifact_kind)
                return finalize_report(state, final_text, report)

            previous_requirement_score = int(verification.get("score", 0))
            previous_requirement_fingerprint = build_verification_fingerprint(verification)
        elif diagnostics["success"]:
            save_text_output(state.output_path, current_text)
            register_managed_file(state, state.output_path)
            return finalize_report(
                state,
                current_text,
                SessionReport(
                    success=True,
                    action=action,
                    attempts=attempt,
                    output_path=state.output_path,
                    working_path="",
                    saved_output=True,
                    diagnostics=diagnostics,
                    message="File passed validation.",
                ),
            )

        if attempt == args.max_attempts:
            break

        print(f"[{action}] validation failed, requesting a fix from LM Studio")
        try:
            if last_diagnostics.get("failure_kind") == "format" and action in {"new", "retry"}:
                current_text = request_generated_text_artifact(
                    args,
                    model_prompt,
                    strict_mode=True,
                    format_reason=last_diagnostics.get("run_error", ""),
                )
            else:
                current_text = request_fixed_text_artifact(
                    args,
                    model_prompt,
                    current_text,
                    last_diagnostics,
                    last_verification,
                )
        except RuntimeError as exc:
            last_diagnostics = dict(last_diagnostics)
            last_diagnostics["run_error"] = repair_mojibake(str(exc))
            report = SessionReport(
                success=False,
                action=action,
                attempts=attempt,
                output_path=state.output_path,
                working_path="",
                saved_output=False,
                diagnostics=last_diagnostics,
                verification=last_verification,
                message="LM Studio fix request failed.",
            )
            report, final_text = maybe_save_failed_output(state, report, current_text, artifact_kind)
            return finalize_report(state, final_text, report)

    report = SessionReport(
        success=False,
        action=action,
        attempts=args.max_attempts,
        output_path=state.output_path,
        working_path="",
        saved_output=False,
        diagnostics=last_diagnostics,
        verification=last_verification,
        message="Maximum attempts reached before the file passed all checks.",
    )
    report, final_text = maybe_save_failed_output(state, report, current_text, artifact_kind)
    return finalize_report(state, final_text, report)


def process_document(
    args: argparse.Namespace,
    state: SessionState,
    action: str,
    model_prompt: str,
    initial_text: str,
    verification_prompt: str,
) -> SessionReport:
    current_text = normalize_document_text(initial_text)
    last_diagnostics = build_info_diagnostics()
    last_verification = None
    consecutive_format_failures = 0
    requirement_attempts = 0
    previous_requirement_score = -1
    previous_requirement_fingerprint = ""

    for attempt in range(1, args.max_attempts + 1):
        print(f"[{action}] attempt {attempt}/{args.max_attempts}: document validation")
        if current_text.strip():
            diagnostics = build_info_diagnostics()
            consecutive_format_failures = 0
        else:
            diagnostics = build_info_diagnostics()
            diagnostics["failure_kind"] = "format"
            diagnostics["run_error"] = "Model returned an empty document instead of the requested file content."
            consecutive_format_failures += 1
        last_diagnostics = diagnostics

        if current_text.strip() and not args.skip_verification:
            print(f"[{action}] attempt {attempt}/{args.max_attempts}: requirements check")
            try:
                verification = verify_document(args, verification_prompt, current_text)
            except RuntimeError as exc:
                diagnostics = dict(diagnostics)
                diagnostics["verification_checked"] = True
                diagnostics["verification_passed"] = False
                diagnostics["verification_score"] = 0
                diagnostics["verification_summary"] = repair_mojibake(str(exc))
                diagnostics["verification_missing_requirements"] = []
                diagnostics["verification_warnings"] = []
                report = SessionReport(
                    success=False,
                    action=action,
                    attempts=attempt,
                    output_path=state.output_path,
                    working_path="",
                    saved_output=False,
                    diagnostics=diagnostics,
                    message="Requirements check failed to run.",
                )
                report, final_text = maybe_save_failed_output(state, report, current_text, "readme")
                return finalize_report(state, final_text, report)

            diagnostics = dict(diagnostics)
            attach_verification(diagnostics, verification)
            if not verification["passed"]:
                diagnostics["failure_kind"] = "requirements"
            last_diagnostics = diagnostics
            last_verification = verification
            if not verification["passed"]:
                requirement_attempts += 1
                if can_soft_accept_verification(diagnostics, verification):
                    try:
                        save_text_output(state.output_path, current_text)
                    except OSError as exc:
                        diagnostics = dict(diagnostics)
                        diagnostics["run_error"] = f"Could not save final document file: {exc}"
                        report = SessionReport(
                            success=False,
                            action=action,
                            attempts=attempt,
                            output_path=state.output_path,
                            working_path="",
                            saved_output=False,
                            diagnostics=diagnostics,
                            verification=verification,
                            message="Final document could not be written after verification.",
                        )
                        return finalize_report(state, current_text, report)

                    report = SessionReport(
                        success=True,
                        action=action,
                        attempts=attempt,
                        output_path=state.output_path,
                        working_path="",
                        saved_output=True,
                        diagnostics=diagnostics,
                        verification=verification,
                        message=(
                            "Document was saved. Requirements review found remaining gaps, "
                            "so automatic retries were stopped early."
                        ),
                    )
                    return finalize_report(state, current_text, report)

                if should_stop_requirement_retry(
                    verification,
                    requirement_attempts,
                    previous_requirement_score,
                    previous_requirement_fingerprint,
                ):
                    report = SessionReport(
                        success=False,
                        action=action,
                        attempts=attempt,
                        output_path=state.output_path,
                        working_path="",
                        saved_output=False,
                        diagnostics=diagnostics,
                        verification=verification,
                        message="Requirements feedback was not improving, so automatic retries were stopped early.",
                    )
                    report, final_text = maybe_save_failed_output(state, report, current_text, "readme")
                    return finalize_report(state, final_text, report)

                previous_requirement_score = int(verification.get("score", 0))
                previous_requirement_fingerprint = build_verification_fingerprint(verification)

            if verification["passed"]:
                try:
                    save_text_output(state.output_path, current_text)
                except OSError as exc:
                    diagnostics = dict(diagnostics)
                    diagnostics["run_error"] = f"Could not save final document file: {exc}"
                    report = SessionReport(
                        success=False,
                        action=action,
                        attempts=attempt,
                        output_path=state.output_path,
                        working_path="",
                        saved_output=False,
                        diagnostics=diagnostics,
                        verification=verification,
                        message="Final document could not be written after successful verification.",
                    )
                    return finalize_report(state, current_text, report)

                report = SessionReport(
                    success=True,
                    action=action,
                    attempts=attempt,
                    output_path=state.output_path,
                    working_path="",
                    saved_output=True,
                    diagnostics=diagnostics,
                    verification=verification,
                    message="Document passed requirements checks.",
                )
                return finalize_report(state, current_text, report)

        elif current_text.strip():
            try:
                save_text_output(state.output_path, current_text)
            except OSError as exc:
                diagnostics = dict(diagnostics)
                diagnostics["run_error"] = f"Could not save final document file: {exc}"
                report = SessionReport(
                    success=False,
                    action=action,
                    attempts=attempt,
                    output_path=state.output_path,
                    working_path="",
                    saved_output=False,
                    diagnostics=diagnostics,
                    message="Final document could not be written.",
                )
                return finalize_report(state, current_text, report)

            report = SessionReport(
                success=True,
                action=action,
                attempts=attempt,
                output_path=state.output_path,
                working_path="",
                saved_output=True,
                diagnostics=diagnostics,
                message="Document saved.",
            )
            return finalize_report(state, current_text, report)

        if attempt == args.max_attempts:
            break

        print(f"[{action}] validation failed, requesting a document fix from LM Studio")
        try:
            if last_diagnostics.get("failure_kind") == "format" and action in {"new", "retry"} and consecutive_format_failures:
                current_text = request_generated_document(
                    args,
                    model_prompt,
                    strict_mode=True,
                    format_reason=last_diagnostics["run_error"],
                )
            else:
                current_text = request_fixed_document(
                    args,
                    model_prompt,
                    current_text,
                    last_diagnostics,
                    last_verification,
                )
        except RuntimeError as exc:
            last_diagnostics = dict(last_diagnostics)
            last_diagnostics["run_error"] = repair_mojibake(str(exc))
            report = SessionReport(
                success=False,
                action=action,
                attempts=attempt,
                output_path=state.output_path,
                working_path="",
                saved_output=False,
                diagnostics=last_diagnostics,
                verification=last_verification,
                message="LM Studio document fix request failed.",
            )
            report, final_text = maybe_save_failed_output(state, report, current_text, "readme")
            return finalize_report(state, final_text, report)

    report = SessionReport(
        success=False,
        action=action,
        attempts=args.max_attempts,
        output_path=state.output_path,
        working_path="",
        saved_output=False,
        diagnostics=last_diagnostics,
        verification=last_verification,
        message="Maximum attempts reached before the document passed all checks.",
    )
    report, final_text = maybe_save_failed_output(state, report, current_text, "readme")
    return finalize_report(state, final_text, report)


def build_workspace_fallback_output(
    workspace_root: str,
    prompt: str,
    semantics: ParsedRequestSemantics,
) -> str:
    if semantics.explicit_path:
        return os.path.abspath(semantics.explicit_path)
    if semantics.requested_entity_type == "directory":
        return os.path.abspath(os.path.join(workspace_root, build_task_slug(prompt) or "workspace_item"))
    filename = semantics.explicit_filename or default_filename_for_artifact_kind(
        semantics.requested_artifact_kind,
        prompt,
        semantics.explicit_extension,
    )
    return os.path.abspath(os.path.join(workspace_root, filename))


def prepare_workspace_for_semantics(
    args: argparse.Namespace,
    state: SessionState,
    prompt: str,
    semantics: ParsedRequestSemantics,
) -> None:
    workspace_root = resolve_workspace_root(args, prompt)
    fallback_output = build_workspace_fallback_output(workspace_root, prompt, semantics)
    fallback_working = build_working_path(fallback_output) if looks_like_file_path(fallback_output) else ""
    if not paths_equal(workspace_root, state.workspace_root):
        switch_workspace(state, workspace_root, fallback_output, fallback_working)
    else:
        rebuild_state_inventory(state, [workspace_root, semantics.explicit_path, semantics.target_directory])


def resolve_target_for_request(
    args: argparse.Namespace,
    state: SessionState,
    prompt: str,
    semantics: ParsedRequestSemantics,
) -> TargetSelection:
    prepare_workspace_for_semantics(args, state, prompt, semantics)
    target = resolve_target(args, state, prompt, semantics)
    target_workspace_root = target.output_path if target.entity_type == "directory" else (os.path.dirname(target.output_path) or state.workspace_root)
    if target_workspace_root and not paths_equal(target_workspace_root, state.workspace_root):
        fallback_working = build_working_path(target.output_path) if target.entity_type == "file" else ""
        switch_workspace(state, target_workspace_root, target.output_path, fallback_working)
        target = resolve_target(args, state, prompt, semantics)
    return target


def execute_directory_request(
    state: SessionState,
    action: str,
    target: TargetSelection,
    semantics: ParsedRequestSemantics,
) -> SessionReport:
    activate_target_selection(state, target)
    if semantics.intent == "create":
        try:
            os.makedirs(target.output_path, exist_ok=True)
        except OSError as exc:
            diagnostics = build_info_diagnostics()
            diagnostics["run_error"] = f"Could not create directory: {exc}"
            return finalize_report(
                state,
                "",
                SessionReport(
                    success=False,
                    action=action,
                    attempts=1,
                    output_path=target.output_path,
                    working_path="",
                    saved_output=False,
                    diagnostics=diagnostics,
                    message="Directory creation failed.",
                ),
            )
        rebuild_state_inventory(state, [target.output_path])
        persist_state(state)
        return finalize_report(
            state,
            "",
            SessionReport(
                success=True,
                action=action,
                attempts=1,
                output_path=target.output_path,
                working_path="",
                saved_output=True,
                diagnostics=build_info_diagnostics(),
                message="Directory is ready and selected as the active workspace scope.",
            ),
        )
    persist_state(state)
    return finalize_report(
        state,
        "",
        SessionReport(
            success=True,
            action=action,
            attempts=1,
            output_path=target.output_path,
            working_path="",
            saved_output=False,
            diagnostics=build_info_diagnostics(),
            message="Directory selected as the active workspace scope.",
        ),
    )


def execute_lua_request(
    args: argparse.Namespace,
    state: SessionState,
    action: str,
    request_text: str,
    target: TargetSelection,
) -> SessionReport:
    explicit_target = target.explicit
    if action == "retry":
        model_prompt = build_retry_prompt(state, target.output_path, explicit_target)
        verification_prompt = build_verification_prompt(state, target.output_path, explicit_target)
        if state.current_code.strip():
            print("[retry] re-running validation and auto-fix on the current code")
            return process_code(
                args,
                state,
                "retry",
                model_prompt,
                state.current_code,
                verification_prompt,
                repair_seed_code=state.current_code,
            )
        print("[retry] current code is empty, generating again from the stored requirements")
        try:
            generated_code = request_generated_code(args, model_prompt)
        except RuntimeError as exc:
            diagnostics = empty_diagnostics()
            diagnostics["run_error"] = repair_mojibake(str(exc))
            return finalize_report(
                state,
                state.current_code,
                SessionReport(
                    success=False,
                    action="retry",
                    attempts=0,
                    output_path=state.output_path,
                    working_path="",
                    saved_output=False,
                    diagnostics=diagnostics,
                    message="Retry generation failed.",
                ),
            )
        state.current_content = generated_code
        state.current_code = generated_code
        persist_state(state)
        return process_code(
            args,
            state,
            "retry",
            model_prompt,
            generated_code,
            verification_prompt,
            repair_seed_code=state.current_code,
        )

    model_prompt = build_generation_prompt(state, request_text, target.output_path, explicit_target)
    verification_prompt = build_verification_prompt(state, target.output_path, explicit_target)
    file_context = build_file_context_for_model(state, target.output_path, explicit_target)
    overall_prompt = build_model_change_summary(state, latest_request=request_text if action == "edit" else "")
    previous_code = target.current_code
    try:
        if target.current_code.strip():
            print("[new] existing target file found, updating it for the new chat" if action == "new" else "[edit] applying the requested change")
            initial_code = request_edited_code(
                args,
                overall_prompt,
                request_text,
                target.current_code,
                file_context,
            )
        else:
            print("[new] generating initial Lua code" if action == "new" else "[edit] no current code, generating a fresh version for the updated requirements")
            initial_code = request_generated_code(args, model_prompt)
    except RuntimeError as exc:
        diagnostics = empty_diagnostics()
        diagnostics["run_error"] = repair_mojibake(str(exc))
        return finalize_report(
            state,
            target.current_code,
            SessionReport(
                success=False,
                action=action,
                attempts=0,
                output_path=state.output_path,
                working_path="",
                saved_output=False,
                diagnostics=diagnostics,
                message="Initial generation failed." if action == "new" else "Edit request failed before validation.",
            ),
        )
    state.current_content = initial_code
    state.current_code = initial_code
    persist_state(state)
    return process_code(
        args,
        state,
        action,
        model_prompt,
        initial_code,
        verification_prompt,
        repair_seed_code=previous_code,
    )


def execute_text_file_request(
    args: argparse.Namespace,
    state: SessionState,
    action: str,
    request_text: str,
    target: TargetSelection,
) -> SessionReport:
    artifact_kind = target.artifact_kind or target.requested_artifact_kind or state.artifact_type or "generic_file"
    explicit_target = target.explicit
    if action == "retry":
        model_prompt = build_text_artifact_generation_prompt(state, state.effective_prompt(), target.output_path, explicit_target, artifact_kind)
        verification_prompt = build_text_artifact_verification_prompt(state, target.output_path, explicit_target, artifact_kind)
        if state.current_code.strip():
            print("[retry] re-running validation and auto-fix on the current text file")
            return process_text_artifact(args, state, "retry", model_prompt, state.current_code, verification_prompt, artifact_kind)
        print("[retry] current file is empty, generating it again from the stored requirements")
        try:
            generated_text = request_generated_text_artifact(args, model_prompt)
        except RuntimeError as exc:
            diagnostics = build_info_diagnostics()
            diagnostics["run_error"] = repair_mojibake(str(exc))
            return finalize_report(
                state,
                state.current_code,
                SessionReport(
                    success=False,
                    action="retry",
                    attempts=0,
                    output_path=state.output_path,
                    working_path="",
                    saved_output=False,
                    diagnostics=diagnostics,
                    message="Retry file generation failed.",
                ),
            )
        state.current_content = generated_text
        state.current_code = generated_text
        persist_state(state)
        return process_text_artifact(args, state, "retry", model_prompt, generated_text, verification_prompt, artifact_kind)

    model_prompt = build_text_artifact_generation_prompt(state, request_text, target.output_path, explicit_target, artifact_kind)
    verification_prompt = build_text_artifact_verification_prompt(state, target.output_path, explicit_target, artifact_kind)
    file_context = build_generic_workspace_context(state, target.output_path, explicit_target)
    overall_prompt = build_model_change_summary(state, latest_request=request_text if action == "edit" else "")
    try:
        if target.current_code.strip():
            print("[new] existing text file found, updating it for the new chat" if action == "new" else "[edit] applying the requested text-file change")
            initial_text = request_edited_text_artifact(
                args,
                overall_prompt,
                request_text,
                target.current_code,
                file_context,
            )
        else:
            print("[new] generating initial text file" if action == "new" else "[edit] no current file, generating a fresh version for the updated requirements")
            initial_text = request_generated_text_artifact(args, model_prompt)
    except RuntimeError as exc:
        diagnostics = build_info_diagnostics()
        diagnostics["run_error"] = repair_mojibake(str(exc))
        return finalize_report(
            state,
            target.current_code,
            SessionReport(
                success=False,
                action=action,
                attempts=0,
                output_path=state.output_path,
                working_path="",
                saved_output=False,
                diagnostics=diagnostics,
                message="Initial text-file generation failed." if action == "new" else "Text-file edit request failed before validation.",
            ),
        )
    state.current_content = initial_text
    state.current_code = initial_text
    persist_state(state)
    return process_text_artifact(args, state, action, model_prompt, initial_text, verification_prompt, artifact_kind)


def execute_semantic_request(
    args: argparse.Namespace,
    state: SessionState,
    prompt: str,
    semantics: ParsedRequestSemantics,
    target: TargetSelection,
    action: str,
) -> SessionReport:
    state.last_route_intent = semantics.intent
    activate_target_selection(state, target)
    effective_kind = target.artifact_kind or target.requested_artifact_kind or semantics.requested_artifact_kind or "unknown"
    state.artifact_type = effective_kind
    if semantics.expects_existing_target and not (target.exists or bool(target.current_code.strip())):
        persist_state(state)
        return build_missing_existing_target_report(
            state,
            semantics.intent,
            target.output_path or semantics.explicit_path or semantics.target_directory,
            "No existing target file or directory was found for the requested action.",
        )
    if target.entity_type == "directory":
        return execute_directory_request(state, action, target, semantics)
    if semantics.intent == "inspect":
        if target.current_code.strip():
            state.base_prompt = build_existing_file_base_prompt(state, target.output_path, effective_kind)
        persist_state(state)
        if target.current_code.strip() and effective_kind == "lua":
            return inspect_target_file(args, state, prompt, target)
        if target.current_code.strip():
            return inspect_text_artifact_file(args, state, prompt, target)
        return finalize_report(
            state,
            target.current_code,
            SessionReport(
                success=True,
                action="inspect",
                attempts=1,
                output_path=target.output_path,
                working_path="",
                saved_output=False,
                diagnostics=build_info_diagnostics(),
                message="Target file exists, but it is empty.",
            ),
        )
    persist_state(state)
    if effective_kind == "lua":
        return execute_lua_request(args, state, action, prompt, target)
    return execute_text_file_request(args, state, action, prompt, target)


def start_new_project(args: argparse.Namespace, state: SessionState, prompt: str) -> SessionReport:
    semantics = parse_request_semantics(args, state, prompt)
    target = resolve_target_for_request(args, state, prompt, semantics)
    state.chat_id = new_chat_id()
    state.base_prompt = prompt.strip()
    state.change_requests = []
    state.last_report = None
    state.current_content = ""
    state.current_code = ""
    state.current_target_path = ""
    state.artifact_type = semantics.requested_artifact_kind or "unknown"
    state.last_route_intent = semantics.intent
    return execute_semantic_request(args, state, prompt, semantics, target, "new")


def apply_change_request(
    args: argparse.Namespace,
    state: SessionState,
    change_request: str,
) -> SessionReport:
    if not state.has_project():
        return start_new_project(args, state, change_request)

    semantics = parse_request_semantics(args, state, change_request)
    target = resolve_target_for_request(args, state, change_request, semantics)
    if not state.base_prompt.strip() and target.current_code.strip():
        state.base_prompt = build_existing_file_base_prompt(
            state,
            target.output_path,
            target.artifact_kind or semantics.requested_artifact_kind,
        )
    if semantics.intent != "inspect":
        state.change_requests.append(change_request.strip())
    return execute_semantic_request(args, state, change_request, semantics, target, "edit")


def retry_current_project(args: argparse.Namespace, state: SessionState) -> SessionReport:
    if not state.has_project():
        diagnostics = empty_diagnostics()
        diagnostics["run_error"] = "There is no active project yet."
        return finalize_report(
            state,
            state.current_code,
            SessionReport(
                success=False,
                action="retry",
                attempts=0,
                output_path=state.output_path,
                working_path="",
                saved_output=False,
                diagnostics=diagnostics,
                message="Start a project first by sending a plain-text request or using /new.",
            ),
        )

    active_target = get_active_target_artifact(state)
    if not active_target:
        diagnostics = empty_diagnostics()
        diagnostics["run_error"] = "There is no active target to retry."
        return finalize_report(
            state,
            state.current_code,
            SessionReport(
                success=False,
                action="retry",
                attempts=0,
                output_path=state.output_path,
                working_path="",
                saved_output=False,
                diagnostics=diagnostics,
                message="No active target is selected for retry.",
            ),
        )

    target = (
        build_directory_selection_for_path(
            state,
            active_target.path,
            explicit=False,
            source="carryover",
            confidence=0.9,
            reasons=["Retry keeps the active directory."],
        )
        if active_target.entity_type == "directory"
        else build_target_selection_for_path(
            state,
            active_target.path,
            explicit=False,
            source="carryover",
            requested_artifact_kind=active_target.artifact_kind,
            confidence=0.9,
            reasons=["Retry keeps the active target from the current chat."],
        )
    )
    semantics = ParsedRequestSemantics(
        intent="change",
        requested_entity_type=active_target.entity_type,
        requested_artifact_kind=active_target.artifact_kind,
        follow_active_context=True,
        expects_existing_target=False,
        create_if_missing=False,
        reason="retry current chat",
    )
    return execute_semantic_request(args, state, state.effective_prompt(), semantics, target, "retry")


def print_help() -> None:
    print("Commands:")
    print("/new <prompt>    start a new project")
    print("/edit <request>  apply a change to the current project")
    print("/retry           re-run validation and auto-fix on the current project")
    print("/status          show the current session status")
    print("/code            print the current artifact content")
    print("/prompt          print the accumulated project requirements")
    print("/path            show the target file path")
    print("/help            show this help")
    print("/exit            quit the console")
    print("Plain text:")
    print("If no project exists, plain text starts a new project.")
    print("If a project exists, plain text is treated as a change request.")
    print("Context:")
    print("Chat context is saved in the workspace and restored on the next launch.")
    print("If a file is named explicitly, only that file becomes the edit target.")
    print("Saving:")
    print("Lua requests still create a task folder when a target directory is given.")
    print("README requests are saved directly as README.md in the chosen directory.")


def print_status(state: SessionState) -> None:
    if not state.has_project():
        print("No active project.")
        print(f"Workspace: {state.workspace_root}")
        if state.managed_files:
            print(f"Managed Lua files: {len(state.managed_files)}")
        return

    active_target = get_active_target_artifact(state)
    active_directory = get_active_directory_artifact(state)
    print("Project status:")
    print(f"Workspace: {state.workspace_root}")
    print(f"Chat id: {state.chat_id}")
    print(f"Artifact type: {state.artifact_type}")
    print(f"Final output: {state.output_path}")
    print(f"Active target: {(active_target.path if active_target else (state.current_target_path or state.output_path))}")
    print(f"Active target id: {state.active_target_id or 'none'}")
    print(f"Active directory: {(active_directory.path if active_directory else state.workspace_root)}")
    print(f"Base prompt set: yes")
    print(f"Change requests: {len(state.change_requests)}")
    print(f"Known artifacts: {len(state.artifacts)}")
    print(f"Managed Lua files: {len(state.managed_files)}")
    print(f"Current content loaded: {'yes' if state.current_content.strip() else 'no'}")
    if state.last_resolution:
        print(f"Last resolution source: {state.last_resolution.source}")
        if state.last_resolution.reasons:
            print("Last resolution reasons:")
            for reason in state.last_resolution.reasons:
                print(f"- {reason}")

    if not state.last_report:
        print("Last result: no actions have completed yet.")
        return

    print(f"Last result: {'OK' if state.last_report.success else 'ERROR'}")
    print(f"Last action: {state.last_report.action}")
    print(f"Last attempts used: {state.last_report.attempts}")
    verification = state.last_report.verification
    if verification:
        print(f"Last requirements score: {verification['score']}/100")
        print(f"Last requirements summary: {verification['summary']}")


def print_paths(state: SessionState) -> None:
    if not state.has_project():
        print(f"Workspace: {state.workspace_root}")
        print("No active target yet.")
        return

    active_target = get_active_target_artifact(state)
    active_directory = get_active_directory_artifact(state)
    target_path = active_target.path if active_target else (state.current_target_path or state.output_path)
    target_status = "exists" if os.path.exists(target_path) else "not created yet"
    output_status = "exists" if os.path.exists(state.output_path) else "not created yet"
    print(f"Workspace: {state.workspace_root}")
    print(f"Active target: {target_path} ({target_status})")
    print(f"Active directory: {(active_directory.path if active_directory else state.workspace_root)}")
    print(f"Final output: {state.output_path} ({output_status})")


def print_prompt(state: SessionState) -> None:
    if not state.has_project():
        print("No active project.")
        return

    print("Current project requirements:")
    print(state.effective_prompt())


def print_code(state: SessionState) -> None:
    if not state.current_content.strip():
        print("No current artifact content is loaded.")
        return

    print(state.current_content)


def print_report(report: SessionReport, lua_bin: str) -> None:
    status = "OK" if report.success else "ERROR"
    output_extension = os.path.splitext(report.output_path)[1].lower()
    print(f"Status: {status}")
    print(f"Action: {report.action}")
    print(f"Attempts: {report.attempts}")
    print(f"Final output: {report.output_path}")
    if report.saved_output:
        print("File saved: yes")
        if output_extension == ".lua":
            print(f'Run command: {lua_bin} "{report.output_path}"')
    elif report.action == "inspect":
        print("File saved: no changes made")
    else:
        print("File saved: no")
        print("Unsaved changes remain only in chat context.")

    if report.message:
        print(f"Summary: {report.message}")

    diagnostics = report.diagnostics
    if diagnostics.get("program_mode"):
        print(f"Program mode: {diagnostics['program_mode']}")
    if diagnostics.get("failure_kind") and diagnostics["failure_kind"] not in {"unknown", "none"}:
        print(f"Failure kind: {diagnostics['failure_kind']}")

    if diagnostics["run_output"]:
        print("Console output:")
        print(diagnostics["run_output"])
    elif diagnostics["timed_out"]:
        print("Console output: script started and stayed active during the startup check.")

    if diagnostics["run_error"]:
        print("Run error:")
        print(diagnostics["run_error"])
    elif diagnostics.get("run_warning"):
        print("Run warning:")
        print(diagnostics["run_warning"])

    if diagnostics["luacheck_error"]:
        print("Luacheck error:")
        print(diagnostics["luacheck_error"])
    elif diagnostics.get("luacheck_warning"):
        print("Luacheck warning:")
        print(diagnostics["luacheck_warning"])

    verification = report.verification
    if verification:
        print(f"Requirements: {'passed' if verification['passed'] else 'failed'}")
        print(f"Requirements score: {verification['score']}/100")
        print(f"Requirements summary: {verification['summary']}")
        if verification["missing_requirements"]:
            print("Missing requirements:")
            for item in verification["missing_requirements"]:
                print(f"- {item}")
        if verification["warnings"]:
            print("Warnings:")
            for item in verification["warnings"]:
                print(f"- {item}")
    elif diagnostics.get("verification_checked"):
        print("Requirements: failed")
        print(f"Requirements score: {diagnostics.get('verification_score', 0)}/100")
        if diagnostics.get("verification_summary"):
            print(f"Requirements summary: {diagnostics['verification_summary']}")


def run_repl(args: argparse.Namespace, state: SessionState) -> int:
    print("Lua Console Builder")
    print("Type plain text to create a project or request a change.")
    print("Use /help for commands.")

    if args.prompt:
        report = start_new_project(args, state, args.prompt)
        print_paths(state)
        print_report(report, args.lua_bin)
    else:
        sync_current_code_from_active_target(state)
        persist_state(state)
        if state.has_project():
            print_paths(state)
            print(f"Restored chat context: {state.chat_id}")

    while True:
        try:
            user_input = input("chat> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return 0

        if not user_input:
            continue

        if not user_input.startswith("/"):
            report = apply_change_request(args, state, user_input)
            print_report(report, args.lua_bin)
            continue

        command, _, argument = user_input.partition(" ")
        argument = argument.strip()
        command = command.lower()

        if command == "/exit":
            return 0
        if command == "/help":
            print_help()
            continue
        if command == "/status":
            print_status(state)
            continue
        if command == "/code":
            print_code(state)
            continue
        if command == "/show":
            print_code(state)
            continue
        if command == "/prompt":
            print_prompt(state)
            continue
        if command == "/path":
            print_paths(state)
            continue
        if command == "/retry":
            report = retry_current_project(args, state)
            print_report(report, args.lua_bin)
            continue
        if command == "/new":
            if not argument:
                print("Usage: /new <prompt>")
                continue
            report = start_new_project(args, state, argument)
            print_report(report, args.lua_bin)
            continue
        if command == "/edit":
            if not argument:
                print("Usage: /edit <request>")
                continue
            report = apply_change_request(args, state, argument)
            print_report(report, args.lua_bin)
            continue

        print("Unknown command. Use /help.")


def main() -> int:
    configure_console_utf8()
    args = parse_args()
    args.output_explicit = output_argument_was_provided()
    if args.max_attempts < 1:
        print("Error: --max-attempts must be at least 1.", file=sys.stderr)
        return 1

    workspace_root = resolve_workspace_root(args, args.prompt or "")
    if args.prompt:
        output_path, working_path = resolve_output_paths(args, args.prompt)
    else:
        output_path = os.path.abspath(args.output)
        working_path = os.path.abspath(build_working_path(output_path))

    state = load_session_state(workspace_root, output_path, working_path)
    return run_repl(args, state)


if __name__ == "__main__":
    raise SystemExit(main())
