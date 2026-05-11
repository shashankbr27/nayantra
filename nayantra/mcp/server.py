"""
nayantra/mcp/server.py

MCP (Model Context Protocol) Server — FastAPI implementation.

Transports:
  GET  /tools          — list available tools
  POST /run            — execute a tool (REST, blocking)
  GET  /sse            — Server-Sent Events stream for real-time updates
  GET  /health         — liveness probe

Auth:
  All endpoints (except /health) require a Bearer JWT when USE_AUTH=true.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator, Dict, List, Optional

import uvicorn
from fastapi import Depends, FastAPI, HTTPException, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel

from nayantra.config import settings
from nayantra.mcp.auth import verify_token
from nayantra.mcp.tools import TOOL_REGISTRY, execute_tool, get_all_tools
from nayantra.rmf_client.client import OpenRMFClient

logger = logging.getLogger("rmf.mcp")

# ---------------------------------------------------------------------------
# Shared state
# ---------------------------------------------------------------------------

rmf_client: Optional[OpenRMFClient] = None
_event_queues: List[asyncio.Queue] = []   # fans out to SSE subscribers


@asynccontextmanager
async def lifespan(app: FastAPI):
    global rmf_client
    rmf_client = OpenRMFClient(
        api_url=settings.OPENRMF_API_URL,
        token=settings.OPENRMF_API_TOKEN,
        debug=settings.DEBUG_MODE,
    )
    logger.info(f"MCP Server ready — RMF endpoint: {settings.OPENRMF_API_URL}")
    yield
    if rmf_client:
        await rmf_client.close()


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

app = FastAPI(
    title="RMF MCP Server",
    description="Model Context Protocol server for Open-RMF fleet management",
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

security = HTTPBearer(auto_error=False)


# ---------------------------------------------------------------------------
# Auth dependency
# ---------------------------------------------------------------------------

async def auth_guard(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(security),
) -> Optional[Dict[str, Any]]:
    if not settings.USE_AUTH:
        return None
    if not credentials:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing Bearer token",
        )
    payload = verify_token(credentials.credentials)
    if payload is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Invalid or expired token",
        )
    return payload


# ---------------------------------------------------------------------------
# Request / Response models
# ---------------------------------------------------------------------------

class RunRequest(BaseModel):
    tool: str
    parameters: Dict[str, Any] = {}


class RunResponse(BaseModel):
    tool: str
    result: Any
    duration_ms: float
    timestamp: float


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@app.get("/health")
async def health():
    return {"status": "ok", "tools": len(get_all_tools())}


@app.get("/tools", dependencies=[Depends(auth_guard)])
async def list_tools() -> List[Dict[str, Any]]:
    """Return the full tool schema for all registered MCP tools."""
    return get_all_tools()


@app.post("/run", response_model=RunResponse, dependencies=[Depends(auth_guard)])
async def run_tool(req: RunRequest):
    """Execute a named tool against the OpenRMF backend."""
    if rmf_client is None:
        raise HTTPException(503, "RMF client not initialised")

    t0 = time.monotonic()
    try:
        result = await execute_tool(rmf_client, req.tool, req.parameters)
    except KeyError:
        raise HTTPException(404, f"Unknown tool: {req.tool!r}")
    except Exception as exc:
        logger.exception(f"Tool execution error [{req.tool}]: {exc}")
        raise HTTPException(500, str(exc))

    duration = (time.monotonic() - t0) * 1000

    # Fan-out to SSE subscribers
    event = {
        "type": "tool_result",
        "tool": req.tool,
        "result": result,
        "duration_ms": round(duration, 2),
        "timestamp": time.time(),
    }
    slow_subscribers = 0
    for q in list(_event_queues):
        try:
            q.put_nowait(event)
        except asyncio.QueueFull:
            slow_subscribers += 1
    if slow_subscribers:
        logger.warning(
            f"SSE: dropped event for {slow_subscribers}/{len(_event_queues)} "
            f"slow subscriber(s) on tool {req.tool!r}"
        )

    return RunResponse(
        tool=req.tool,
        result=result,
        duration_ms=round(duration, 2),
        timestamp=time.time(),
    )


@app.get("/sse", dependencies=[Depends(auth_guard)])
async def sse_stream(request: Request):
    """
    Server-Sent Events endpoint.
    Subscribers receive real-time tool execution events.
    """
    queue: asyncio.Queue = asyncio.Queue(maxsize=100)
    _event_queues.append(queue)

    async def generator() -> AsyncIterator[str]:
        try:
            # Send a heartbeat every 15 s to keep the connection alive
            while True:
                if await request.is_disconnected():
                    break
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=15.0)
                    yield f"data: {json.dumps(event)}\n\n"
                except asyncio.TimeoutError:
                    yield ": heartbeat\n\n"
        finally:
            _event_queues.remove(queue)

    return StreamingResponse(
        generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ---------------------------------------------------------------------------
# CLI entry
# ---------------------------------------------------------------------------

def main() -> None:
    uvicorn.run(
        "nayantra.mcp.server:app",
        host=settings.MCP_SERVER_HOST,
        port=settings.MCP_SERVER_PORT,
        reload=settings.DEBUG_MODE,
        log_level=settings.LOGGING_LEVEL.lower(),
    )


if __name__ == "__main__":
    main()
