#!/usr/bin/env python3
import argparse
import os
import re
import sys

from check_lua import check_lua_file
from console_utils import configure_console_utf8
from generate import (
    DEFAULT_SYSTEM_PROMPT,
    analyze_lua_response,
    build_strict_system_prompt,
    build_payload,
    normalize_lua_code,
    request_lua_code,
    save_lua_code,
)
from prompt_verifier import verify_prompt_requirements
from run_lua import run_lua_file


DEFAULT_URL = "http://127.0.0.1:1234/v1/chat/completions"
DEFAULT_MODEL = "local-model"
DEFAULT_LUA_FILE = "generated.lua"
DEFAULT_LUA_BIN = "lua"
DEFAULT_LUACHECK_BIN = "luacheck"
DEFAULT_MAX_ATTEMPTS = 5
DEFAULT_TEMPERATURE = 0.1
DEFAULT_STARTUP_TIMEOUT = 3.0
DEFAULT_REQUEST_TIMEOUT = 600.0
FIX_SYSTEM_PROMPT = (
    "You fix broken Lua code using the user's goal and diagnostics. "
    "Return only corrected Lua code without markdown fences, explanations, or extra text. "
    "Do not remove legitimate interactivity just to pass startup checks. "
    "If the program is a Windows console app, prefer ASCII-only UI text unless you explicitly configure UTF-8 safely."
)
STRICT_FIX_SYSTEM_PROMPT = build_strict_system_prompt(FIX_SYSTEM_PROMPT)


