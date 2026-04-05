from importlib import import_module
from pathlib import Path
from typing import Dict

from config import AGENT_ROLES, AgentVersionConfig, ModelConfig, RuntimeConfig
from llm_client import LocalChatModel
from lua_runtime import LuaToolchain
from registry import AgentRegistry


def create_registry(
    *,
    agent_versions: Dict[str, str] | None = None,
    model_config: ModelConfig | None = None,
    runtime_config: RuntimeConfig | None = None,
) -> AgentRegistry:
    resolved_model_config = model_config or ModelConfig()
    resolved_runtime_config = runtime_config or RuntimeConfig()
    resolved_versions = AgentVersionConfig.from_env().versions
    if agent_versions:
        resolved_versions.update(agent_versions)

    model_client = LocalChatModel(resolved_model_config)
    lua_toolchain = LuaToolchain(
        execution_timeout_seconds=resolved_runtime_config.execution_timeout_seconds,
        backend=resolved_runtime_config.lua_backend,
        lua_path=resolved_runtime_config.lua_path,
        luajit_path=resolved_runtime_config.luajit_path,
        linter=resolved_runtime_config.luacheck_path,
    )

    agents = {
        role: _build_agent(
            role=role,
            version=resolved_versions.get(role, "v1"),
            model_client=model_client,
            lua_toolchain=lua_toolchain,
            runtime_config=resolved_runtime_config,
        )
        for role in AGENT_ROLES
    }
    return AgentRegistry(**agents)


def list_available_versions() -> Dict[str, list[str]]:
    base_path = Path(__file__).resolve().parent / "agents"
    available: Dict[str, list[str]] = {}

    for role in AGENT_ROLES:
        role_path = base_path / role
        available[role] = sorted(
            file_path.stem
            for file_path in role_path.glob("*.py")
            if file_path.stem != "__init__"
        )

    return available


def _build_agent(
    *,
    role: str,
    version: str,
    model_client: LocalChatModel,
    lua_toolchain: LuaToolchain,
    runtime_config: RuntimeConfig,
):
    module = import_module(f"agents.{role}.{version}")
    agent_class = getattr(module, "AGENT_CLASS")
    return agent_class(
        model_client=model_client,
        lua_toolchain=lua_toolchain,
        runtime_config=runtime_config,
    )
