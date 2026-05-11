"""
nayantra/agent/api.py

AI Agent HTTP API — v1.

Endpoints:
  POST /run           — Execute a command and wait for the full result
  GET  /stream        — Execute a command with SSE step-by-step streaming
  GET  /history       — Recent mission list
  GET  /history/{id}  — Single mission detail
  GET  /stats         — Mission statistics
  GET  /health        — Liveness probe
  GET  /readiness     — Full startup readiness check
"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import Optional

import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from nayantra.agent.agent import RMFAgent
from nayantra.agent.health import HealthChecker
from nayantra.agent.history import MissionStore
from nayantra.config import settings

logger = logging.getLogger("nayantra.api")

_agent: Optional[RMFAgent] = None
_store: Optional[MissionStore] = None
_health: Optional[HealthChecker] = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _agent, _store, _health
    if not settings.USE_AUTH:
        logger.warning(
            "USE_AUTH=false — the agent API is unauthenticated. "
            "DO NOT expose this service beyond localhost. "
            "Set USE_AUTH=true and configure JWT_SECRET for any non-local deployment."
        )
    if settings.USE_AUTH and not settings.JWT_SECRET:
        raise RuntimeError(
            "USE_AUTH=true but JWT_SECRET is empty. "
            "Generate a strong secret: "
            "python -c \"import secrets; print(secrets.token_urlsafe(48))\""
        )
    _agent = RMFAgent()
    _store = MissionStore()
    _health = HealthChecker()
    report = await _health.run_all()
    if not report.ready:
        logger.warning("Startup health check FAILED — some services may be unavailable")
    yield
    if _agent:
        await _agent.close()
    if _health:
        await _health.close()


app = FastAPI(
    title="Nayantra Agent API",
    description="LLM-powered robot navigation API",
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.CORS_ORIGINS,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type"],
)


class CommandRequest(BaseModel):
    command: str


class CommandResponse(BaseModel):
    mission_id: str
    command: str
    summary: str
    success: bool
    step_count: int


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/readiness")
async def readiness():
    if _health is None:
        raise HTTPException(503, "Health checker not ready")
    report = await _health.run_all()
    if not report.ready:
        raise HTTPException(503, report.to_dict())
    return report.to_dict()


@app.post("/run", response_model=CommandResponse)
async def run_command(req: CommandRequest):
    """Execute a natural-language robot command."""
    if _agent is None:
        raise HTTPException(503, "Agent not initialised")
    mission = await _agent.run(req.command)
    if _store:
        await _store.save(mission)
    return CommandResponse(
        mission_id=mission.mission_id,
        command=mission.command,
        summary=mission.summary,
        success=mission.success,
        step_count=len(mission.steps),
    )


@app.get("/stream")
async def stream_command(command: str):
    """Execute a command with Server-Sent Events streaming."""
    if _agent is None:
        raise HTTPException(503, "Agent not initialised")
    return StreamingResponse(
        _agent.stream_run(command),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.get("/history")
async def get_history(limit: int = 50):
    if _store is None:
        return []
    return await _store.recent(limit)


@app.get("/history/{mission_id}")
async def get_mission(mission_id: str):
    if _store is None:
        raise HTTPException(503, "Store not initialised")
    result = await _store.get(mission_id)
    if not result:
        raise HTTPException(404, f"Mission {mission_id} not found")
    return result


@app.get("/stats")
async def get_stats():
    if _store is None:
        return {}
    return await _store.stats()


def main() -> None:
    uvicorn.run(
        "nayantra.agent.api:app",
        host=settings.AGENT_API_HOST,
        port=settings.AGENT_API_PORT,
        reload=settings.DEBUG_MODE,
        log_level=settings.LOGGING_LEVEL.lower(),
    )


if __name__ == "__main__":
    main()
