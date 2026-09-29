"""
nayantra/mcp/server.py

MCP tool server (FastAPI). The agent (and any other client) discovers and runs
Nayantra's high-level operations here. Tools act on the Nayantra Core; nothing
here touches robots directly.

Transports:
  GET  /tools          — tool schemas (JSON schema per tool, risk level)
  POST /run            — run a tool: {tool, parameters, context?: {mission_id, command}}
  GET  /sse            — Server-Sent Events stream of tool executions
  GET  /health         — liveness probe

Status codes on /run:
  200  the tool ran; `result` may be {"ok": false, "error": …} when the core
       rejected the request (unknown waypoint, no capable robot …) so the LLM
       can read the reason and adapt; `result.confirmation_required` when an
       operator must approve
  404  unknown tool     422  parameters failed schema validation

This is a REST transport shaped around MCP tool semantics, not the MCP
JSON-RPC wire protocol.

Auth: all endpoints except /health require a Bearer JWT when USE_AUTH=true.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import uvicorn
from fastapi import Depends, FastAPI, HTTPException, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field

from nayantra.config import settings
from nayantra.mcp.auth import verify_token
from nayantra.mcp.core_client import CoreClient, Trace
from nayantra.mcp.tools import ToolContext, ToolParamError, execute_tool, get_all_tools
from nayantra.rmf_client.client import OpenRMFClient

logger = logging.getLogger("nayantra.mcp")

core_client: CoreClient | None = None
rmf_client: OpenRMFClient | None = None
_event_queues: list[asyncio.Queue] = []  # fans out to SSE subscribers


@asynccontextmanager
async def lifespan(app: FastAPI):
    global core_client, rmf_client
    core_client = CoreClient()
    if settings.OPENRMF_INFRA_TOOLS:
        rmf_client = OpenRMFClient(
            api_url=settings.OPENRMF_API_URL,
            token=settings.OPENRMF_API_TOKEN,
            debug=settings.DEBUG_MODE,
        )
    logger.info(
        f"MCP server ready — {len(get_all_tools())} tools, core at {settings.NAYANTRA_CORE_URL}"
    )
    yield
    await core_client.close()
    if rmf_client:
        await rmf_client.close()


app = FastAPI(
    title="Nayantra MCP tool server",
    description="High-level, schema-validated robot-fleet operations for LLM agents",
    version="2.0.0",
    lifespan=lifespan,
)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])
security = HTTPBearer(auto_error=False)


async def auth_guard(
    credentials: HTTPAuthorizationCredentials | None = Depends(security),
) -> dict[str, Any] | None:
    if not settings.USE_AUTH:
        return None
    if not credentials:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Missing Bearer token")
    payload = verify_token(credentials.credentials)
    if payload is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="Invalid or expired token"
        )
    return payload


class RunContext(BaseModel):
    mission_id: str | None = None
    command: str | None = None


class RunRequest(BaseModel):
    tool: str
    parameters: dict[str, Any] = Field(default_factory=dict)
    context: RunContext | None = None


class RunResponse(BaseModel):
    tool: str
    result: Any
    ok: bool
    duration_ms: float
    timestamp: float


@app.get("/health")
async def health():
    return {"status": "ok", "tools": len(get_all_tools()), "core": settings.NAYANTRA_CORE_URL}


@app.get("/tools", dependencies=[Depends(auth_guard)])
async def list_tools(include_legacy: bool = False) -> list[dict[str, Any]]:
    """Tool schemas the LLM may call. Legacy aliases are executable but hidden by default."""
    return get_all_tools(include_legacy=include_legacy)


@app.post("/run", response_model=RunResponse, dependencies=[Depends(auth_guard)])
async def run_tool(req: RunRequest):
    if core_client is None:
        raise HTTPException(503, "MCP server not initialised")
    ctx = ToolContext(
        core=core_client,
        rmf=rmf_client,
        trace=Trace(
            mission_id=req.context.mission_id if req.context else None,
            command=req.context.command if req.context else None,
        ),
    )
    t0 = time.monotonic()
    try:
        result = await execute_tool(ctx, req.tool, req.parameters)
    except KeyError as exc:
        raise HTTPException(
            404, f"Unknown tool: {req.tool!r}. Call GET /tools for the list."
        ) from exc
    except ToolParamError as exc:
        raise HTTPException(422, str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        logger.exception(f"Tool execution error [{req.tool}]")
        raise HTTPException(500, f"{type(exc).__name__}: {exc}") from exc
    duration = round((time.monotonic() - t0) * 1000, 2)
    ok = not (isinstance(result, dict) and result.get("ok") is False)
    event = {
        "type": "tool_result",
        "tool": req.tool,
        "ok": ok,
        "result": result,
        "duration_ms": duration,
        "timestamp": time.time(),
    }
    for q in list(_event_queues):
        try:
            q.put_nowait(event)
        except asyncio.QueueFull:
            logger.warning(f"SSE: dropped event for a slow subscriber on tool {req.tool!r}")
    return RunResponse(
        tool=req.tool, result=result, ok=ok, duration_ms=duration, timestamp=time.time()
    )


@app.get("/sse", dependencies=[Depends(auth_guard)])
async def sse_stream(request: Request):
    queue: asyncio.Queue = asyncio.Queue(maxsize=100)
    _event_queues.append(queue)

    async def generator() -> AsyncIterator[str]:
        try:
            while True:
                if await request.is_disconnected():
                    break
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=15.0)
                    yield f"data: {json.dumps(event, default=str)}\n\n"
                except TimeoutError:
                    yield ": heartbeat\n\n"
        finally:
            _event_queues.remove(queue)

    return StreamingResponse(
        generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


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