def classify_failure_kind(diagnostics: dict) -> str:
    explicit_kind = diagnostics.get("failure_kind", "").strip().lower()
    if explicit_kind:
        return explicit_kind

    combined = "\n".join(
        part for part in (
            diagnostics.get("run_error", ""),
            diagnostics.get("luacheck_error", ""),
            diagnostics.get("verification_summary", ""),
        )
        if part
    ).lower()

    if is_tooling_problem(diagnostics):
        return "tooling"
    if "unexpected symbol" in combined or "expected statement" in combined:
        return "syntax"
    if diagnostics.get("verification_checked") and not diagnostics.get("verification_passed"):
        return "requirements"
    if diagnostics.get("run_error"):
        return "runtime"
    if diagnostics.get("luacheck_error"):
        return "lint"
    return "unknown"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Fix Lua code with LM Studio until lua run and luacheck pass."
    )
    parser.add_argument("prompt", help="Original prompt describing what the Lua code should do.")
    parser.add_argument(
        "--lua-file",
        default=DEFAULT_LUA_FILE,
        help="Path to the Lua file to fix.",
    )
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
        default=DEFAULT_TEMPERATURE,
        help="Sampling temperature for fix attempts.",
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
        help="Maximum number of LLM fix attempts.",
    )
    parser.add_argument(
        "--startup-timeout",
        type=float,
        default=DEFAULT_STARTUP_TIMEOUT,
        help="Seconds to wait for Lua startup before treating the run as successful.",
    )
    parser.add_argument(
        "--reuse-existing",
        action="store_true",
        help="Reuse the existing Lua file instead of generating fresh code from the current prompt.",
    )
    parser.add_argument(
        "--request-timeout",
        type=float,
        default=DEFAULT_REQUEST_TIMEOUT,
        help="Seconds to wait for each LM Studio request.",
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


def merge_process_output(stdout: str, stderr: str) -> str:
    parts = []
    if stdout.strip():
        parts.append(stdout.rstrip())
    if stderr.strip():
        parts.append(stderr.rstrip())
    return "\n".join(parts)


def repair_mojibake(text: str) -> str:
    if not text.strip():
        return text

    candidates = [text]
    for from_encoding, to_encoding in (
        ("cp1251", "cp866"),
        ("cp866", "cp1251"),
        ("latin1", "cp1251"),
        ("latin1", "cp866"),
    ):
        try:
            candidates.append(text.encode(from_encoding).decode(to_encoding))
        except UnicodeError:
            continue

    keywords = (
        "не является",
        "внутренней",
        "внешней",
        "командой",
        "программой",
        "файлом",
        "unexpected symbol",
        "syntax error",
        "warning",
        "error",
        "ошибка",
        "module",
        "not found",
    )

    def score(candidate: str) -> float:
        lower = candidate.lower()
        keyword_score = sum(12 for keyword in keywords if keyword in lower)
        cyrillic_score = sum(1 for char in candidate.lower() if "а" <= char <= "я")
        replacement_penalty = candidate.count("\ufffd") * 20
        mojibake_penalty = sum(candidate.count(char) for char in "¤¦©®ўҐђ‘’")
        return keyword_score + cyrillic_score - replacement_penalty - mojibake_penalty

    return max(candidates, key=score).strip()


def contains_mojibake(text: str) -> bool:
    if not text.strip():
        return False

    markers = ("╨", "╤", "Ð", "Ñ", "�")
    if any(marker in text for marker in markers):
        return True

    rs_count = text.count("Р") + text.count("С")
    if rs_count >= 4 and rs_count * 3 >= len(text):
        return True

    pair_count = 0
    for index in range(len(text) - 1):
        current = text[index]
        next_char = text[index + 1]
        if current in ("Р", "С") and (
            ("А" <= next_char <= "я") or next_char in "Ёё"
        ):
            pair_count += 1

    return pair_count >= 4


def infer_program_mode(lua_code: str) -> str:
    interactive_patterns = (
        r"\bio\.read\s*\(",
        r"\bio\.stdin\s*:\s*read\s*\(",
        r"\bio\.stdin:read\s*\(",
    )
    for pattern in interactive_patterns:
        if re.search(pattern, lua_code):
            return "interactive"
    return "batch"


def read_lua_code(lua_file: str) -> str:
    with open(lua_file, "r", encoding="utf-8") as file:
        return file.read()


def load_existing_code(lua_file: str) -> str:
    existing_code = normalize_lua_code(read_lua_code(lua_file))
    if not existing_code.strip():
        raise RuntimeError(f"Lua file '{lua_file}' is empty.")
    save_lua_code(lua_file, existing_code)
    return existing_code


def ensure_initial_code(args: argparse.Namespace) -> tuple[str, str]:
    if args.reuse_existing and os.path.exists(args.lua_file):
        return load_existing_code(args.lua_file), ""

    payload = build_payload(args.model, args.prompt, DEFAULT_SYSTEM_PROMPT, args.temperature)
    try:
        response_text = request_lua_code(args.url, payload, args.request_timeout)
        analysis = analyze_lua_response(response_text)
        lua_code = analysis["normalized"]
        if not analysis["valid"]:
            strict_payload = build_payload(
                args.model,
                (
                    f"{args.prompt}\n\n"
                    "Previous model response format issue to avoid:\n"
                    f"{analysis['reason']}\n\n"
                    "Return only the full Lua file."
                ),
                build_strict_system_prompt(DEFAULT_SYSTEM_PROMPT),
                min(args.temperature, 0.05),
            )
            response_text = request_lua_code(args.url, strict_payload, args.request_timeout)
            lua_code = analyze_lua_response(response_text)["normalized"]
        if not lua_code:
            raise RuntimeError("LM Studio returned empty Lua code.")
        save_lua_code(args.lua_file, lua_code)
        return lua_code, ""
    except RuntimeError as exc:
        if os.path.exists(args.lua_file):
            warning = (
                f"Initial LM Studio generation failed: {repair_mojibake(str(exc))}\n"
                f"Reusing existing Lua file: {args.lua_file}"
            )
            return load_existing_code(args.lua_file), warning
        raise


def run_diagnostics(lua_file: str, lua_bin: str, luacheck_bin: str, startup_timeout: float) -> dict:
    run_error = ""
    run_warning = ""
    luacheck_error = ""
    luacheck_warning = ""
    run_output = ""
    luacheck_output = ""
    started_ok = False
    timed_out = False
    program_mode = "batch"

    try:
        program_mode = infer_program_mode(read_lua_code(lua_file))
    except OSError:
        program_mode = "batch"

    try:
        run_result = run_lua_file(
            lua_file,
            lua_bin,
            startup_timeout,
            stdin_mode="inherit" if program_mode == "interactive" else "devnull",
        )
        raw_run_output = merge_process_output(run_result["stdout"], run_result["stderr"])
        run_output = repair_mojibake(raw_run_output)
        timed_out = run_result["timed_out"]
        no_runtime_stderr = not run_result["stderr"].strip()
        if program_mode == "interactive":
            started_ok = no_runtime_stderr and (run_result["success"] or timed_out)
        else:
            started_ok = run_result["success"] and no_runtime_stderr and not timed_out

        if contains_mojibake(raw_run_output) or contains_mojibake(run_output):
            run_warning = (
                "Console output looks garbled in Windows cmd. "
                "Prefer ASCII UI text or configure UTF-8 explicitly.\n"
                f"{run_output}"
            )
        if not run_result["success"] and not timed_out:
            run_error = run_output or f"Lua process exited with code {run_result['returncode']}."
        elif program_mode == "batch" and timed_out:
            run_error = (
                "Batch Lua script did not finish during the startup timeout. "
                "If the program is intentionally interactive, keep input-driven behavior explicit."
            )
    except (FileNotFoundError, RuntimeError) as exc:
        run_error = repair_mojibake(str(exc))

    try:
        luacheck_result = check_lua_file(lua_file, luacheck_bin)
        raw_luacheck_output = merge_process_output(luacheck_result["stdout"], luacheck_result["stderr"])
        luacheck_output = repair_mojibake(raw_luacheck_output)
        if not luacheck_result["success"]:
            luacheck_error = (
                luacheck_output or f"luacheck exited with code {luacheck_result['returncode']}."
            )
        elif contains_mojibake(raw_luacheck_output) or contains_mojibake(luacheck_output):
            luacheck_warning = (
                "Luacheck output looks garbled in Windows cmd. "
                "Review console encoding if this keeps happening.\n"
                f"{luacheck_output}"
            )
    except (FileNotFoundError, RuntimeError) as exc:
        luacheck_error = repair_mojibake(str(exc))

    diagnostics = {
        "success": started_ok or (not run_error and not luacheck_error),
        "started_ok": started_ok,
        "timed_out": timed_out,
        "program_mode": program_mode,
        "run_output": run_output,
        "run_error": run_error,
        "run_warning": run_warning,
        "luacheck_output": luacheck_output,
        "luacheck_error": luacheck_error,
        "luacheck_warning": luacheck_warning,
    }
    diagnostics["failure_kind"] = classify_failure_kind(diagnostics)
    return diagnostics


def is_tooling_problem(diagnostics: dict) -> bool:
    combined = f"{diagnostics['run_error']}\n{diagnostics['luacheck_error']}".lower()
    tooling_markers = (
        "not found",
        "не является внутренней",
        "could not connect to lm studio",
        "lua interpreter",
        "luacheck exited with code 9009",
        "could not save lua file",
        "module 'luacheck.main' not found",
        "missing argument 'files'",
        "usage: luacheck",
    )
    return any(marker in combined for marker in tooling_markers) and "unexpected symbol" not in combined


def build_fix_user_prompt(diagnostics: dict, attempt: int) -> str:
    program_mode = diagnostics.get("program_mode", "batch")
    failure_kind = classify_failure_kind(diagnostics)
    run_error = diagnostics["run_error"] or "none"
    run_warning = diagnostics.get("run_warning", "") or "none"
    luacheck_error = diagnostics["luacheck_error"] or "none"
    luacheck_warning = diagnostics.get("luacheck_warning", "") or "none"
    run_output = diagnostics["run_output"] or "none"
    luacheck_output = diagnostics["luacheck_output"] or "none"
    verification_summary = diagnostics.get("verification_summary", "") or "none"
    verification_missing = diagnostics.get("verification_missing_requirements", [])
    verification_warnings = diagnostics.get("verification_warnings", [])
    extra_instruction = (
        "The previous reply format was invalid. Start immediately with Lua code on the first non-empty line."
        if failure_kind == "format"
        else "Keep the reply as a single standalone Lua file."
    )

    return (
        f"Fix attempt: {attempt}\n\n"
        "Failure kind:\n"
        f"{failure_kind}\n\n"
        "Program mode:\n"
        f"{program_mode}\n\n"
        "Runtime output:\n"
        f"{run_output}\n\n"
        "Runtime error:\n"
        f"{run_error}\n\n"
        "Runtime warning:\n"
        f"{run_warning}\n\n"
        "Luacheck output:\n"
        f"{luacheck_output}\n\n"
        "Luacheck error:\n"
        f"{luacheck_error}\n\n"
        "Luacheck warning:\n"
        f"{luacheck_warning}\n\n"
        "Prompt requirements summary:\n"
        f"{verification_summary}\n\n"
        "Missing requirements:\n"
        f"{chr(10).join(verification_missing) if verification_missing else 'none'}\n\n"
        "Warnings:\n"
        f"{chr(10).join(verification_warnings) if verification_warnings else 'none'}\n\n"
        "If the program is interactive, it may wait for user input after startup; that is acceptable. "
        "Fix the Lua code so it starts correctly in its intended mode and passes luacheck. "
        f"{extra_instruction} "
        "Return only the full corrected Lua code."
    )


def request_fixed_code(
    model: str,
    url: str,
    temperature: float,
    request_timeout: float,
    user_prompt: str,
    current_code: str,
    diagnostics: dict,
    attempt: int,
) -> str:
    failure_kind = classify_failure_kind(diagnostics)
    system_prompt = FIX_SYSTEM_PROMPT if failure_kind != "format" else STRICT_FIX_SYSTEM_PROMPT
    payload = {
        "model": model,
        "temperature": min(temperature, 0.05),
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": f"Original task:\n{user_prompt}"},
            {"role": "assistant", "content": current_code},
            {"role": "user", "content": build_fix_user_prompt(diagnostics, attempt)},
        ],
    }
    response_text = request_lua_code(url, payload, request_timeout)
    analysis = analyze_lua_response(response_text)
    if not analysis["valid"]:
        return analysis["normalized"]
    return analysis["normalized"]


