"""Abstract base class shared by all agent tools."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any


class Tool(ABC):
    """A tool an agent can invoke. Concrete tools define their own kwargs."""

    @abstractmethod
    async def run(self, **kwargs: Any) -> dict:
        """Primary entry point for the tool."""
