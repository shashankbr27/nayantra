"""
nayantra/core/confirmations.py

Pending dangerous actions ("Stop all robots?", "Delete map?", "Send robot into
restricted area?").

Any caller may *request* a dangerous action. It is parked here and executes
only when an operator confirms it (the UI's Confirm button, or an API call
with confirm=true from a non-MCP client). The MCP layer has no confirm tool, so
an LLM can never approve its own request.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any

from nayantra.core.errors import NotFound
from nayantra.core.events import EventBus
from nayantra.core.models import EventType, PendingAction, Severity, TaskTrace, new_id

logger = logging.getLogger("nayantra.core.confirm")

Handler = Callable[[dict[str, Any]], Awaitable[Any]]
TTL_S = 180.0


class Confirmations:
    def __init__(self, bus: EventBus) -> None:
        self.bus = bus
        self.handlers: dict[str, Handler] = {}
        self.actions: dict[str, PendingAction] = {}

    def register(self, kind: str, handler: Handler) -> None:
        self.handlers[kind] = handler

    def request(
        self,
        kind: str,
        summary: str,
        params: dict[str, Any],
        detail: str = "",
        requested_by: str = "operator",
        trace: TaskTrace | None = None,
    ) -> PendingAction:
        if kind not in self.handlers:
            raise ValueError(f"unknown confirmation kind {kind!r}")
        action = PendingAction(
            id=new_id("confirm"),
            kind=kind,
            summary=summary,
            detail=detail,
            params=params,
            requested_by=requested_by,
            trace=trace or TaskTrace(),
            expires_at=time.time() + TTL_S,
        )
        self.actions[action.id] = action
        self.bus.emit(
            EventType.CONFIRMATION_REQUESTED,
            f"Confirmation needed: {summary} (requested by {requested_by})",
            Severity.WARNING,
            data={"confirmation": action.model_dump(mode="json")},
        )
        self.bus.publish("confirmation", action.model_dump(mode="json"))
        return action

    def get(self, action_id: str) -> PendingAction:
        self.expire()
        action = self.actions.get(action_id)
        if action is None:
            raise NotFound(f"Unknown confirmation {action_id!r}")
        return action

    async def confirm(self, action_id: str, by: str = "operator") -> PendingAction:
        action = self.get(action_id)
        if action.status != "pending":
            return action
        action.status = "confirmed"
        action.resolved_at = time.time()
        try:
            action.result = await self.handlers[action.kind](action.params)
            action.status = "executed"
            msg = f"Confirmed by {by}: {action.summary}"
        except Exception as exc:  # noqa: BLE001
            logger.exception(f"confirmed action {action.kind} failed")
            action.status = "failed"
            action.result = {"error": getattr(exc, "message", str(exc))}
            msg = f"Confirmed action failed: {action.summary} — {action.result['error']}"
        self.bus.emit(EventType.CONFIRMATION_RESOLVED, msg, data={"confirmation_id": action.id})
        self.bus.publish("confirmation", action.model_dump(mode="json"))
        return action

    def reject(self, action_id: str, by: str = "operator") -> PendingAction:
        action = self.get(action_id)
        if action.status == "pending":
            action.status = "rejected"
            action.resolved_at = time.time()
            self.bus.emit(
                EventType.CONFIRMATION_RESOLVED,
                f"Rejected by {by}: {action.summary}",
                data={"confirmation_id": action.id},
            )
            self.bus.publish("confirmation", action.model_dump(mode="json"))
        return action

    def expire(self) -> None:
        now = time.time()
        for action in self.actions.values():
            if action.status == "pending" and action.expires_at < now:
                action.status = "expired"
                action.resolved_at = now
                self.bus.publish("confirmation", action.model_dump(mode="json"))

    def list(self, pending_only: bool = True) -> list[PendingAction]:
        self.expire()
        items = [a for a in self.actions.values() if not pending_only or a.status == "pending"]
        return sorted(items, key=lambda a: a.created_at, reverse=True)
