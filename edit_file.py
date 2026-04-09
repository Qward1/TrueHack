#!/usr/bin/env python3
import argparse
import os
import re
import shutil
import sys

from console_utils import configure_console_utf8
from generate import DEFAULT_MODEL, DEFAULT_REQUEST_TIMEOUT, DEFAULT_URL, request_lua_code
from prompt_verifier import verify_prompt_requirements


DEFAULT_TEMPERATURE = 0.1
DEFAULT_SYSTEM_PROMPT = (
    "You update an existing file according to the user's request. "
    "Return the full updated file content only, with no markdown fences, comments outside the file, "
    "or explanations. Preserve working code and unchanged sections unless the request requires edits."
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Edit an existing file through LM Studio using a natural-language change request."
    )
    parser.add_argument("file_path", help="Path to the file that should be changed.")
    parser.add_argument("instruction", help="What should be added or changed in the file.")
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
        help="Sampling temperature.",
    )
    parser.add_argument(
        "--request-timeout",
        type=float,
        default=DEFAULT_REQUEST_TIMEOUT,
        help="Seconds to wait for the LM Studio response.",
    )
    parser.add_argument(
        "--system-prompt",
        default=DEFAULT_SYSTEM_PROMPT,
        help="System prompt sent to the model.",
    )
    parser.add_argument(
        "--backup",
        action="store_true",
        help="Create a .bak copy before overwriting the file.",
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


def read_text_file(path: str) -> str:
    with open(path, "r", encoding="utf-8") as file:
        return file.read()


def write_text_file(path: str, content: str) -> None:
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)

    with open(path, "w", encoding="utf-8", newline="\n") as file:
        file.write(content)
        if content and not content.endswith("\n"):
            file.write("\n")


def normalize_model_output(text: str) -> str:
    cleaned = text.strip()
    fenced = re.search(r"```(?:[\w.+-]+)?\s*(.*?)```", cleaned, flags=re.DOTALL)
    if fenced:
        cleaned = fenced.group(1).strip()
    return cleaned.replace("\r\n", "\n").strip()


def build_edit_payload(
    model: str,
    system_prompt: str,
    instruction: str,
    file_path: str,
    original_content: str,
    temperature: float,
) -> dict:
    return {
        "model": model,
        "temperature": temperature,
        "messages": [
            {"role": "system", "content": system_prompt},
            {
                "role": "user",
                "content": (
                    f"File path:\n{file_path}\n\n"
                    f"Requested changes:\n{instruction}\n\n"
                    "Update the file and return the full new file content only."
                ),
            },
            {"role": "assistant", "content": original_content},
            {
                "role": "user",
                "content": (
                    "Apply the requested changes to the file above. "
                    "Return the complete updated file content only."
                ),
            },
        ],
    }


def main() -> int:
    configure_console_utf8()
    args = parse_args()

    if not os.path.exists(args.file_path):
        print(f"Error: file not found: {args.file_path}", file=sys.stderr)
        return 1

    try:
        original_content = read_text_file(args.file_path)
    except OSError as exc:
        print(f"Error: could not read '{args.file_path}': {exc}", file=sys.stderr)
        return 1

    payload = build_edit_payload(
        model=args.model,
        system_prompt=args.system_prompt,
        instruction=args.instruction,
        file_path=args.file_path,
        original_content=original_content,
        temperature=args.temperature,
    )

    try:
        updated_content = normalize_model_output(
            request_lua_code(args.url, payload, args.request_timeout)
        )
    except RuntimeError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    if not updated_content:
        print("Error: LM Studio returned empty file content.", file=sys.stderr)
        return 1

    if not args.skip_verification:
        verify_model = args.verify_model or args.model
        try:
            verification = verify_prompt_requirements(
                prompt=args.instruction,
                solution_content=updated_content,
                model=verify_model,
                url=args.url,
                timeout_seconds=args.request_timeout,
                extra_context=(
                    f"Target file path:\n{args.file_path}\n\n"
                    f"Original file content:\n{original_content}"
                ),
            )
        except RuntimeError as exc:
            print(f"Error: verification failed: {exc}", file=sys.stderr)
            return 1

        if not verification["passed"]:
            print("Status: ERROR", file=sys.stderr)
            print("Requirements check failed.", file=sys.stderr)
            print(f"Score: {verification['score']}/100", file=sys.stderr)
            print(f"Summary: {verification['summary']}", file=sys.stderr)
            if verification["missing_requirements"]:
                print("Missing requirements:", file=sys.stderr)
                for item in verification["missing_requirements"]:
                    print(f"- {item}", file=sys.stderr)
            if verification["warnings"]:
                print("Warnings:", file=sys.stderr)
                for item in verification["warnings"]:
                    print(f"- {item}", file=sys.stderr)
            return 1

    if args.backup:
        backup_path = f"{args.file_path}.bak"
        try:
            shutil.copyfile(args.file_path, backup_path)
            print(f"Backup: {backup_path}")
        except OSError as exc:
            print(f"Error: could not create backup '{backup_path}': {exc}", file=sys.stderr)
            return 1

    try:
        write_text_file(args.file_path, updated_content)
    except OSError as exc:
        print(f"Error: could not write '{args.file_path}': {exc}", file=sys.stderr)
        return 1

    print("Status: OK")
    print(f"File updated: {args.file_path}")
    if not args.skip_verification:
        print("Requirements check:")
        print(f"Passed: {'yes' if verification['passed'] else 'no'}")
        print(f"Score: {verification['score']}/100")
        print(f"Summary: {verification['summary']}")
        if verification["warnings"]:
            print("Warnings:")
            for item in verification["warnings"]:
                print(f"- {item}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
