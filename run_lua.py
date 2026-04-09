#!/usr/bin/env python3
import argparse
import locale
import os
import shutil
import subprocess
import sys
import tempfile
import time
from typing import TypedDict

from console_utils import configure_console_utf8


DEFAULT_LUA_FILE = "generated.lua"
DEFAULT_LUA_BIN = "lua"
DEFAULT_TIMEOUT_SECONDS = 3.0


class LuaRunResult(TypedDict):
    success: bool
    timed_out: bool
    returncode: int
    stdout: str
    stderr: str


def decode_process_bytes(data: bytes) -> str:
    if not data:
        return ""

    encodings = []
    encoding_candidates = ["utf-8"]
    if os.name == "nt":
        encoding_candidates.extend(["oem", "cp866", locale.getpreferredencoding(False), "cp1251"])
    else:
        encoding_candidates.extend([locale.getpreferredencoding(False), "cp866", "cp1251"])

    for encoding in encoding_candidates:
        if encoding and encoding not in encodings:
            encodings.append(encoding)

    for encoding in encodings:
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue

    return data.decode("utf-8", errors="replace")


def to_cmd_path(path: str) -> str:
    absolute_path = os.path.abspath(path)
    wslpath_bin = shutil.which("wslpath")
    if not wslpath_bin:
        return absolute_path

    converted = subprocess.run(
        [wslpath_bin, "-w", absolute_path],
        capture_output=True,
        text=True,
        check=False,
    )
    if converted.returncode == 0 and converted.stdout.strip():
        return converted.stdout.strip()

    return absolute_path


def run_lua_file(
    lua_file: str,
    lua_bin: str = DEFAULT_LUA_BIN,
    timeout_seconds: float | None = None,
    stdin_mode: str = "devnull",
) -> LuaRunResult:
    if not os.path.exists(lua_file):
        raise FileNotFoundError(f"Lua file not found: {lua_file}")

    command = [lua_bin, to_cmd_path(lua_file)]
    stdin_stream = None if stdin_mode == "inherit" else subprocess.DEVNULL

    try:
        with tempfile.TemporaryFile() as stdout_file, tempfile.TemporaryFile() as stderr_file:
            process = subprocess.Popen(
                command,
                stdin=stdin_stream,
                stdout=stdout_file,
                stderr=stderr_file,
                text=False,
            )
            timed_out = False

            if timeout_seconds is None:
                process.wait()
            else:
                deadline = time.monotonic() + timeout_seconds
                while process.poll() is None and time.monotonic() < deadline:
                    time.sleep(0.05)

                if process.poll() is None:
                    timed_out = True
                    process.kill()
                    process.wait()

            stdout_file.seek(0)
            stderr_file.seek(0)
            stdout = stdout_file.read()
            stderr = stderr_file.read()
    except FileNotFoundError as exc:
        raise RuntimeError(f"Lua interpreter '{lua_bin}' not found or unavailable.") from exc

    return {
        "success": timed_out or process.returncode == 0,
        "timed_out": timed_out,
        "returncode": 0 if timed_out else (process.returncode or 0),
        "stdout": decode_process_bytes(stdout),
        "stderr": decode_process_bytes(stderr),
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run a Lua file and capture its console output or errors."
    )
    parser.add_argument(
        "lua_file",
        nargs="?",
        default=DEFAULT_LUA_FILE,
        help="Path to the Lua file to run.",
    )
    parser.add_argument(
        "--lua-bin",
        default=DEFAULT_LUA_BIN,
        help="Lua interpreter executable name or path.",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=DEFAULT_TIMEOUT_SECONDS,
        help="Seconds to wait before treating a running Lua process as successfully started.",
    )
    return parser.parse_args()


def main() -> int:
    configure_console_utf8()
    args = parse_args()

    try:
        result = run_lua_file(args.lua_file, args.lua_bin, args.timeout)
    except (FileNotFoundError, RuntimeError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    if result["stdout"]:
        print(result["stdout"], end="")

    if result["stderr"]:
        print(result["stderr"], end="", file=sys.stderr)

    if result["timed_out"]:
        print(
            f"\nProcess is still running after {args.timeout} seconds; treating startup as successful."
        )

    return result["returncode"]


if __name__ == "__main__":
    raise SystemExit(main())
