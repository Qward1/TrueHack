#!/usr/bin/env python3
import argparse
import os
import re
import sys

from check_lua import check_lua_file
from console_utils import configure_console_utf8
from lmstudio_client import DEFAULT_MODEL, DEFAULT_REQUEST_TIMEOUT, DEFAULT_URL, request_chat_completion
from prompt_verifier import verify_prompt_requirements
from run_lua import run_lua_file


DEFAULT_OUTPUT = "generated.lua"
DEFAULT_LUA_BIN = "lua"
DEFAULT_LUACHECK_BIN = "luacheck"
DEFAULT_SYSTEM_PROMPT = (
    "You generate clean, correct Lua code from the user's request. "
    "Return only Lua code without markdown fences or explanations. "
    "If the program is a Windows console app, prefer ASCII-only UI text unless the user explicitly asks for Unicode or you configure UTF-8 safely."
)
STRICT_LUA_OUTPUT_SUFFIX = (
    " The first non-whitespace character of your response must be valid Lua source code or '-' for a Lua comment. "
    "Never prefix the file with explanations, prose, labels, or markdown."
)
ZERO_WIDTH_PATTERN = re.compile(r"[\u200b\u200c\u200d\ufeff]")
PROBABLE_LUA_LINE_PATTERN = re.compile(
    r"^(--|local\b|function\b|if\b|for\b|while\b|repeat\b|return\b|break\b|goto\b|do\b|"
    r"print\s*\(|io\.|os\.|table\.|math\.|string\.|package\.|require\s*\(|"
    r"[A-Za-z_][A-Za-z0-9_:.]*\s*(?:=|\())"
)
LUA_SIGNAL_PATTERN = re.compile(
    r"\b(local|function|if|then|elseif|end|for|while|repeat|until|return|break|goto|do|"
    r"print|io|os|table|math|string|package|require)\b|--"
)
RUSSIAN_PROSE_PREFIXES = (
    "да",
    "вот",
    "конечно",
    "исправленный",
    "обновленный",
    "данный",
    "этот",
    "ниже",
)
CODE_SUFFIX_MARKERS = ("code:", "код:")
PROSE_PREFIXES = (
    "да",
    "вот",
    "конечно",
    "исправленный",
    "обновленный",
    "данный",
    "этот",
    "ниже",
    "sure",
    "here",
    "the following",
    "updated",
    "corrected",
    "this lua",
    "lua script",
    "code:",
)


def build_payload(model: str, user_prompt: str, system_prompt: str, temperature: float) -> dict:
    return {
        "model": model,
        "temperature": temperature,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
    }


def request_lua_code(url: str, payload: dict, timeout_seconds: float = DEFAULT_REQUEST_TIMEOUT) -> str:
    return request_chat_completion(url, payload, timeout_seconds)


def build_strict_system_prompt(system_prompt: str) -> str:
    return f"{system_prompt.rstrip()}{STRICT_LUA_OUTPUT_SUFFIX}"


def strip_explanatory_preamble(cleaned: str) -> str:
    lines = cleaned.split("\n")
    start_index = 0

    for index, line in enumerate(lines):
        stripped = line.strip()
        if not stripped:
            continue
        lower = stripped.lower().lstrip("> -*").strip()
        if PROBABLE_LUA_LINE_PATTERN.match(stripped):
            start_index = index
            break
        if lower.endswith(("код:", "code:")) or any(lower.startswith(prefix) for prefix in PROSE_PREFIXES):
            continue
        if index >= 5:
            break

    trimmed = "\n".join(lines[start_index:]).strip()
    while trimmed.endswith("```"):
        trimmed = trimmed[:-3].rstrip()
    return trimmed


def strip_explanatory_preamble_safe(cleaned: str) -> str:
    lines = cleaned.split("\n")
    start_index = 0

    for index, line in enumerate(lines):
        stripped = line.strip()
        if not stripped:
            continue
        lower = stripped.lower().lstrip("> -*").strip()
        if PROBABLE_LUA_LINE_PATTERN.match(stripped):
            start_index = index
            break
        if lower.endswith(CODE_SUFFIX_MARKERS) or any(
            lower.startswith(prefix) for prefix in (*PROSE_PREFIXES, *RUSSIAN_PROSE_PREFIXES)
        ):
            continue
        if index >= 5:
            break

    trimmed = "\n".join(lines[start_index:]).strip()
    while trimmed.endswith("```"):
        trimmed = trimmed[:-3].rstrip()
    return trimmed


