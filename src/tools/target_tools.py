"""Lua target resolution and filesystem helpers for the canonical runtime."""

from __future__ import annotations

import ntpath
import os
import re
from typing import TypedDict


class ResolvedTarget(TypedDict):
    workspace_root: str
    target_path: str
    target_directory: str
    target_explicit: bool


PATH_CANDIDATE_PATTERN = re.compile(
    r'"((?:[A-Za-z]:[\\/]|\.{1,2}[\\/]|[\\/]|(?:[A-Za-z0-9_.-]+[\\/])+).+?)"|'
    r"'((?:[A-Za-z]:[\\/]|\.{1,2}[\\/]|[\\/]|(?:[A-Za-z0-9_.-]+[\\/])+).+?)'|"
    r"((?:[A-Za-z]:[\\/]|\.{1,2}[\\/]|[\\/]|(?:[A-Za-z0-9_.-]+[\\/])+)[^\s,;]+)"
)
LUA_PATH_CANDIDATE_PATTERN = re.compile(
    r'"((?:[A-Za-z]:[\\/]|\.{1,2}[\\/]|[\\/]|(?:[A-Za-z0-9_.-]+[\\/])+).+?\.lua)"|'
    r"'((?:[A-Za-z]:[\\/]|\.{1,2}[\\/]|[\\/]|(?:[A-Za-z0-9_.-]+[\\/])+).+?\.lua)'|"
    r"((?:[A-Za-z]:[\\/]|\.{1,2}[\\/]|[\\/]|(?:[A-Za-z0-9_.-]+[\\/])+)[^\s,;]+\.lua)"
)
LUA_FILE_NAME_PATTERN = re.compile(
    r"(?<![A-Za-z0-9_.-])([A-Za-z0-9_.-]+\.lua)(?![A-Za-z0-9_.-])"
)
PATH_CONTEXT_KEYWORDS = (
    "path",
    "folder",
    "directory",
    "save to",
    "save in",
    "save under",
    "сохрани",
    "сохранить",
    "по пути",
    "в папке",
    "в папку",
    "в директории",
    "в директорию",
    "в каталоге",
    "в каталог",
    "путь",
)
DIRECT_PATH_CONTEXT_SUFFIXES = (" в", " во", " in", " to", " at")
SLUG_STOP_WORDS = {
    "a",
    "an",
    "and",
    "app",
    "application",
    "build",
    "code",
    "create",
    "for",
    "generate",
    "in",
    "lua",
    "make",
    "new",
    "please",
    "program",
    "project",
    "save",
    "script",
    "the",
    "to",
    "write",
    "в",
    "во",
    "на",
    "по",
    "для",
    "или",
    "нужно",
    "создай",
    "сделай",
    "напиши",
    "сгенерируй",
    "сохрани",
    "папка",
    "папке",
    "папку",
    "директория",
    "директории",
    "директорию",
    "каталог",
    "путь",
    "скрипт",
    "код",
    "программа",
    "проект",
    "файл",
    "sozday",
    "sozdai",
    "sozdat",
    "sdelay",
    "sdelai",
    "sdelat",
    "napishi",
    "napisat",
    "sgeneriruy",
    "sohrani",
    "sohranit",
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
    "programma",
    "proekt",
    "fail",
    "fayl",
}
CYRILLIC_TO_LATIN = {
    "а": "a",
    "б": "b",
    "в": "v",
    "г": "g",
    "д": "d",
    "е": "e",
    "ё": "yo",
    "ж": "zh",
    "з": "z",
    "и": "i",
    "й": "y",
    "к": "k",
    "л": "l",
    "м": "m",
    "н": "n",
    "о": "o",
    "п": "p",
    "р": "r",
    "с": "s",
    "т": "t",
    "у": "u",
    "ф": "f",
    "х": "h",
    "ц": "ts",
    "ч": "ch",
    "ш": "sh",
    "щ": "sch",
    "ъ": "",
    "ы": "y",
    "ь": "",
    "э": "e",
    "ю": "yu",
    "я": "ya",
}
WINDOWS_INVALID_COMPONENT_PATTERN = re.compile(r'[<>:"/\\|?*\x00-\x1f]+')
WINDOWS_RESERVED_NAMES = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    "COM1",
    "COM2",
    "COM3",
    "COM4",
    "COM5",
    "COM6",
    "COM7",
    "COM8",
    "COM9",
    "LPT1",
    "LPT2",
    "LPT3",
    "LPT4",
    "LPT5",
    "LPT6",
    "LPT7",
    "LPT8",
    "LPT9",
}
GENERIC_ACTION_TOKENS = {
    "add",
    "change",
    "create",
    "fix",
    "generate",
    "implement",
    "improve",
    "refactor",
    "refine",
    "update",
    "uluchshi",
    "uluchshit",
    "izmeni",
    "izmenit",
    "dobav",
    "dobavit",
    "ispravi",
    "ispravit",
}
CHAT_TITLE_ACTION_PREFIX = re.compile(
    r"^(?:создай|сделай|напиши|сгенерируй|улучши|доработай|исправь|добавь|реализуй|"
    r"create|make|build|generate|improve|refine|fix|add|implement|update)\b[\s:,\-.]*",
    re.IGNORECASE,
)
CHAT_TITLE_TRAILING_FILLER = {
    "в",
    "во",
    "на",
    "по",
    "для",
    "and",
    "at",
    "for",
    "in",
    "to",
    "with",
}
LOCATION_CLAUSE_PATTERN = re.compile(
    r"\b(?:в\s+папке|в\s+папку|в\s+директории|в\s+директорию|в\s+каталоге|в\s+каталог|"
    r"по\s+пути|save\s+to|save\s+in|save\s+under|in\s+folder|in\s+directory|under\s+path)\b.*$",
    re.IGNORECASE,
)


