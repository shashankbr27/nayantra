"""Typed errors raised by the core and mapped to HTTP status codes by the API."""

from __future__ import annotations

from typing import Any


class CoreError(Exception):
    status_code = 400

    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.details = details or {}


class NotFound(CoreError):
    status_code = 404


class Conflict(CoreError):
    status_code = 409


class Invalid(CoreError):
    status_code = 422


class SafetyViolation(CoreError):
    """A hard safety limit rejected the request (restricted zone, e-stop …)."""

    status_code = 403


class ConfirmationRequired(CoreError):
    """The operation is dangerous; it was parked as a pending action."""

    status_code = 202

    def __init__(self, message: str, action: dict[str, Any]) -> None:
        super().__init__(message, {"confirmation": action})
        self.action = action


class NotImplementedCapability(CoreError):
    status_code = 501