def normalize_lua_code(text: str) -> str:
    cleaned = ZERO_WIDTH_PATTERN.sub("", text).replace("\r\n", "\n").replace("\r", "\n").strip()
    fenced = re.search(r"```(?:lua)?\s*(.*?)```", cleaned, flags=re.IGNORECASE | re.DOTALL)
    if fenced:
        cleaned = fenced.group(1).strip()
    else:
        cleaned = strip_explanatory_preamble_safe(cleaned)

    return cleaned.strip()


def analyze_lua_response(text: str) -> dict:
    normalized = normalize_lua_code(text)
    if not normalized:
        return {
            "valid": False,
            "reason": "Model returned an empty response instead of a Lua file.",
            "normalized": "",
            "excerpt": "",
        }

    non_empty_lines = [line.strip() for line in normalized.splitlines() if line.strip()]
    first_line = non_empty_lines[0] if non_empty_lines else ""
    first_line_lower = first_line.lower()
    starts_like_lua = bool(PROBABLE_LUA_LINE_PATTERN.match(first_line))
    lua_signal_count = len(LUA_SIGNAL_PATTERN.findall(normalized[:2000]))
    cyrillic_count = sum(
        1 for char in normalized[:400]
        if "\u0400" <= char <= "\u04ff"
    )
    prose_prefix = any(first_line_lower.startswith(prefix) for prefix in PROSE_PREFIXES)
    if not prose_prefix:
        prose_prefix = any(first_line_lower.startswith(prefix) for prefix in RUSSIAN_PROSE_PREFIXES)

    if prose_prefix and not starts_like_lua:
        reason = "Model prefixed the response with explanatory text instead of starting with Lua code."
    elif cyrillic_count >= 8 and lua_signal_count == 0:
        reason = "Model returned natural-language text instead of Lua code."
    elif not starts_like_lua and lua_signal_count == 0:
        reason = "Response does not look like a standalone Lua file."
    else:
        reason = ""

    return {
        "valid": not reason,
        "reason": reason,
        "normalized": normalized,
        "excerpt": normalized[:500].strip(),
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Ask a model in LM Studio to generate Lua code from a prompt."
    )
    parser.add_argument("prompt", help="Prompt describing the Lua code to generate.")
    parser.add_argument(
        "--model",
        default=os.getenv("LMSTUDIO_MODEL", DEFAULT_MODEL),
        help="Model name loaded in LM Studio.",
    )
    parser.add_argument(
        "--url",
        default=os.getenv("LMSTUDIO_URL", DEFAULT_URL),
        help="LM Studio chat completions endpoint.",
    )
    parser.add_argument(
        "--temperature",
        type=float,
        default=0.2,
        help="Sampling temperature.",
    )
    parser.add_argument(
        "--system-prompt",
        default=DEFAULT_SYSTEM_PROMPT,
        help="System prompt sent to the model.",
    )
    parser.add_argument(
        "--output",
        default=DEFAULT_OUTPUT,
        help="Path to the Lua file where the generated code will be saved.",
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
        "--request-timeout",
        type=float,
        default=DEFAULT_REQUEST_TIMEOUT,
        help="Seconds to wait for the LM Studio response.",
    )
    parser.add_argument(
        "--verify-model",
        default="",
        help="Model name to use for prompt-requirements verification. Defaults to --model.",
    )
    parser.add_argument(
        "--skip-verification",
        action="store_true",
        help="Skip the LLM-based prompt requirements verification step.",
    )
    return parser.parse_args()


def save_lua_code(path: str, lua_code: str) -> None:
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)

    with open(path, "w", encoding="utf-8") as file:
        file.write(lua_code)
        if lua_code and not lua_code.endswith("\n"):
            file.write("\n")


def merge_process_output(stdout: str, stderr: str) -> str:
    parts = []
    if stdout.strip():
        parts.append(stdout.rstrip())
    if stderr.strip():
        parts.append(stderr.rstrip())
    return "\n".join(parts)


def print_success_report(lua_file: str, run_output: str, luacheck_output: str) -> None:
    print("Status: OK")
    print(f"Lua file: {lua_file}")

    if run_output:
        print("Run result:")
        print(run_output)
    else:
        print("Run result: script finished without console output.")

    if luacheck_output:
        print("Luacheck:")
        print(luacheck_output)
    else:
        print("Luacheck: no output, check completed successfully.")


