"""Local Lua execution helpers plus optional luacheck wrappers."""

from __future__ import annotations

import locale
import os
import shutil
import subprocess
import tempfile
import time
from typing import TypedDict


DEFAULT_LUA_BIN = os.getenv("LUA_BIN", "lua55")


class LuaRunResult(TypedDict):
    success: bool
    timed_out: bool
    returncode: int
    stdout: str
    stderr: str


def decode_process_bytes(data: bytes) -> str:
    """Decode subprocess output with Windows-friendly fallbacks."""
    if not data:
        return ""

    encodings: list[str] = []
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
    """Convert a path for Windows command invocations when WSL path tools exist."""
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
    """Run a Lua file and capture its output."""
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
