"""
nayantra/core/events.py

Event bus + alerts + realtime fan-out.

Two kinds of traffic flow through here:

  * **Events** — durable, human-readable facts ("UGV-03 delayed 4.2 s at
    Intersection 2"). Persisted, queryable by robot/task, shown in the
    dashboard's event log. These are the observability trail.
  * **Messages** — realtime state pushes for the WebSocket (robot poses at
    10 Hz, task snapshots, traffic state, registry changes). Not persisted.

Subscribers get an asyncio.Queue. A slow subscriber never blocks the core:
when its queue is full the oldest message is dropped and the subscriber is
flagged for a full resync.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any

from nayantra.core.models import Alert, Event, EventType, Severity, new_id
from nayantra.core.store import DocumentStore

logger = logging.getLogger("nayantra.core.events")


@dataclass
class Subscription:
    queue: asyncio.Queue = field(default_factory=lambda: asyncio.Queue(maxsize=2000))
    needs_resync: bool = False

    def push(self, message: dict[str, Any]) -> None:
        try:
            self.queue.put_nowait(message)
        except asyncio.QueueFull:
            try:
                self.queue.get_nowait()
            except asyncio.QueueEmpty:
                pass
            self.needs_resync = True
            try:
                self.queue.put_nowait(message)
            except asyncio.QueueFull:
                pass


class EventBus:
    def __init__(self, store: DocumentStore | None = None, history: int = 2000) -> None:
        self._store = store
        self._seq = store.max_event_id() if store else 0
        self._recent: deque[Event] = deque(maxlen=history)
        self._subs: list[Subscription] = []
        self._alerts: dict[str, Alert] = {}  # key -> alert
        self._listeners: list = []  # sync callbacks(event) — e.g. alert rules
        if store:
            for raw in reversed(store.events(limit=history)):
                try:
                    self._recent.append(Event.model_validate(raw))
                except Exception:  # noqa: BLE001 — tolerate old rows
                    continue

    # ------------------------------------------------------------------
    # Subscriptions
    # ------------------------------------------------------------------

    def subscribe(self) -> Subscription:
        sub = Subscription()
        self._subs.append(sub)
        return sub

    def unsubscribe(self, sub: Subscription) -> None:
        if sub in self._subs:
            self._subs.remove(sub)

    @property
    def subscriber_count(self) -> int:
        return len(self._subs)

    def publish(self, msg_type: str, data: Any) -> None:
        """Realtime (non-persisted) message to every subscriber."""
        message = {"type": msg_type, "data": data, "ts": time.time()}
        for sub in list(self._subs):
            sub.push(message)

    def on_event(self, callback) -> None:
        self._listeners.append(callback)

    # ------------------------------------------------------------------
    # Events
    # ------------------------------------------------------------------

    def emit(
        self,
        type: EventType | str,
        message: str,
        severity: Severity | str = Severity.INFO,
        *,
        robot_id: str | None = None,
        fleet_id: str | None = None,
        task_id: str | None = None,
        map_id: str | None = None,
        data: dict[str, Any] | None = None,
    ) -> Event:
        self._seq += 1
        event = Event(
            id=self._seq,
            ts=time.time(),
            type=EventType(type),
            severity=Severity(severity),
            message=message,
            robot_id=robot_id,
            fleet_id=fleet_id,
            task_id=task_id,
            map_id=map_id,
            data=data or {},
        )
        self._recent.append(event)
        if self._store:
            try:
                self._store.append_event(event.model_dump(mode="json"))
            except Exception as exc:  # noqa: BLE001 — never lose the loop over logging
                logger.error(f"event persist failed: {exc}")
        log = logger.warning if event.severity in ("warning", "error", "critical") else logger.info
        log(f"[{event.type}] {message}")
        self.publish("event", event.model_dump(mode="json"))
        for cb in self._listeners:
            try:
                cb(event)
            except Exception as exc:  # noqa: BLE001
                logger.error(f"event listener failed: {exc}")
        return event

    def recent(
        self,
        limit: int = 200,
        robot_id: str | None = None,
        task_id: str | None = None,
        types: set[str] | None = None,
        min_severity: str | None = None,
    ) -> list[Event]:
        order = {"info": 0, "warning": 1, "error": 2, "critical": 3}
        floor = order.get(min_severity or "info", 0)
        out: list[Event] = []
        for ev in reversed(self._recent):
            if robot_id and ev.robot_id != robot_id:
                continue
            if task_id and ev.task_id != task_id:
                continue
            if types and ev.type not in types:
                continue
            if order.get(ev.severity, 0) < floor:
                continue
            out.append(ev)
            if len(out) >= limit:
                break
        if len(out) < limit and self._store and (robot_id or task_id):
            # Fall back to the durable log for older history.
            seen = {e.id for e in out}
            for raw in self._store.events(limit=limit, robot_id=robot_id, task_id=task_id):
                if raw["id"] not in seen:
                    out.append(Event.model_validate(raw))
            out.sort(key=lambda e: e.id, reverse=True)
            out = out[:limit]
        return out

    # ------------------------------------------------------------------
    # Alerts — deduplicated by key, raised/resolved by the subsystems that
    # own the condition (battery monitor, localization watchdog, …).
    # ------------------------------------------------------------------

    def raise_alert(
        self,
        key: str,
        severity: Severity | str,
        title: str,
        message: str,
        robot_id: str | None = None,
        task_id: str | None = None,
    ) -> Alert:
        existing = self._alerts.get(key)
        if existing and existing.status != "resolved":
            existing.message = message
            existing.severity = Severity(severity)
            existing.updated_at = time.time()
            self.publish("alert", existing.model_dump(mode="json"))
            return existing
        alert = Alert(
            id=new_id("alert"),
            key=key,
            severity=Severity(severity),
            title=title,
            message=message,
            robot_id=robot_id,
            task_id=task_id,
        )
        self._alerts[key] = alert
        self.publish("alert", alert.model_dump(mode="json"))
        return alert

    def resolve_alert(self, key: str) -> None:
        alert = self._alerts.get(key)
        if alert and alert.status != "resolved":
            alert.status = "resolved"
            alert.updated_at = time.time()
            self.publish("alert", alert.model_dump(mode="json"))

    def acknowledge_alert(self, alert_id: str) -> Alert | None:
        for alert in self._alerts.values():
            if alert.id == alert_id:
                if alert.status == "active":
                    alert.status = "acknowledged"
                    alert.updated_at = time.time()
                    self.publish("alert", alert.model_dump(mode="json"))
                return alert
        return None

    def alerts(self, include_resolved: bool = False) -> list[Alert]:
        items = [a for a in self._alerts.values() if include_resolved or a.status != "resolved"]
        return sorted(items, key=lambda a: a.raised_at, reverse=True)
