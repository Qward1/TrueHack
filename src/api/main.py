"""FastAPI application entry point."""

from __future__ import annotations

import os
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
import structlog
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from src.api.dependencies import set_engine
from src.api.routes import chats, code, messages
from src.api.schemas import HealthResponse
from src.core.config import get_settings
from src.graph.engine import AgentEngine

logger = structlog.get_logger(__name__)

# Ensure lua54 is on PATH on Windows when the interpreter lives at C:\lua54
_LUA_DIR = r"C:\lua54"
if os.path.isdir(_LUA_DIR) and _LUA_DIR not in os.environ.get("PATH", ""):
    os.environ["PATH"] = _LUA_DIR + os.pathsep + os.environ["PATH"]

_UI_DIR = Path(__file__).parents[1] / "ui"


# ── Lifespan ──────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Initialise resources on startup; close them on shutdown."""
    settings = get_settings()
    engine = AgentEngine(settings)
    await engine.initialize()
    set_engine(engine)
    logger.info("api_startup_complete")
    yield
    await engine.close()
    logger.info("api_shutdown_complete")


# ── App ───────────────────────────────────────────────────────────────────

app = FastAPI(
    title="LocalScript API",
    description="Local agentic Lua code generation system",
    version="0.1.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── Routers ───────────────────────────────────────────────────────────────

app.include_router(chats.router)
app.include_router(messages.router)
app.include_router(code.router)


# ── Health ────────────────────────────────────────────────────────────────

@app.get("/api/health", response_model=HealthResponse, tags=["health"])
async def health() -> HealthResponse:
    """Check LM Studio availability and model load status."""
    settings = get_settings()
    base_url = settings.llm.base_url.rstrip("/")
    llm_available = False
    models_loaded = False

    try:
        async with httpx.AsyncClient(timeout=3.0) as client:
            resp = await client.get(f"{base_url}/models")
            if resp.status_code == 200:
                llm_available = True
                data = resp.json()
                models = data.get("data", [])
                models_loaded = len(models) > 0
    except Exception:  # noqa: BLE001
        pass

    status = "ok" if (llm_available and models_loaded) else "degraded"
    return HealthResponse(
        status=status,
        llm_available=llm_available,
        models_loaded=models_loaded,
    )


# ── Static UI ─────────────────────────────────────────────────────────────

if _UI_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(_UI_DIR)), name="static")

    @app.get("/", include_in_schema=False)
    async def serve_ui() -> FileResponse:
        index = _UI_DIR / "index.html"
        if index.exists():
            return FileResponse(str(index))
        return FileResponse(str(_UI_DIR))
