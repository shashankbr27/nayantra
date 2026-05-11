"""
nayantra/agent/api_v2.py

AI Agent HTTP API — v2.

Adds over v1:
  - WebSocket fleet monitor (/ws/fleet)
  - Dashboard static file serving (GET /)
  - Batch command endpoint (POST /v2/batch)
  - History search (GET /v2/history/search?q=...)
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import List, Optional

import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel

from nayantra.agent.api import app as v1_app
from nayantra.api.ws_monitor import router as ws_router
from nayantra.config import settings

logger = logging.getLogger("nayantra.api_v2")

app = FastAPI(
    title="Nayantra Agent API v2",
    description="LLM-powered robot navigation API (v2)",
    version="2.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.CORS_ORIGINS,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type"],
)

app.include_router(ws_router)
app.mount("/v1", v1_app)

_DASHBOARD = Path(__file__).resolve().parents[2] / "nayantra" / "api" / "dashboard.html"


class BatchRequest(BaseModel):
    commands: List[str]


@app.get("/v2/health")
async def health_v2():
    return {"status": "ok", "version": "2.0.0"}


@app.get("/")
async def serve_dashboard():
    if _DASHBOARD.exists():
        return FileResponse(str(_DASHBOARD))
    raise HTTPException(404, "Dashboard not found")


def main() -> None:
    uvicorn.run(
        "nayantra.agent.api_v2:app",
        host=settings.AGENT_API_HOST,
        port=settings.AGENT_API_PORT,
        reload=settings.DEBUG_MODE,
        log_level=settings.LOGGING_LEVEL.lower(),
    )


if __name__ == "__main__":
    main()
