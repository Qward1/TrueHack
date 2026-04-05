import importlib
from importlib import util as importlib_util
import glob
import os
import shutil
import subprocess
import tempfile
import textwrap
import time
from dataclasses import dataclass, field
from pathlib import Path
from collections import deque
from typing import Any, Dict


@dataclass
class LuaToolchain:
    execution_timeout_seconds: int = 15
    backend: str = "auto"
    lua_path: str | None = field(default=None)
    luajit_path: str | None = field(default=None)
    linter: str | None = field(default=None)

    def __post_init__(self) -> None:
        if self.lua_path is None:
            self.lua_path = self._resolve_binary(
                ["lua", "lua.exe"],
                windows_wsl_candidates=[
                    "/mnt/c/Users/*/AppData/Local/Programs/Lua/bin/lua.exe",
                ],
                windows_native_candidates=[
                    os.path.join(os.environ.get("LOCALAPPDATA", ""), "Programs", "Lua", "bin", "lua.exe"),
                ],
            )
        if self.luajit_path is None:
            self.luajit_path = self._resolve_binary(
                ["luajit", "luajit.exe"],
                windows_wsl_candidates=[
                    "/mnt/c/Users/*/AppData/Local/Programs/LuaJIT/luajit.exe",
                    "/mnt/c/Users/*/scoop/apps/luajit/current/luajit.exe",
                ],
                windows_native_candidates=[
                    os.path.join(os.environ.get("LOCALAPPDATA", ""), "Programs", "LuaJIT", "luajit.exe"),
                ],
            )
        if self.linter is None:
            self.linter = self._resolve_binary(
                ["luacheck", "luacheck.exe"],
                windows_wsl_candidates=[
                    "/mnt/c/Users/*/AppData/Roaming/luarocks/bin/luacheck",
                    "/mnt/c/Users/*/AppData/Roaming/luarocks/bin/luacheck.bat",
                    "/mnt/c/Users/*/AppData/Roaming/luarocks/bin/luacheck.cmd",
                ],
                windows_native_candidates=[
                    os.path.join(os.environ.get("APPDATA", ""), "luarocks", "bin", "luacheck"),
                    os.path.join(os.environ.get("APPDATA", ""), "luarocks", "bin", "luacheck.bat"),
                    os.path.join(os.environ.get("APPDATA", ""), "luarocks", "bin", "luacheck.cmd"),
                ],
            )

        allowed_backends = {"auto", "lua", "luajit", "lupa"}
        if self.backend not in allowed_backends:
            raise ValueError(
                f"Unsupported Lua backend '{self.backend}'. Expected one of: {sorted(allowed_backends)}."
            )

    def describe_environment(self) -> Dict[str, Any]:
        selected = self.resolve_backend()
        return {
            "backend_preference": self.backend,
            "selected_backend": selected,
            "lua_path": self.lua_path,
            "luajit_path": self.luajit_path,
            "linter": self.linter,
            "lupa_available": self._lupa_is_available(),
            "ready": selected is not None,
        }

    def run_script(self, code: str, *, stdin_data: str | None = None) -> Dict[str, Any]:
        with tempfile.TemporaryDirectory(prefix="lua-agent-") as temp_dir:
            script_path = Path(temp_dir) / "generated.lua"
            script_path.write_text(code, encoding="utf-8")

            lint_result = self._run_linter(script_path)
            runtime_result = self._run_chunk(
                script_path,
                code,
                cwd=temp_dir,
                stdin_data=stdin_data,
            )

            return {
                "success": runtime_result["success"],
                "stdout": runtime_result["stdout"],
                "stderr": runtime_result["stderr"],
                "exit_code": runtime_result.get("exit_code"),
                "command": runtime_result["command"],
                "runtime_seconds": runtime_result["runtime_seconds"],
                "stdin_data": stdin_data,
                "lint": lint_result,
                "environment": self.describe_environment(),
            }

    def run_smoke_check(self, code: str) -> Dict[str, Any]:
        smoke_script = (
            "assert(target ~= nil, 'target must be available after loading target.lua')\n"
            "print('SMOKE_PASSED')"
        )
        result = self.run_tests(code, smoke_script)
        return {
            "success": result["success"],
            "stdout": result["stdout"],
            "stderr": result["stderr"],
            "exit_code": result.get("exit_code"),
            "command": result["command"],
            "runtime_seconds": result["runtime_seconds"],
            "lint": result["lint"],
            "environment": result["environment"],
            "smoke_mode": True,
        }

    def run_tests(self, code: str, test_script: str) -> Dict[str, Any]:
        with tempfile.TemporaryDirectory(prefix="lua-agent-tests-") as temp_dir:
            target_path = Path(temp_dir) / "target.lua"
            harness_path = Path(temp_dir) / "test_runner.lua"
            harness_code = self._build_test_harness(test_script)

            target_path.write_text(code, encoding="utf-8")
            harness_path.write_text(
                harness_code,
                encoding="utf-8",
            )

            lint_result = self._run_linter(target_path)
            runtime_result = self._run_test_chunk(
                harness_path=harness_path,
                code=code,
                test_script=test_script,
                cwd=temp_dir,
            )

            return {
                "success": runtime_result["success"],
                "stdout": runtime_result["stdout"],
                "stderr": runtime_result["stderr"],
                "exit_code": runtime_result.get("exit_code"),
                "command": runtime_result["command"],
                "runtime_seconds": runtime_result["runtime_seconds"],
                "lint": lint_result,
                "test_script": test_script,
                "environment": self.describe_environment(),
            }

    def _run_linter(self, script_path: Path) -> Dict[str, Any]:
        if not self.linter:
            return {
                "available": False,
                "success": None,
                "stdout": "",
                "stderr": "luacheck is not installed",
                "command": None,
            }

        lint_command = self._build_linter_command(script_path)

        try:
            completed = subprocess.run(
                lint_command,
                capture_output=True,
                text=True,
                cwd=str(script_path.parent),
                timeout=self.execution_timeout_seconds,
                check=False,
            )
        except subprocess.TimeoutExpired:
            return {
                "available": True,
                "success": False,
                "stdout": "",
                "stderr": "luacheck timed out",
                "command": " ".join(lint_command),
            }

        return {
            "available": True,
            "success": completed.returncode == 0,
            "stdout": completed.stdout.strip(),
            "stderr": completed.stderr.strip(),
            "command": " ".join(lint_command),
        }

    def _run_chunk(
        self,
        script_path: Path,
        code: str,
        *,
        cwd: str,
        stdin_data: str | None = None,
    ) -> Dict[str, Any]:
        selected_backend = self.resolve_backend()
        if selected_backend in {"lua", "luajit"}:
            interpreter = self.lua_path if selected_backend == "lua" else self.luajit_path
            return self._run_with_cli(
                script_path,
                interpreter=interpreter,
                cwd=cwd,
                stdin_data=stdin_data,
            )
        if selected_backend == "lupa":
            return self._run_with_lupa(code, stdin_data=stdin_data)
        return {
            "success": False,
            "stdout": "",
            "stderr": (
                "Lua runtime is not available. "
                "Install lua, luajit or the Python package 'lupa', "
                "or configure LUA_BACKEND/LUA_PATH/LUAJIT_PATH."
            ),
            "exit_code": None,
            "command": None,
            "runtime_seconds": 0.0,
            "backend": None,
        }

    def _run_test_chunk(
        self,
        *,
        harness_path: Path,
        code: str,
        test_script: str,
        cwd: str,
    ) -> Dict[str, Any]:
        selected_backend = self.resolve_backend()
        if selected_backend in {"lua", "luajit"}:
            interpreter = self.lua_path if selected_backend == "lua" else self.luajit_path
            return self._run_with_cli(harness_path, interpreter=interpreter, cwd=cwd)
        if selected_backend == "lupa":
            return self._run_tests_with_lupa(code, test_script)
        return {
            "success": False,
            "stdout": "",
            "stderr": (
                "Lua runtime is not available. "
                "Install lua, luajit or the Python package 'lupa', "
                "or configure LUA_BACKEND/LUA_PATH/LUAJIT_PATH."
            ),
            "exit_code": None,
            "command": None,
            "runtime_seconds": 0.0,
            "backend": None,
        }

    def _run_with_cli(
        self,
        script_path: Path,
        *,
        interpreter: str | None,
        cwd: str,
        stdin_data: str | None = None,
    ) -> Dict[str, Any]:
        if not interpreter:
            return {
                "success": False,
                "stdout": "",
                "stderr": "Configured CLI Lua backend is missing an interpreter path.",
                "exit_code": None,
                "command": None,
                "runtime_seconds": 0.0,
                "backend": None,
            }

        command = [interpreter, self._adapt_path_for_command(script_path, executable=interpreter)]
        started_at = time.monotonic()
        try:
            completed = subprocess.run(
                command,
                capture_output=True,
                text=True,
                input=stdin_data,
                cwd=cwd,
                timeout=self.execution_timeout_seconds,
                check=False,
            )
        except subprocess.TimeoutExpired:
            runtime_seconds = time.monotonic() - started_at
            return {
                "success": False,
                "stdout": "",
                "stderr": f"Lua process timed out after {self.execution_timeout_seconds} seconds",
                "exit_code": None,
                "command": " ".join(command),
                "runtime_seconds": round(runtime_seconds, 4),
                "backend": self._backend_name_for_interpreter(interpreter),
            }
        runtime_seconds = time.monotonic() - started_at

        return {
            "success": completed.returncode == 0,
            "stdout": completed.stdout.strip(),
            "stderr": completed.stderr.strip(),
            "exit_code": completed.returncode,
            "command": " ".join(command),
            "runtime_seconds": round(runtime_seconds, 4),
            "backend": self._backend_name_for_interpreter(interpreter),
        }

    def _run_with_lupa(self, code: str, *, stdin_data: str | None = None) -> Dict[str, Any]:
        started_at = time.monotonic()
        try:
            lupa = importlib.import_module("lupa")
            lua_runtime = lupa.LuaRuntime(unpack_returned_tuples=True)
            output_lines: list[str] = []

            def capture_print(*parts: Any) -> None:
                output_lines.append(" ".join(str(part) for part in parts))

            lua_runtime.globals()["print"] = capture_print
            self._configure_lupa_io(lua_runtime, stdin_data)
            lua_runtime.execute(code)
            runtime_seconds = time.monotonic() - started_at
            return {
                "success": True,
                "stdout": "\n".join(output_lines).strip(),
                "stderr": "",
                "exit_code": 0,
                "command": "lupa.execute",
                "runtime_seconds": round(runtime_seconds, 4),
                "backend": "lupa",
            }
        except Exception as exc:  # pragma: no cover - depends on local runtime
            runtime_seconds = time.monotonic() - started_at
            return {
                "success": False,
                "stdout": "",
                "stderr": str(exc),
                "exit_code": 1,
                "command": "lupa.execute",
                "runtime_seconds": round(runtime_seconds, 4),
                "backend": "lupa",
            }

    def _run_tests_with_lupa(self, code: str, test_script: str) -> Dict[str, Any]:
        started_at = time.monotonic()
        try:
            lupa = importlib.import_module("lupa")
            lua_runtime = lupa.LuaRuntime(unpack_returned_tuples=True)
            output_lines: list[str] = []

            def capture_print(*parts: Any) -> None:
                output_lines.append(" ".join(str(part) for part in parts))

            lua_runtime.globals()["print"] = capture_print
            self._configure_lupa_io(lua_runtime, None)
            loader = lua_runtime.eval(
                "function(source, mode) "
                "local chunk, load_error = load(source) "
                "if not chunk then error(load_error) end "
                "return chunk(mode) "
                "end"
            )
            exports = loader(code, "__test__")
            lua_runtime.globals()["target"] = exports if exports is not None else lua_runtime.globals()
            lua_runtime.execute(test_script)
            runtime_seconds = time.monotonic() - started_at
            return {
                "success": True,
                "stdout": "\n".join(output_lines).strip(),
                "stderr": "",
                "exit_code": 0,
                "command": "lupa.execute(test_script)",
                "runtime_seconds": round(runtime_seconds, 4),
                "backend": "lupa",
            }
        except Exception as exc:  # pragma: no cover - depends on local runtime
            runtime_seconds = time.monotonic() - started_at
            return {
                "success": False,
                "stdout": "",
                "stderr": str(exc),
                "exit_code": 1,
                "command": "lupa.execute(test_script)",
                "runtime_seconds": round(runtime_seconds, 4),
                "backend": "lupa",
            }

    def _lupa_is_available(self) -> bool:
        return importlib_util.find_spec("lupa") is not None

    def resolve_backend(self) -> str | None:
        if self.backend == "lua":
            return "lua" if self.lua_path else None
        if self.backend == "luajit":
            return "luajit" if self.luajit_path else None
        if self.backend == "lupa":
            return "lupa" if self._lupa_is_available() else None
        if self.lua_path:
            return "lua"
        if self.luajit_path:
            return "luajit"
        if self._lupa_is_available():
            return "lupa"
        return None

    @staticmethod
    def _resolve_binary(
        candidates: list[str],
        *,
        windows_wsl_candidates: list[str],
        windows_native_candidates: list[str],
    ) -> str | None:
        for candidate in candidates:
            resolved = shutil.which(candidate)
            if resolved:
                return resolved

        for candidate in windows_native_candidates:
            if candidate and os.path.exists(candidate):
                return candidate

        for pattern in windows_wsl_candidates:
            matches = sorted(glob.glob(pattern))
            if matches:
                return matches[0]
        return None

    def _backend_name_for_interpreter(self, interpreter: str) -> str:
        if self.luajit_path and Path(interpreter) == Path(self.luajit_path):
            return "luajit"
        return "lua"

    def _build_linter_command(self, script_path: Path) -> list[str]:
        linter_executable = str(self.linter)

        if self._looks_like_lua_launcher(linter_executable):
            lua_driver = self.lua_path or self.luajit_path
            if not lua_driver:
                lint_target = self._adapt_path_for_command(script_path, executable=self.linter)
                return [linter_executable, lint_target, "--formatter", "plain"]

            if self._is_windows_executable(lua_driver):
                return [
                    lua_driver,
                    "-e",
                    self._build_windows_luarocks_setup_code(),
                    self._adapt_path_for_command(linter_executable, executable=lua_driver),
                    self._adapt_path_for_command(script_path, executable=lua_driver),
                    "--formatter",
                    "plain",
                ]

            return [
                lua_driver,
                self._adapt_path_for_command(linter_executable, executable=lua_driver),
                self._adapt_path_for_command(script_path, executable=lua_driver),
                "--formatter",
                "plain",
            ]

        lint_target = self._adapt_path_for_command(script_path, executable=self.linter)
        return [linter_executable, lint_target, "--formatter", "plain"]

    def _adapt_path_for_command(
        self,
        path: str | os.PathLike[str],
        *,
        executable: str | None,
    ) -> str:
        path_text = str(path)
        if executable and self._is_windows_executable(executable):
            return self._to_windows_path(path_text)
        return path_text

    @staticmethod
    def _is_windows_executable(path: str) -> bool:
        suffix = Path(path).suffix.lower()
        return suffix in {".exe", ".bat", ".cmd"}

    @staticmethod
    def _looks_like_lua_launcher(path: str) -> bool:
        suffix = Path(path).suffix.lower()
        return suffix == "" or suffix == ".lua"

    @staticmethod
    def _to_windows_path(path: str) -> str:
        if os.name == "nt":
            return os.path.normpath(path)

        if LuaToolchain._looks_like_windows_path(path):
            return path

        completed = subprocess.run(
            ["wslpath", "-w", path],
            capture_output=True,
            text=True,
            check=False,
        )
        if completed.returncode == 0:
            return completed.stdout.strip()
        return path

    @staticmethod
    def _looks_like_windows_path(path: str) -> bool:
        normalized = path.replace("/", "\\")
        return len(normalized) >= 3 and normalized[1] == ":" and normalized[2] == "\\"

    def _build_windows_luarocks_setup_code(self) -> str:
        linter_executable = str(self.linter) if self.linter else ""
        tree_root = Path(linter_executable).resolve().parent.parent
        share_root = tree_root / "share" / "lua"
        lib_root = tree_root / "lib" / "lua"

        version_directories = sorted(
            entry.name for entry in share_root.iterdir() if entry.is_dir()
        )
        lua_version = version_directories[-1]

        lua_path_entries = [
            self._to_windows_path(str(share_root / lua_version / "?.lua")),
            self._to_windows_path(str(share_root / lua_version / "?" / "init.lua")),
        ]
        lua_cpath_entries = [
            self._to_windows_path(str(lib_root / lua_version / "?.dll")),
        ]

        lua_path = ";".join(lua_path_entries) + ";"
        lua_cpath = ";".join(lua_cpath_entries) + ";"
        return (
            f"package.path=[[{lua_path}]] .. package.path; "
            f"package.cpath=[[{lua_cpath}]] .. package.cpath"
        )

    @staticmethod
    def _configure_lupa_io(lua_runtime: Any, stdin_data: str | None) -> None:
        if stdin_data is None:
            return

        queue = deque(stdin_data.splitlines())
        lua_io = lua_runtime.globals().io

        def capture_read(fmt: str | None = None) -> Any:
            if not queue:
                return None

            raw_value = queue.popleft()
            normalized_format = fmt or "*l"
            if normalized_format in {"*n", "n"}:
                try:
                    return int(raw_value)
                except ValueError:
                    try:
                        return float(raw_value)
                    except ValueError:
                        return None
            return raw_value

        lua_io.read = capture_read

    @staticmethod
    def _build_test_harness(test_script: str) -> str:
        return textwrap.dedent(
            f"""
            local function load_target()
                local chunk, load_error = loadfile("target.lua")
                if not chunk then
                    error(load_error)
                end

                local exports = chunk("__test__")
                if type(exports) == "table" then
                    return exports
                end

                return _G
            end

            local target = load_target()

            {test_script}
            """
        ).strip() + "\n"
