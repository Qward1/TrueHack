"""Message endpoints — send a user message and get the agent reply."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from src.api.dependencies import get_engine
from src.api.schemas import AgentResponse, MessageResponse, SendMessageRequest
from src.graph.engine import AgentEngine

router = APIRouter(prefix="/api/chats", tags=["messages"])


@router.post("/{chat_id}/messages", response_model=AgentResponse)
async def send_message(
    chat_id: str,
    body: SendMessageRequest,
    engine: AgentEngine = Depends(get_engine),
) -> AgentResponse:
    """Send a user message; run the agent pipeline; return the reply."""
    chat = await engine._repo.get_chat(chat_id)
    if chat is None:
        raise HTTPException(status_code=404, detail="Chat not found")

    result = await engine.process_message(chat_id, body.content)

    validation_results = None
    if "validation_passed" in result:
        validation_results = {
            "passed": result["validation_passed"],
            "errors": result.get("validation_errors", ""),
        }

    return AgentResponse(
        response=result["response"],
        response_type=result["response_type"],
        code=result.get("code"),
        validation_results=validation_results,
    )


@router.get("/{chat_id}/messages", response_model=list[MessageResponse])
async def get_messages(
    chat_id: str,
    limit: int = 50,
    engine: AgentEngine = Depends(get_engine),
) -> list[MessageResponse]:
    """Return the message history for a chat."""
    chat = await engine._repo.get_chat(chat_id)
    if chat is None:
        raise HTTPException(status_code=404, detail="Chat not found")

    messages = await engine._repo.get_messages(chat_id, limit=limit)
    return [
        MessageResponse(
            id=m.id,
            role=m.role,
            content=m.content,
            timestamp=m.timestamp,
            metadata=m.metadata,
        )
        for m in messages
    ]