def print_success(lua_file: str, attempts_used: int, diagnostics: dict) -> None:
    print("Status: OK")
    print(f"Lua file: {lua_file}")
    print(f"Attempts: {attempts_used}")
    print(f"Program mode: {diagnostics.get('program_mode', 'batch')}")
    if diagnostics.get("failure_kind") and diagnostics["failure_kind"] not in {"unknown", "none"}:
        print(f"Failure kind: {diagnostics['failure_kind']}")

    if diagnostics["run_output"]:
        print("Run result:")
        print(diagnostics["run_output"])
    else:
        print("Run result: script finished without console output.")

    if diagnostics["timed_out"]:
        print("Run status: script is active and did not fail during startup.")
    elif diagnostics["luacheck_output"]:
        print("Luacheck:")
        print(diagnostics["luacheck_output"])
    else:
        print("Luacheck: no output, check completed successfully.")

    if diagnostics.get("run_warning"):
        print("Run warning:")
        print(diagnostics["run_warning"])

    if diagnostics.get("luacheck_warning"):
        print("Luacheck warning:")
        print(diagnostics["luacheck_warning"])

    if diagnostics.get("verification_checked"):
        print("Requirements check:")
        print(f"Passed: {'yes' if diagnostics['verification_passed'] else 'no'}")
        print(f"Score: {diagnostics['verification_score']}/100")
        print(f"Summary: {diagnostics['verification_summary']}")

        if diagnostics.get("verification_missing_requirements"):
            print("Missing requirements:")
            for item in diagnostics["verification_missing_requirements"]:
                print(f"- {item}")

        if diagnostics.get("verification_warnings"):
            print("Warnings:")
            for item in diagnostics["verification_warnings"]:
                print(f"- {item}")


