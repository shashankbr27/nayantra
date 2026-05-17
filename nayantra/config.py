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
    model_config = SettingsConfigDict(
        env_file=str(_ROOT / "config" / ".env"),
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

    # Zenoh
    ZENOH_ENABLED: bool = False
    ZENOH_ROUTER_URL: str = "tcp/localhost:7447"
    ZENOH_MODE: str = "peer"

    # Misc
    DEBUG_MODE: bool = True
    LOGGING_LEVEL: str = "INFO"
    FALLBACK_TOOLS_FILE: str = str(_ROOT / "config" / "tools.json")
    API_TIMEOUT: int = 30


settings = Settings()
