import os
from dataclasses import dataclass
from typing import Dict


AGENT_ROLES = (
    "parse_task",
    "plan_task",
    "generate_code",
    "execute_code",
    "test_code",
    "repair_code",
    "finalize_artifact",
)


@dataclass(frozen=True)
class ModelConfig:
    base_url: str = os.getenv("LM_STUDIO_BASE_URL", "http://127.0.0.1:1234/v1")
    model: str = os.getenv("LM_STUDIO_MODEL", "yi-coder-9b-chat")
    api_key: str = os.getenv("LM_STUDIO_API_KEY", "lm-studio")
    timeout_seconds: int = int(os.getenv("LM_STUDIO_TIMEOUT", "120"))
    temperature: float = float(os.getenv("LM_STUDIO_TEMPERATURE", "0.2"))


@dataclass(frozen=True)
class RuntimeConfig:
    artifacts_dir: str = os.getenv("ARTIFACTS_DIR", "artifacts")
    max_attempts: int = int(os.getenv("MAX_ATTEMPTS", "20"))
    execution_timeout_seconds: int = int(os.getenv("LUA_EXEC_TIMEOUT", "15"))
    lua_backend: str = os.getenv("LUA_BACKEND", "auto")
    lua_path: str | None = os.getenv("LUA_BIN_PATH") or os.getenv("LUA_PATH")
    luajit_path: str | None = os.getenv("LUAJIT_BIN_PATH") or os.getenv("LUAJIT_PATH")
    luacheck_path: str | None = os.getenv("LUACHECK_BIN_PATH") or os.getenv("LUACHECK_PATH")


@dataclass(frozen=True)
class AgentVersionConfig:
    versions: Dict[str, str]

    @classmethod
    def from_env(cls) -> "AgentVersionConfig":
        versions = {
            role: os.getenv(f"{role.upper()}_VERSION", "v1")
            for role in AGENT_ROLES
        }
        return cls(versions=versions)
