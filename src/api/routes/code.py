"""Code execution and retrieval endpoints."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from src.api.dependencies import get_engine
from src.api.schemas import ExecuteCodeRequest, ExecuteResponse
from src.graph.engine import AgentEngine

router = APIRouter(prefix="/api", tags=["code"])


@router.post("/execute", response_model=ExecuteResponse)
async def execute_code(
    body: ExecuteCodeRequest,
    engine: AgentEngine = Depends(get_engine),
) -> ExecuteResponse:
    """Run Lua code in the sandbox and return stdout/stderr."""
    result = await engine.execute_code(body.code)
    return ExecuteResponse(
        success=result["success"],
        stdout=result["stdout"],
        stderr=result["stderr"],
        timed_out=result["timed_out"],
    )


@router.get("/chats/{chat_id}/code")
async def get_latest_code(
    chat_id: str,
    engine: AgentEngine = Depends(get_engine),
) -> dict:
    """Return the most recently generated Lua code for a chat."""
    chat = await engine._repo.get_chat(chat_id)
    if chat is None:
        raise HTTPException(status_code=404, detail="Chat not found")

    code = await engine._repo.get_latest_code(chat_id)
    return {"code": code}
