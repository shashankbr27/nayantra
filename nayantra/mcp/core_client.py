"""
nayantra/mcp/core_client.py

HTTP client from the MCP layer to the Nayantra Core (/api/v1).

Every request identifies itself as the MCP layer (X-Nayantra-Client: mcp).
The core therefore records "llm" as the origin of whatever the agent does and
never lets it confirm a dangerous action on its own. The agent's mission id
and the operator's command travel along, so tasks stay traceable back to the
sentence that caused them.
"""

from __future__ import annotations

import urllib.parse
from dataclasses import dataclass, field
from typing import Any

import httpx

from nayantra.config import settings


class CoreError(Exception):
    """The core rejected the request (validation, conflict, not found …)."""

    def __init__(self, status: int, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.status = status
        self.message = message
        self.details = details or {}


@dataclass
class Trace:
    mission_id: str | None = None
    command: str | None = None
    tool: str | None = None

    def headers(self) -> dict[str, str]:
        h = {}
        if self.mission_id:
            h["X-Nayantra-Mission"] = self.mission_id[:64]
        if self.command:
            h["X-Nayantra-Command"] = urllib.parse.quote(self.command[:300])
        if self.tool:
            h["X-Nayantra-Tool"] = self.tool
        return h


@dataclass
class CoreClient:
    base_url: str = field(default_factory=lambda: settings.NAYANTRA_CORE_URL)
    timeout: float = 15.0
    transport: httpx.AsyncBaseTransport | None = None  # tests inject the ASGI app here

    def __post_init__(self) -> None:
        self._http = httpx.AsyncClient(
            base_url=self.base_url.rstrip("/") + "/api/v1",
            timeout=self.timeout,
            headers={"X-Nayantra-Client": "mcp"},
            transport=self.transport,
        )

    async def close(self) -> None:
        await self._http.aclose()

    async def request(
        self,
        method: str,
        path: str,
        body: Any = None,
        params: dict[str, Any] | None = None,
        trace: Trace | None = None,
    ) -> Any:
        try:
            resp = await self._http.request(
                method,
                path,
                json=body,
                params={k: v for k, v in (params or {}).items() if v is not None},
                headers=trace.headers() if trace else None,
            )
        except httpx.TransportError as exc:
            raise CoreError(
                503,
                f"Nayantra Core is not reachable at {self.base_url} ({type(exc).__name__}). "
                "Start it with `nayantra-core`.",
            ) from exc
        data: Any
        try:
            data = resp.json()
        except ValueError:
            data = {"message": resp.text}
        if resp.status_code >= 400:
            if isinstance(data, dict):
                raise CoreError(
                    resp.status_code,
                    str(data.get("message") or data.get("detail")),
                    data.get("details"),
                )
            raise CoreError(resp.status_code, str(data))
        return data

    async def get(
        self, path: str, params: dict[str, Any] | None = None, trace: Trace | None = None
    ) -> Any:
        return await self.request("GET", path, params=params, trace=trace)

    async def post(
        self,
        path: str,
        body: Any = None,
        params: dict[str, Any] | None = None,
        trace: Trace | None = None,
    ) -> Any:
        return await self.request(
            "POST", path, body if body is not None else {}, params=params, trace=trace
        )

    async def patch(self, path: str, body: Any, trace: Trace | None = None) -> Any:
        return await self.request("PATCH", path, body, trace=trace)

    async def delete(self, path: str, trace: Trace | None = None) -> Any:
        return await self.request("DELETE", path, trace=trace)
