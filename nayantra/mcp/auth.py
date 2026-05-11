"""
nayantra/mcp/auth.py

JWT token creation and verification for the MCP server.
"""
from __future__ import annotations

import datetime
import logging
from typing import Dict, Optional

import jwt

from nayantra.config import settings

logger = logging.getLogger("rmf.auth")


def create_token(
    subject: str = "admin",
    username: str = "admin",
    hours: int = 24,
) -> str:
    """Create a signed JWT token."""
    now = datetime.datetime.utcnow()
    payload = {
        "iss": settings.JWT_ISSUER,
        "aud": settings.JWT_AUDIENCE,
        "sub": subject,
        "preferred_username": username,
        "iat": now,
        "exp": now + datetime.timedelta(hours=hours),
    }
    return jwt.encode(payload, settings.JWT_SECRET, algorithm=settings.JWT_ALGORITHM)


def verify_token(token: str) -> Optional[Dict]:
    """Verify a JWT token and return its payload, or None if invalid."""
    if not token:
        return None
    try:
        return jwt.decode(
            token,
            settings.JWT_SECRET,
            algorithms=[settings.JWT_ALGORITHM],
            audience=settings.JWT_AUDIENCE,
            issuer=settings.JWT_ISSUER,
        )
    except jwt.PyJWTError as exc:
        logger.debug(f"Token verification failed: {exc}")
        return None
