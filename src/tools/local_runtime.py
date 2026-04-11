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
DEFAULT_LUACHECK_BIN = "luacheck"
EXECUTABLE_EXTENSIONS = {".exe", ".bat", ".cmd", ".com"}
SHELL_WRAPPER_EXTENSIONS = {".bat", ".cmd"}


class LuaCheckResult(TypedDict):
    success: bool
    returncode: int
    stdout: str
    stderr: str


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


def build_luarocks_setup_code(tree_root: str) -> str | None:
    """Build `package.path`/`package.cpath` bootstrap code for LuaRocks wrappers."""
    share_root = os.path.join(tree_root, "share", "lua")
    lib_root = os.path.join(tree_root, "lib", "lua")

    versions: list[str] = []
    if os.path.isdir(share_root) and os.path.isdir(lib_root):
        share_versions = {
            entry for entry in os.listdir(share_root)
            if os.path.isdir(os.path.join(share_root, entry))
        }
        lib_versions = {
            entry for entry in os.listdir(lib_root)
            if os.path.isdir(os.path.join(lib_root, entry))
        }
        versions = sorted(share_versions & lib_versions, reverse=True)

    if not versions:
        return None

    version = versions[0]
    share_dir = to_cmd_path(os.path.join(share_root, version))
    lib_dir = to_cmd_path(os.path.join(lib_root, version))
    return (
        f"package.path=[[{share_dir}\\?.lua;{share_dir}\\?\\init.lua;]]..package.path;"
        f"package.cpath=[[{lib_dir}\\?.dll;]]..package.cpath"
    )


def find_luacheck_fallback() -> str | None:
    """Locate luacheck installed under the current Windows user profile."""
    candidates: list[str] = []
    appdata = os.environ.get("APPDATA")
    userprofile = os.environ.get("USERPROFILE")

    if appdata:
        candidates.extend(
            [
                os.path.join(appdata, "luarocks", "bin", "luacheck"),
                os.path.join(appdata, "luarocks", "bin", "luacheck.bat"),
                os.path.join(appdata, "luarocks", "bin", "luacheck.cmd"),
                os.path.join(appdata, "luarocks", "bin", "luacheck.exe"),
            ]
        )

    if userprofile:
        roaming = os.path.join(userprofile, "AppData", "Roaming", "luarocks", "bin")
        candidates.extend(
            [
                os.path.join(roaming, "luacheck"),
                os.path.join(roaming, "luacheck.bat"),
                os.path.join(roaming, "luacheck.cmd"),
                os.path.join(roaming, "luacheck.exe"),
            ]
        )

    for candidate in candidates:
        if os.path.exists(candidate):
            return candidate
    return None


def resolve_luacheck_command(luacheck_bin: str, lua_file: str) -> list[str]:
    """Resolve the right luacheck invocation command for the current environment."""
    resolved_path = luacheck_bin if os.path.exists(luacheck_bin) else shutil.which(luacheck_bin)
    if not resolved_path and luacheck_bin == DEFAULT_LUACHECK_BIN:
        resolved_path = find_luacheck_fallback()
    cmd_lua_file = to_cmd_path(lua_file)

    if not resolved_path:
        return [luacheck_bin, cmd_lua_file]

    extension = os.path.splitext(resolved_path)[1].lower()
    cmd_resolved_path = to_cmd_path(resolved_path)
    if extension in SHELL_WRAPPER_EXTENSIONS:
        tree_root = os.path.dirname(os.path.dirname(resolved_path))
        setup_code = build_luarocks_setup_code(tree_root)
        runner_code = (
            "for i=#arg,0,-1 do arg[i+1]=arg[i] end; "
            "arg[0]='luacheck'; require('luacheck.main')"
        )
        if setup_code:
            return [DEFAULT_LUA_BIN, "-e", f"{setup_code};{runner_code}", "--", cmd_lua_file]
        return [DEFAULT_LUA_BIN, "-e", runner_code, "--", cmd_lua_file]

    if extension in EXECUTABLE_EXTENSIONS:
        return [cmd_resolved_path, cmd_lua_file]

    tree_root = os.path.dirname(os.path.dirname(resolved_path))
    setup_code = build_luarocks_setup_code(tree_root)
    if setup_code:
        return [DEFAULT_LUA_BIN, "-e", setup_code, cmd_resolved_path, cmd_lua_file]
    return [DEFAULT_LUA_BIN, cmd_resolved_path, cmd_lua_file]


def check_lua_file(lua_file: str, luacheck_bin: str = DEFAULT_LUACHECK_BIN) -> LuaCheckResult:
    """Run luacheck for the target Lua file."""
    if not os.path.exists(lua_file):
        raise FileNotFoundError(f"Lua file not found: {lua_file}")

    command = resolve_luacheck_command(luacheck_bin, lua_file)
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=False,
            check=False,
        )
    except FileNotFoundError as exc:
        raise RuntimeError(
            f"Luacheck command '{command[0]}' not found or unavailable in the current environment."
        ) from exc

    return {
        "success": completed.returncode == 0,
        "returncode": completed.returncode,
        "stdout": decode_process_bytes(completed.stdout),
        "stderr": decode_process_bytes(completed.stderr),
    }


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


def run_lua_file_with_input(
    lua_file: str,
    stdin_text: str,
    lua_bin: str = DEFAULT_LUA_BIN,
    timeout_seconds: float | None = None,
) -> LuaRunResult:
    """Run a Lua file with predefined stdin text and capture its output."""
    if not os.path.exists(lua_file):
        raise FileNotFoundError(f"Lua file not found: {lua_file}")

    command = [lua_bin, to_cmd_path(lua_file)]
    input_bytes = (stdin_text or "").encode("utf-8")

    try:
        try:
            completed = subprocess.run(
                command,
                input=input_bytes,
                capture_output=True,
                text=False,
                timeout=timeout_seconds,
                check=False,
            )
            timed_out = False
            returncode = completed.returncode
            stdout = completed.stdout
            stderr = completed.stderr
        except subprocess.TimeoutExpired as exc:
            timed_out = True
            returncode = 0
            stdout = exc.stdout or b""
            stderr = exc.stderr or b""
    except FileNotFoundError as exc:
        raise RuntimeError(f"Lua interpreter '{lua_bin}' not found or unavailable.") from exc

    return {
        "success": timed_out or returncode == 0,
        "timed_out": timed_out,
        "returncode": returncode,
        "stdout": decode_process_bytes(stdout),
        "stderr": decode_process_bytes(stderr),
    }
