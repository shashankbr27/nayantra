"""
nayantra/config.py

Application settings loaded from environment variables via pydantic-settings.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict

_ROOT = Path(__file__).resolve().parents[1]


class Settings(BaseSettings):
    # Pydantic-settings reads the first .env file it finds in this tuple.
    # We accept both the project-root .env (what scripts/setup.* creates and
    # what local dev uses) and config/.env (a legacy / Docker-style location).
    model_config = SettingsConfigDict(
        env_file=(str(_ROOT / ".env"), str(_ROOT / "config" / ".env")),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # LLM — provider must be one of: anthropic | openai | gemini
    LLM_PROVIDER: Literal["anthropic", "openai", "gemini"] = "anthropic"
    ANTHROPIC_API_KEY: str = ""
    ANTHROPIC_MODEL: str = "claude-sonnet-4-6"
    OPENAI_API_KEY: str = ""
    OPENAI_MODEL: str = "gpt-4o"
    GEMINI_API_KEY: str = ""
    GEMINI_MODEL: str = "gemini-3.5-flash"

    # MCP Server — defaults to loopback. Override to 0.0.0.0 inside Docker / for LAN.
    MCP_SERVER_URL: str = "http://localhost:7000"
    MCP_SERVER_HOST: str = "127.0.0.1"
    MCP_SERVER_PORT: int = 7000

    # Agent API — defaults to loopback. Override to 0.0.0.0 inside Docker / for LAN.
    AGENT_API_HOST: str = "127.0.0.1"
    AGENT_API_PORT: int = 8080

    # OpenRMF
    OPENRMF_API_URL: str = "http://localhost:8000"
    OPENRMF_API_TOKEN: str = ""

    # Auth — JWT_SECRET has no default; if USE_AUTH=true, set it in .env.
    # The agent will refuse to start with auth enabled and an empty secret.
    USE_AUTH: bool = False
    JWT_SECRET: str = ""
    JWT_ALGORITHM: str = "HS256"
    JWT_AUDIENCE: str = "nayantra"
    JWT_ISSUER: str = "nayantra"
    API_KEY: str = ""

    # CORS — restrictive default. Add your front-end origins via .env (comma-separated).
    CORS_ORIGINS: list[str] = [
        "http://localhost:3000",
        "http://localhost:8080",
        "http://127.0.0.1:3000",
        "http://127.0.0.1:8080",
    ]

    # Isaac Sim
    ISAAC_SIM_ENABLED: bool = False
    ISAAC_SIM_URL: str = "http://localhost:8211"
    ISAAC_SIM_SCENE_PATH: str = "/Isaac/Environments/Simple_Warehouse/warehouse.usd"

    # Isaac Demo (scripts/isaac_demo.py HTTP control API). Empty = disabled.
    # When set, the isaac_* MCP tools become available to the LLM.
    ISAAC_DEMO_URL: str = ""

    # Zenoh
    ZENOH_ENABLED: bool = False
    ZENOH_ROUTER_URL: str = "tcp/localhost:7447"
    ZENOH_MODE: str = "peer"

    # Nayantra Core — the multi-fleet control plane (REST /api/v1, WebSocket,
    # web UI, legacy rmf-web-shaped routes). Replaces rmf_bridge + the stub.
    CORE_HOST: str = "127.0.0.1"
    CORE_PORT: int = 8000
    NAYANTRA_CORE_URL: str = "http://localhost:8000"  # how MCP / scripts reach the core
    NAYANTRA_DB_PATH: str = str(_ROOT / "data" / "nayantra.db")
    # Scenario seeded into an EMPTY database on first start (config/scenarios/<name>.json).
    # Empty string = start with nothing registered.
    NAYANTRA_SCENARIO: str = "warehouse_demo"
    AGENT_API_URL: str = "http://localhost:8080"  # the core proxies NL commands here
    WEB_DIST_DIR: str = str(_ROOT / "web" / "dist")
    # Register the Open-RMF infrastructure MCP tools (doors, lifts, dispensers,
    # fire alarm). Only meaningful against a real rmf-web api-server.
    OPENRMF_INFRA_TOOLS: bool = False

    # Legacy RMF bridge launcher (python -m nayantra.rmf_bridge.server) — now
    # starts the core with a single Nav2 robot described by these settings.
    ROS2_ENABLED: bool = False
    RMF_BRIDGE_HOST: str = "127.0.0.1"
    RMF_BRIDGE_PORT: int = 8000
    FLEET_NAME: str = "warehouse_fleet"
    ROBOT_NAME: str = "carter1"
    # Deprecated: waypoints now live in the core's map registry
    # (config/maps/*.json seeds, editable via the UI / API).
    WAYPOINTS_FILE: str = str(_ROOT / "config" / "waypoints.json")

    # Misc
    DEBUG_MODE: bool = True
    LOGGING_LEVEL: str = "INFO"
    FALLBACK_TOOLS_FILE: str = str(_ROOT / "config" / "tools.json")
    API_TIMEOUT: int = 30


settings = Settings()
