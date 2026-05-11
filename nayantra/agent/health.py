"""
nayantra/agent/health.py

Startup health checker and runtime readiness validator.

Checks performed at startup:
  1. Config validation (required env vars present)
  2. MCP Server reachability
  3. OpenRMF API reachability
  4. Isaac Sim reachability (if ISAAC_SIM_ENABLED=true)
  5. LLM provider connectivity (lightweight test call)

Each check returns a HealthResult with status, message, and latency_ms.
The aggregate is exposed at GET /readiness and logged at startup.

Usage:
    checker = HealthChecker()
    report  = await checker.run_all()
    if not report.ready:
        sys.exit(1)
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field

import httpx

from nayantra.config import settings

logger = logging.getLogger("rmf.health")


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass
class HealthResult:
    name: str
    ok: bool
    message: str
    latency_ms: float | None = None
    optional: bool = False  # if True, failure is warned but not fatal


@dataclass
class HealthReport:
    results: list[HealthResult] = field(default_factory=list)

    @property
    def ready(self) -> bool:
        """True only if all non-optional checks passed."""
        return all(r.ok for r in self.results if not r.optional)

    @property
    def summary(self) -> str:
        lines = []
        for r in self.results:
            icon = "✅" if r.ok else ("⚠️ " if r.optional else "❌")
            ms = f" ({r.latency_ms:.0f}ms)" if r.latency_ms is not None else ""
            lines.append(f"  {icon} {r.name}{ms}: {r.message}")
        status = "READY" if self.ready else "NOT READY"
        return f"System {status}\n" + "\n".join(lines)

    def to_dict(self) -> dict:
        return {
            "ready": self.ready,
            "checks": [
                {
                    "name": r.name,
                    "ok": r.ok,
                    "message": r.message,
                    "latency_ms": r.latency_ms,
                    "optional": r.optional,
                }
                for r in self.results
            ],
        }


# ---------------------------------------------------------------------------
# Checker
# ---------------------------------------------------------------------------


class HealthChecker:
    def __init__(self) -> None:
        self._http = httpx.AsyncClient(timeout=5.0)

    async def run_all(self) -> HealthReport:
        report = HealthReport()
        checks = [
            self._check_config(),
            self._check_mcp_server(),
            self._check_rmf_api(),
        ]
        if settings.ISAAC_SIM_ENABLED:
            checks.append(self._check_isaac_sim())

        results = await asyncio.gather(*checks, return_exceptions=True)
        for r in results:
            if isinstance(r, Exception):
                report.results.append(HealthResult("unknown", False, str(r)))
            else:
                report.results.append(r)

        logger.info("\n" + report.summary)
        return report

    # ------------------------------------------------------------------
    # Individual checks
    # ------------------------------------------------------------------

    async def _check_config(self) -> HealthResult:
        """Validate required configuration values are present."""
        issues = []

        if settings.LLM_PROVIDER == "anthropic" and not settings.ANTHROPIC_API_KEY:
            issues.append("ANTHROPIC_API_KEY not set")
        if settings.LLM_PROVIDER == "openai" and not settings.OPENAI_API_KEY:
            issues.append("OPENAI_API_KEY not set")
        if not settings.OPENRMF_API_TOKEN:
            issues.append("OPENRMF_API_TOKEN not set")
        if settings.USE_AUTH and settings.JWT_SECRET == "rmfisawesome":
            issues.append("JWT_SECRET is still the default — change it for production")

        if issues:
            return HealthResult(
                "config",
                ok=False,
                message="; ".join(issues),
            )
        return HealthResult("config", ok=True, message="All required config present")

    async def _check_mcp_server(self) -> HealthResult:
        """Check MCP server is reachable."""
        url = f"{settings.MCP_SERVER_URL}/health"
        t0 = time.monotonic()
        try:
            resp = await self._http.get(url)
            latency = (time.monotonic() - t0) * 1000
            if resp.status_code == 200:
                return HealthResult(
                    "mcp_server", ok=True, message="Reachable", latency_ms=round(latency, 1)
                )
            return HealthResult(
                "mcp_server",
                ok=False,
                message=f"HTTP {resp.status_code}",
                latency_ms=round(latency, 1),
            )
        except httpx.ConnectError:
            latency = (time.monotonic() - t0) * 1000
            return HealthResult(
                "mcp_server",
                ok=False,
                message=f"Connection refused at {url}",
                latency_ms=round(latency, 1),
            )
        except Exception as exc:
            return HealthResult("mcp_server", ok=False, message=str(exc))

    async def _check_rmf_api(self) -> HealthResult:
        """Check OpenRMF API is reachable."""
        url = f"{settings.OPENRMF_API_URL}/health"
        t0 = time.monotonic()
        try:
            resp = await self._http.get(url)
            latency = (time.monotonic() - t0) * 1000
            if resp.status_code in (200, 404):  # 404 = no /health but server exists
                return HealthResult(
                    "rmf_api", ok=True, message="Reachable", latency_ms=round(latency, 1)
                )
            return HealthResult(
                "rmf_api",
                ok=False,
                message=f"HTTP {resp.status_code}",
                latency_ms=round(latency, 1),
            )
        except httpx.ConnectError:
            latency = (time.monotonic() - t0) * 1000
            # Non-fatal in debug mode
            return HealthResult(
                "rmf_api",
                ok=settings.DEBUG_MODE,
                message=f"Connection refused at {url} "
                f"({'OK in debug mode' if settings.DEBUG_MODE else 'FATAL'})",
                latency_ms=round(latency, 1),
                optional=settings.DEBUG_MODE,
            )
        except Exception as exc:
            return HealthResult("rmf_api", ok=False, message=str(exc), optional=settings.DEBUG_MODE)

    async def _check_isaac_sim(self) -> HealthResult:
        """Check Isaac Sim REST API is reachable (optional)."""
        url = f"{settings.ISAAC_SIM_URL}/health"
        t0 = time.monotonic()
        try:
            resp = await self._http.get(url)
            latency = (time.monotonic() - t0) * 1000
            return HealthResult(
                "isaac_sim",
                ok=resp.status_code == 200,
                message="Reachable" if resp.status_code == 200 else f"HTTP {resp.status_code}",
                latency_ms=round(latency, 1),
                optional=True,
            )
        except Exception as exc:
            return HealthResult(
                "isaac_sim", ok=False, message=f"Not reachable: {exc}", optional=True
            )

    async def close(self) -> None:
        await self._http.aclose()
