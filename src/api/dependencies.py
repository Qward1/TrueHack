"""FastAPI dependency: singleton AgentEngine."""

from __future__ import annotations

from src.graph.engine import AgentEngine

# Set by lifespan in main.py
_engine: AgentEngine | None = None


def set_engine(engine: AgentEngine) -> None:
    """Called once at startup to register the singleton."""
    global _engine
    _engine = engine


def get_engine() -> AgentEngine:
    """FastAPI Depends target — returns the initialised engine."""
    if _engine is None:
        raise RuntimeError("AgentEngine is not initialised yet.")
    return _engine
