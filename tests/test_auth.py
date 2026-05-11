"""
tests/test_auth.py

Tests for the JWT auth module.
"""
from __future__ import annotations

import datetime
import pytest
import jwt

from nayantra.mcp.auth import create_token, verify_token
from nayantra.config import settings


# ---------------------------------------------------------------------------
# Token creation
# ---------------------------------------------------------------------------

def test_create_token_returns_string():
    token = create_token()
    assert isinstance(token, str)
    assert len(token) > 20


def test_create_token_custom_subject():
    token = create_token(subject="operator", username="op1", hours=1)
    payload = jwt.decode(
        token,
        settings.JWT_SECRET,
        algorithms=[settings.JWT_ALGORITHM],
        audience=settings.JWT_AUDIENCE,
        issuer=settings.JWT_ISSUER,
    )
    assert payload["sub"] == "operator"
    assert payload["preferred_username"] == "op1"


def test_create_token_expiry_set():
    token = create_token(hours=2)
    payload = jwt.decode(
        token,
        settings.JWT_SECRET,
        algorithms=[settings.JWT_ALGORITHM],
        audience=settings.JWT_AUDIENCE,
        issuer=settings.JWT_ISSUER,
    )
    assert "exp" in payload
    assert payload["exp"] > datetime.datetime.utcnow().timestamp()


# ---------------------------------------------------------------------------
# Token verification — success
# ---------------------------------------------------------------------------

def test_verify_valid_token_returns_payload():
    token = create_token()
    payload = verify_token(token)
    assert payload is not None
    assert "sub" in payload
    assert payload["aud"] == settings.JWT_AUDIENCE


def test_verify_token_contains_username():
    token = create_token(username="alice")
    payload = verify_token(token)
    assert payload["preferred_username"] == "alice"


# ---------------------------------------------------------------------------
# Token verification — failures
# ---------------------------------------------------------------------------

def test_verify_expired_token_returns_none():
    payload = {
        "iss": settings.JWT_ISSUER,
        "aud": settings.JWT_AUDIENCE,
        "sub": "admin",
        "preferred_username": "admin",
        "exp": datetime.datetime.utcnow() - datetime.timedelta(hours=1),
    }
    token = jwt.encode(payload, settings.JWT_SECRET, algorithm=settings.JWT_ALGORITHM)
    assert verify_token(token) is None


def test_verify_wrong_secret_returns_none():
    payload = {
        "iss": settings.JWT_ISSUER,
        "aud": settings.JWT_AUDIENCE,
        "sub": "admin",
        "preferred_username": "admin",
        "exp": datetime.datetime.utcnow() + datetime.timedelta(hours=1),
    }
    token = jwt.encode(payload, "wrong_secret", algorithm=settings.JWT_ALGORITHM)
    assert verify_token(token) is None


def test_verify_wrong_audience_returns_none():
    payload = {
        "iss": settings.JWT_ISSUER,
        "aud": "wrong_audience",
        "sub": "admin",
        "exp": datetime.datetime.utcnow() + datetime.timedelta(hours=1),
    }
    token = jwt.encode(payload, settings.JWT_SECRET, algorithm=settings.JWT_ALGORITHM)
    assert verify_token(token) is None


def test_verify_garbage_string_returns_none():
    assert verify_token("not.a.valid.token") is None


def test_verify_empty_string_returns_none():
    assert verify_token("") is None