def print_failure(lua_file: str, attempts_used: int, diagnostics: dict) -> None:
    print("Status: ERROR", file=sys.stderr)
    print(f"Lua file: {lua_file}", file=sys.stderr)
    print(f"Attempts: {attempts_used}", file=sys.stderr)
    print(f"Program mode: {diagnostics.get('program_mode', 'batch')}", file=sys.stderr)
    if diagnostics.get("failure_kind") and diagnostics["failure_kind"] not in {"unknown", "none"}:
        print(f"Failure kind: {diagnostics['failure_kind']}", file=sys.stderr)

    if diagnostics["run_error"]:
        print("Run error:", file=sys.stderr)
        print(diagnostics["run_error"], file=sys.stderr)
    elif diagnostics.get("run_warning"):
        print("Run warning:", file=sys.stderr)
        print(diagnostics["run_warning"], file=sys.stderr)

    if diagnostics["luacheck_error"]:
        print("Luacheck error:", file=sys.stderr)
        print(diagnostics["luacheck_error"], file=sys.stderr)
    elif diagnostics.get("luacheck_warning"):
        print("Luacheck warning:", file=sys.stderr)
        print(diagnostics["luacheck_warning"], file=sys.stderr)

    if diagnostics.get("verification_checked"):
        print("Requirements check:", file=sys.stderr)
        print(
            f"Passed: {'yes' if diagnostics['verification_passed'] else 'no'}",
            file=sys.stderr,
        )
        print(f"Score: {diagnostics['verification_score']}/100", file=sys.stderr)
        print(f"Summary: {diagnostics['verification_summary']}", file=sys.stderr)

        if diagnostics.get("verification_missing_requirements"):
            print("Missing requirements:", file=sys.stderr)
            for item in diagnostics["verification_missing_requirements"]:
                print(f"- {item}", file=sys.stderr)

        if diagnostics.get("verification_warnings"):
            print("Warnings:", file=sys.stderr)
            for item in diagnostics["verification_warnings"]:
                print(f"- {item}", file=sys.stderr)


