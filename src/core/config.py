"""Configuration loader: reads config/settings.yaml into typed Pydantic models."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field


class AgentGenerationParams(BaseModel):
    temperature: float
    max_tokens: int


class LLMConfig(BaseModel):
    provider: str
    base_url: str
    api_key: str
    model: str
    generation_params: dict[str, AgentGenerationParams] = Field(default_factory=dict)


class PathsConfig(BaseModel):
    database: str
    rag_index: str
    lua_docs: str


class LuaConfig(BaseModel):
    interpreter: str


class Settings(BaseModel):
    llm: LLMConfig
    max_fix_iterations: int = 3
    max_chat_history: int = 10
    paths: PathsConfig
    lua: LuaConfig

    def get_agent_params(self, agent_name: str) -> AgentGenerationParams:
        """Return generation params for a specific agent, falling back to coder defaults."""
        return self.llm.generation_params.get(
            agent_name,
            AgentGenerationParams(temperature=0.2, max_tokens=1024),
        )


_settings: Settings | None = None
_CONFIG_PATH = Path(__file__).parents[2] / "config" / "settings.yaml"


def load_settings(config_path: Path | None = None) -> Settings:
    """Load and cache settings from YAML file."""
    global _settings
    if _settings is None:
        path = config_path or _CONFIG_PATH
        raw: dict[str, Any] = yaml.safe_load(path.read_text(encoding="utf-8"))
        _settings = Settings.model_validate(raw)
    return _settings


def get_settings() -> Settings:
    """Return cached settings, loading them if necessary."""
    return load_settings()