def clean_path_candidate(candidate: str) -> str:
    """Trim quotes and trailing punctuation from a path candidate."""
    return candidate.strip().strip("\"'").rstrip(".,;:!?)]}").strip()


def resolve_prompt_path(candidate: str, base_dir: str) -> str:
    """Resolve user-provided path relative to the current workspace root."""
    expanded = os.path.expandvars(os.path.expanduser(candidate))
    if os.path.isabs(expanded):
        return os.path.abspath(expanded)
    return os.path.abspath(os.path.join(base_dir, expanded))


def looks_like_file_path(path: str) -> bool:
    """Return True when the path clearly names a known text/Lua file."""
    if not path:
        return False
    file_name = os.path.basename(path.rstrip("\\/"))
    root, extension = os.path.splitext(file_name)
    known_extensions = {".lua", ".md", ".txt", ".json", ".yaml", ".yml", ".toml", ".ini", ".cfg"}
    return bool(root and extension.lower() in known_extensions)


def extract_explicit_lua_path_candidate(prompt: str) -> str | None:
    """Extract the first explicit Lua file path from the prompt, if present."""
    for match in LUA_PATH_CANDIDATE_PATTERN.finditer(prompt):
        raw_path = next((group for group in match.groups() if group), "")
        candidate = clean_path_candidate(raw_path)
        if candidate:
            return candidate
    for match in LUA_FILE_NAME_PATTERN.finditer(prompt):
        candidate = clean_path_candidate(match.group(1))
        if candidate:
            return candidate
    return None


def _iter_path_candidates(prompt: str) -> list[tuple[int, int, str]]:
    matches: list[tuple[int, int, str]] = []
    for match in PATH_CANDIDATE_PATTERN.finditer(prompt):
        raw_path = next((group for group in match.groups() if group), "")
        candidate = clean_path_candidate(raw_path)
        if candidate:
            matches.append((match.start(), match.end(), candidate))
    return matches


def _looks_like_direct_location_reference(context: str) -> bool:
    lowered = context.rstrip().lower()
    if lowered.endswith(DIRECT_PATH_CONTEXT_SUFFIXES):
        return True
    normalized = transliterate_for_slug(lowered)
    return normalized.rstrip().endswith(DIRECT_PATH_CONTEXT_SUFFIXES)


def _iter_requested_paths(prompt: str) -> list[tuple[int, int, str]]:
    matches: list[tuple[int, int, str]] = []
    for start, end, candidate in _iter_path_candidates(prompt):
        context = prompt[max(0, start - 80):start].lower()
        normalized_context = transliterate_for_slug(context)
        has_keyword = any(
            keyword in context or keyword in normalized_context
            for keyword in PATH_CONTEXT_KEYWORDS
        )
        if has_keyword or _looks_like_direct_location_reference(context):
            matches.append((start, end, candidate))
    return matches