def main() -> int:
    configure_console_utf8()
    args = parse_args()
    if args.max_attempts < 1:
        print("Error: --max-attempts must be at least 1.", file=sys.stderr)
        return 1

    try:
        current_code, initialization_note = ensure_initial_code(args)
    except (OSError, RuntimeError) as exc:
        print(f"Status: ERROR\nInitialization error:\n{repair_mojibake(str(exc))}", file=sys.stderr)
        return 1

    if initialization_note:
        print("Initialization note:")
        print(initialization_note)

    last_diagnostics = {
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
    consecutive_format_failures = 0
    last_valid_lua_code = ""

    for attempt in range(1, args.max_attempts + 1):
        response_analysis = analyze_lua_response(current_code)
        if response_analysis["valid"]:
            current_code = response_analysis["normalized"]
            last_valid_lua_code = current_code
            consecutive_format_failures = 0
            save_lua_code(args.lua_file, current_code)
            diagnostics = run_diagnostics(
                args.lua_file,
                args.lua_bin,
                args.luacheck_bin,
                args.startup_timeout,
            )
        else:
            diagnostics = {
                "success": False,
                "started_ok": False,
                "timed_out": False,
                "failure_kind": "format",
                "program_mode": infer_program_mode(response_analysis["normalized"]),
                "run_output": response_analysis["excerpt"],
                "run_error": (
                    "Model returned text that is not a standalone Lua file. "
                    f"{response_analysis['reason']}"
                ).strip(),
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
            consecutive_format_failures += 1
        last_diagnostics = diagnostics

        if is_tooling_problem(diagnostics):
            print_failure(args.lua_file, attempt, diagnostics)
            return 1

        if diagnostics["success"] and not args.skip_verification:
            verify_model = args.verify_model or args.model
            try:
                verification = verify_prompt_requirements(
                    prompt=args.prompt,
                    solution_content=current_code,
                    model=verify_model,
                    url=args.url,
                    timeout_seconds=args.request_timeout,
                    extra_context=(
                        f"Runtime output:\n{diagnostics['run_output'] or 'none'}\n\n"
                        f"Luacheck output:\n{diagnostics['luacheck_output'] or 'none'}"
                    ),
                )
            except RuntimeError as exc:
                diagnostics["verification_checked"] = True
                diagnostics["verification_passed"] = False
                diagnostics["verification_score"] = 0
                diagnostics["verification_summary"] = repair_mojibake(str(exc))
                diagnostics["verification_missing_requirements"] = []
                diagnostics["verification_warnings"] = []
                print_failure(args.lua_file, attempt, diagnostics)
                return 1

            diagnostics["verification_checked"] = True
            diagnostics["verification_passed"] = verification["passed"]
            diagnostics["verification_score"] = verification["score"]
            diagnostics["verification_summary"] = verification["summary"]
            diagnostics["verification_missing_requirements"] = verification["missing_requirements"]
            diagnostics["verification_warnings"] = verification["warnings"]
            last_diagnostics = diagnostics

            if verification["passed"]:
                print_success(args.lua_file, attempt, diagnostics)
                return 0
        elif diagnostics["success"]:
            print_success(args.lua_file, attempt, diagnostics)
            return 0

        if attempt == args.max_attempts:
            break

        try:
            repair_source_code = last_valid_lua_code or response_analysis.get("normalized", current_code) or current_code
            if diagnostics.get("failure_kind") == "format" and not last_valid_lua_code:
                strict_reason = diagnostics["run_error"]
                if consecutive_format_failures > 1:
                    strict_reason = (
                        f"{strict_reason}\n\n"
                        "The model already failed a previous formatting attempt. "
                        "Return raw Lua source immediately."
                    )
                payload = build_payload(
                    model=args.model,
                    user_prompt=(
                        f"{args.prompt}\n\n"
                        "Return only the full Lua file.\n"
                        f"Previous model response format issue to avoid:\n{strict_reason}"
                    ),
                    system_prompt=build_strict_system_prompt(DEFAULT_SYSTEM_PROMPT),
                    temperature=min(args.temperature, 0.05),
                )
                current_code = normalize_lua_code(request_lua_code(args.url, payload, args.request_timeout))
                if not current_code:
                    raise RuntimeError("LM Studio returned empty Lua code.")
                continue
            current_code = request_fixed_code(
                args.model,
                args.url,
                args.temperature,
                args.request_timeout,
                args.prompt,
                repair_source_code,
                diagnostics,
                attempt,
            )
        except RuntimeError as exc:
            last_diagnostics["run_error"] = repair_mojibake(str(exc))
            print_failure(args.lua_file, attempt, last_diagnostics)
            return 1

    print_failure(args.lua_file, args.max_attempts, last_diagnostics)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
