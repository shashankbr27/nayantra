"""
tests/test_config.py

Settings smoke tests — verify all fields have sensible defaults.
"""

from nayantra.config import settings


def test_settings_instance():
    assert settings is not None


def test_default_llm_provider():
    assert settings.LLM_PROVIDER in ("anthropic", "openai")


def test_mcp_server_port_is_int():
    assert isinstance(settings.MCP_SERVER_PORT, int)
    assert settings.MCP_SERVER_PORT > 0


def test_agent_api_port_is_int():
    assert isinstance(settings.AGENT_API_PORT, int)
    assert settings.AGENT_API_PORT > 0


def test_jwt_fields():
    assert isinstance(settings.JWT_SECRET, str)
    assert isinstance(settings.JWT_ALGORITHM, str)
    assert isinstance(settings.JWT_AUDIENCE, str)
    assert isinstance(settings.JWT_ISSUER, str)


def test_debug_mode_is_bool():
    assert isinstance(settings.DEBUG_MODE, bool)


def test_fallback_tools_file_is_string():
    assert isinstance(settings.FALLBACK_TOOLS_FILE, str)
    assert settings.FALLBACK_TOOLS_FILE.endswith("tools.json")


def test_timeout_positive():
    assert settings.API_TIMEOUT > 0