def extract_requested_output_directory(prompt: str, workspace_root: str) -> str | None:
    """Extract the directory scope the user wants to write into."""
    requested_matches = _iter_requested_paths(prompt)
    if not requested_matches:
        raw_matches = _iter_path_candidates(prompt)
        if len(raw_matches) == 1:
            requested_matches = raw_matches

    for _, _, candidate in requested_matches:
        resolved = resolve_prompt_path(candidate, workspace_root)
        if looks_like_file_path(resolved):
            resolved = os.path.dirname(resolved)
        if resolved:
            return os.path.abspath(resolved)
    return None


def transliterate_for_slug(text: str) -> str:
    """Transliterate Cyrillic text so prompt-derived file names stay readable."""
    parts: list[str] = []
    for char in text.lower():
        if char in CYRILLIC_TO_LATIN:
            parts.append(CYRILLIC_TO_LATIN[char])
        elif char.isascii():
            parts.append(char)
        else:
            parts.append(" ")
    return "".join(parts)


def _strip_requested_paths(prompt: str) -> str:
    cleaned = prompt
    for start, end, _ in reversed(_iter_path_candidates(prompt)):
        cleaned = f"{cleaned[:start]} {cleaned[end:]}"
    return cleaned


def _strip_location_clause(prompt: str) -> str:
    """Remove trailing location phrases after path candidates are stripped."""
    return LOCATION_CLAUSE_PATTERN.sub("", prompt).strip()


def sanitize_windows_component(
    component: str,
    *,
    fallback: str = "item",
    lowercase: bool = False,
) -> str:
    """Normalize a single Windows path segment into a safe filesystem name."""
    cleaned = WINDOWS_INVALID_COMPONENT_PATTERN.sub("_", component.strip())
    cleaned = re.sub(r"\s+", "_", cleaned)
    cleaned = re.sub(r"[_-]{2,}", "_", cleaned)
    cleaned = cleaned.strip(" ._-")

    if lowercase:
        cleaned = cleaned.lower()

    if not cleaned:
        cleaned = fallback.lower() if lowercase else fallback

    root, extension = os.path.splitext(cleaned)
    root = root.rstrip(" .")
    extension = extension.rstrip(" .")
    if not root:
        root = fallback.lower() if lowercase else fallback
    if root.upper() in WINDOWS_RESERVED_NAMES:
        root = f"{root}_item"

    normalized = f"{root}{extension}"
    normalized = normalized.strip(" .")
    return normalized[:80].rstrip(" .") or (fallback.lower() if lowercase else fallback)


def sanitize_creation_path(path: str) -> str:
    """Sanitize a user-provided Windows-style path before creating folders/files."""
    if not path:
        return path

    drive, tail = ntpath.splitdrive(path)
    is_windows_like = bool(drive) or "\\" in path
    if not is_windows_like and os.name != "nt":
        return path

    absolute = tail.startswith(("\\", "/"))
    parts = [part for part in re.split(r"[\\/]+", tail) if part]
    sanitized_parts: list[str] = []
    for index, part in enumerate(parts):
        if part in (".", ".."):
            sanitized_parts.append(part)
            continue
        fallback = "folder" if index < len(parts) - 1 else "item"
        sanitized_parts.append(sanitize_windows_component(part, fallback=fallback))

    rebuilt = "\\".join(sanitized_parts)
    if drive:
        return f"{drive}\\{rebuilt}" if rebuilt else f"{drive}\\"
    if absolute:
        return f"\\{rebuilt}" if rebuilt else "\\"
    return rebuilt


def _collect_prompt_tokens(prompt: str) -> list[str]:
    cleaned_prompt = _strip_location_clause(_strip_requested_paths(prompt))
    transliterated = transliterate_for_slug(cleaned_prompt)
    raw_tokens = re.findall(r"[a-z0-9]+", transliterated)

    filtered: list[str] = []
    seen: set[str] = set()
    for token in raw_tokens:
        if token in SLUG_STOP_WORDS or token in GENERIC_ACTION_TOKENS:
            continue
        if token in {"dd", "hh", "mm", "yyyy", "yy"}:
            continue
        if not token.isdigit() and len(token) < 2:
            continue
        if token in seen:
            continue
        seen.add(token)
        filtered.append(token)
    return filtered