def print_error_report(lua_file: str, generation_error: str, run_error: str, luacheck_error: str) -> None:
    print("Status: ERROR", file=sys.stderr)
    print(f"Lua file: {lua_file}", file=sys.stderr)

    if generation_error:
        print("Generation error:", file=sys.stderr)
        print(generation_error, file=sys.stderr)

    if run_error:
        print("Run error:", file=sys.stderr)
        print(run_error, file=sys.stderr)

    if luacheck_error:
        print("Luacheck error:", file=sys.stderr)
        print(luacheck_error, file=sys.stderr)


def print_verification_report(verification: dict) -> None:
    print("Requirements check:")
    print(f"Passed: {'yes' if verification['passed'] else 'no'}")
    print(f"Score: {verification['score']}/100")
    print(f"Summary: {verification['summary']}")

    if verification["missing_requirements"]:
        print("Missing requirements:")
        for item in verification["missing_requirements"]:
            print(f"- {item}")

    if verification["warnings"]:
        print("Warnings:")
        for item in verification["warnings"]:
            print(f"- {item}")


def main() -> int:
    configure_console_utf8()
    args = parse_args()
    payload = build_payload(
        model=args.model,
        user_prompt=args.prompt,
        system_prompt=args.system_prompt,
        temperature=args.temperature,
    )

    try:
        response_text = request_lua_code(args.url, payload, args.request_timeout)
        analysis = analyze_lua_response(response_text)
        lua_code = analysis["normalized"]
        if not analysis["valid"]:
            strict_payload = build_payload(
                model=args.model,
                user_prompt=(
                    f"{args.prompt}\n\n"
                    "Previous model response format issue to avoid:\n"
                    f"{analysis['reason']}\n\n"
                    "Return only the full Lua file."
                ),
                system_prompt=build_strict_system_prompt(args.system_prompt),
                temperature=min(args.temperature, 0.05),
            )
            response_text = request_lua_code(args.url, strict_payload, args.request_timeout)
            lua_code = analyze_lua_response(response_text)["normalized"]
        if not lua_code:
            raise RuntimeError("LM Studio returned empty Lua code.")
    except RuntimeError as exc:
        print_error_report(args.output, str(exc), "", "")
        return 1

    try:
        save_lua_code(args.output, lua_code)
    except OSError as exc:
        print_error_report(
            args.output,
            f"Could not save Lua file '{args.output}': {exc}",
            "",
            "",
        )
        return 1

    run_error = ""
    luacheck_error = ""
    run_output = ""
    luacheck_output = ""
    exit_code = 0

    try:
        run_result = run_lua_file(args.output, args.lua_bin)
        run_output = merge_process_output(run_result["stdout"], run_result["stderr"])
        if not run_result["success"]:
            run_error = run_output or f"Lua process exited with code {run_result['returncode']}."
            exit_code = run_result["returncode"] or 1
    except (FileNotFoundError, RuntimeError) as exc:
        run_error = str(exc)
        exit_code = 1

    try:
        luacheck_result = check_lua_file(args.output, args.luacheck_bin)
        luacheck_output = merge_process_output(
            luacheck_result["stdout"],
            luacheck_result["stderr"],
        )
        if not luacheck_result["success"]:
            luacheck_error = (
                luacheck_output
                or f"luacheck exited with code {luacheck_result['returncode']}."
            )
            if exit_code == 0:
                exit_code = luacheck_result["returncode"] or 1
    except (FileNotFoundError, RuntimeError) as exc:
        luacheck_error = str(exc)
        if exit_code == 0:
            exit_code = 1

    if run_error or luacheck_error:
        print_error_report(args.output, "", run_error, luacheck_error)
        return exit_code or 1

    if not args.skip_verification:
        verify_model = args.verify_model or args.model
        try:
            verification = verify_prompt_requirements(
                prompt=args.prompt,
                solution_content=lua_code,
                model=verify_model,
                url=args.url,
                timeout_seconds=args.request_timeout,
                extra_context=(
                    f"Runtime output:\n{run_output or 'none'}\n\n"
                    f"Luacheck output:\n{luacheck_output or 'none'}"
                ),
            )
        except RuntimeError as exc:
            print_error_report(args.output, f"Verification failed: {exc}", "", "")
            return 1

        if not verification["passed"]:
            print_error_report(
                args.output,
                "Prompt requirements verification failed.",
                "",
                "",
            )
            print_verification_report(verification)
            return 1

    print_success_report(args.output, run_output, luacheck_output)
    if not args.skip_verification:
        print_verification_report(verification)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
