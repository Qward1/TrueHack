"""Chat management endpoints."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from src.api.dependencies import get_engine
from src.api.schemas import ChatResponse, CreateChatRequest
from src.graph.engine import AgentEngine

router = APIRouter(prefix="/api/chats", tags=["chats"])


@router.post("", response_model=ChatResponse, status_code=201)
async def create_chat(
    body: CreateChatRequest,
    engine: AgentEngine = Depends(get_engine),
) -> ChatResponse:
    """Create a new chat session."""
    chat = await engine._repo.create_chat(body.title)
    return ChatResponse(
        id=chat.id,
        title=chat.title,
        created_at=chat.created_at,
        updated_at=chat.updated_at,
    )


@router.get("", response_model=list[ChatResponse])
async def list_chats(
    engine: AgentEngine = Depends(get_engine),
) -> list[ChatResponse]:
    """Return all chats ordered by creation time."""
    chats = await engine._repo.list_chats()
    return [
        ChatResponse(
            id=c.id,
            title=c.title,
            created_at=c.created_at,
            updated_at=c.updated_at,
        )
        for c in chats
    ]


@router.delete("/{chat_id}", status_code=204)
async def delete_chat(
    chat_id: str,
    engine: AgentEngine = Depends(get_engine),
) -> None:
    """Delete a chat and all its messages."""
    chat = await engine._repo.get_chat(chat_id)
    if chat is None:
        raise HTTPException(status_code=404, detail="Chat not found")
    await engine._repo.delete_chat(chat_id)