def build_task_slug(prompt: str) -> str:
    """Build a deterministic, filesystem-friendly slug from the prompt."""
    filtered = _collect_prompt_tokens(prompt)
    if not filtered:
        return "lua_project"
    slug = "_".join(filtered[:3]).strip("_")
    return sanitize_windows_component(slug, fallback="lua_project", lowercase=True)


def humanize_identifier(value: str) -> str:
    """Convert a slug/file stem into a compact human-readable label."""
    if not value:
        return ""
    normalized = re.sub(r"[_\-.]+", " ", value).strip()
    normalized = re.sub(r"\s+", " ", normalized)
    if not normalized:
        return ""
    return normalized[0].upper() + normalized[1:]


def build_chat_title(prompt: str, target_path: str = "", fallback: str = "Новый чат") -> str:
    """Build a compact chat title from a cleaned prompt and optional target path."""
    cleaned_prompt = _strip_location_clause(_strip_requested_paths(prompt))
    single_line = " ".join(cleaned_prompt.split())
    single_line = CHAT_TITLE_ACTION_PREFIX.sub("", single_line).strip(" -:,.")

    title_tokens = single_line.split()
    while title_tokens and title_tokens[-1].lower() in CHAT_TITLE_TRAILING_FILLER:
        title_tokens.pop()
    title = " ".join(title_tokens).strip(" -:,.")

    if not title and target_path:
        title = humanize_identifier(os.path.splitext(os.path.basename(target_path))[0])
    if not title:
        title = fallback
    elif title:
        title = title[0].upper() + title[1:]
    if len(title) <= 72:
        return title
    return f"{title[:69].rstrip()}..."


def resolve_lua_target(
    prompt: str,
    workspace_root: str,
    current_target_path: str = "",
    allow_fallback: bool = False,
) -> ResolvedTarget:
    """Resolve the active Lua target for this prompt."""
    effective_workspace = os.path.abspath(workspace_root or os.getcwd())
    explicit_candidate = extract_explicit_lua_path_candidate(prompt)
    if explicit_candidate:
        target_path = sanitize_creation_path(
            resolve_prompt_path(explicit_candidate, effective_workspace)
        )
        target_directory = os.path.dirname(target_path) or effective_workspace
        return {
            "workspace_root": target_directory,
            "target_path": target_path,
            "target_directory": target_directory,
            "target_explicit": True,
        }

    requested_directory = extract_requested_output_directory(prompt, effective_workspace)
    if requested_directory:
        requested_directory = sanitize_creation_path(requested_directory)
        slug = build_task_slug(prompt)
        target_directory = os.path.join(requested_directory, slug)
        target_path = os.path.join(target_directory, f"{slug}.lua")
        return {
            "workspace_root": requested_directory,
            "target_path": os.path.abspath(target_path),
            "target_directory": os.path.abspath(target_directory),
            "target_explicit": False,
        }

    if current_target_path:
        normalized_target = sanitize_creation_path(os.path.abspath(current_target_path))
        target_directory = os.path.dirname(normalized_target) or effective_workspace
        return {
            "workspace_root": target_directory,
            "target_path": normalized_target,
            "target_directory": target_directory,
            "target_explicit": False,
        }

    if not allow_fallback:
        return {
            "workspace_root": effective_workspace,
            "target_path": "",
            "target_directory": effective_workspace,
            "target_explicit": False,
        }

    slug = build_task_slug(prompt)
    target_path = os.path.abspath(os.path.join(effective_workspace, f"{slug}.lua"))
    return {
        "workspace_root": effective_workspace,
        "target_path": target_path,
        "target_directory": os.path.dirname(target_path) or effective_workspace,
        "target_explicit": False,
    }


def read_text_if_exists(path: str) -> str:
    """Read UTF-8 text from disk when the file exists."""
    if not path or not os.path.isfile(path):
        return ""
    with open(path, "r", encoding="utf-8", errors="replace") as file:
        return file.read().replace("\r\n", "\n")


def load_target_code(target_path: str) -> str:
    """Load Lua code for the active target if the file already exists."""
    return read_text_if_exists(target_path).strip()


def save_final_output(output_path: str, code: str) -> None:
    """Persist the generated Lua code to the resolved target path."""
    directory = os.path.dirname(os.path.abspath(output_path))
    if directory:
        os.makedirs(directory, exist_ok=True)
    with open(output_path, "w", encoding="utf-8", newline="\n") as file:
        file.write(code.rstrip())
        file.write("\n")
