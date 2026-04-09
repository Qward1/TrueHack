"""Router agent — classifies user intent."""

from __future__ import annotations

import time

import structlog

from src.agents.base import BaseAgent
from src.core.config import Settings
from src.core.llm import LLMProvider
from src.core.state import AgentState

logger = structlog.get_logger(__name__)

_ROUTER_SCHEMA = {
    "intent": "string",
    "confidence": "number",
}

_ROUTER_FALLBACK = {"intent": "generate_unclear", "confidence": 0.0}


class RouterAgent(BaseAgent):
    """Classifies the user's message into a routing intent."""

    def __init__(self, llm: LLMProvider, settings: Settings) -> None:
        super().__init__(llm, settings)
        self._template = self._load_prompt("router")

    async def run(self, state: AgentState) -> AgentState:
        """Determine intent and update ``state.intent``."""
        start = time.perf_counter()

        has_previous_code = bool(
            state.get("assembled_code")
            or state.get("generated_code")
            or state.get("generated_codes")
        )

        # Derive last_topic from the most recent assistant message (if any)
        last_topic = ""
        for msg in reversed(state.get("messages", [])):
            if msg["role"] == "assistant":
                last_topic = msg["content"][:120].replace("\n", " ")
                break

        prompt = self._render_prompt(
            self._template,
            has_previous_code=str(has_previous_code).lower(),
            last_topic=last_topic,
            user_message=state["user_input"],
        )

        result = await self._llm.generate_structured(
            prompt=prompt,
            system="You are an intent classifier. Respond with JSON only.",
            schema=_ROUTER_SCHEMA,
            fallback=_ROUTER_FALLBACK,
        )

        intent: str = result.get("intent", "generate_unclear")
        confidence: float = float(result.get("confidence", 0.0))

        if confidence < 0.5:
            intent = "generate_unclear"

        elapsed = time.perf_counter() - start
        logger.info(
            "router_done",
            intent=intent,
            confidence=confidence,
            elapsed_s=round(elapsed, 3),
        )

        return {**state, "intent": intent}
