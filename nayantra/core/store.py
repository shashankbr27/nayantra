"""
nayantra/core/store.py

SQLite persistence for the Nayantra Core.

Registry objects (maps, waypoints, lanes, zones, fleets, robots, tasks) are
stored as JSON documents keyed by (collection, id). Registries are small (a
few thousand rows at most), so the WorldModel keeps them in memory and writes
through on every change. Events are append-only in their own table.

The core runs on a single asyncio loop, so one connection is shared; WAL mode
keeps readers (e.g. `sqlite3` CLI for debugging) from blocking writes.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger("nayantra.core.store")

COLLECTIONS = ("maps", "waypoints", "lanes", "zones", "fleets", "robots", "tasks")


class DocumentStore:
    def __init__(self, path: str | Path = ":memory:") -> None:
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        if self.path != ":memory:":
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS documents (
                collection TEXT NOT NULL,
                id         TEXT NOT NULL,
                map_id     TEXT,
                data       TEXT NOT NULL,
                updated_at REAL NOT NULL,
                PRIMARY KEY (collection, id)
            );
            CREATE INDEX IF NOT EXISTS idx_documents_map ON documents(collection, map_id);

            CREATE TABLE IF NOT EXISTS events (
                id        INTEGER PRIMARY KEY,
                ts        REAL NOT NULL,
                type      TEXT NOT NULL,
                severity  TEXT NOT NULL,
                robot_id  TEXT,
                task_id   TEXT,
                data      TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_events_robot ON events(robot_id, id);
            CREATE INDEX IF NOT EXISTS idx_events_task ON events(task_id, id);

            CREATE TABLE IF NOT EXISTS meta (
                key   TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            """
        )

    # ------------------------------------------------------------------
    # Documents
    # ------------------------------------------------------------------

    def put(self, collection: str, doc_id: str, data: dict[str, Any], map_id: str | None = None):
        payload = json.dumps(data, separators=(",", ":"))
        with self._lock:
            self._conn.execute(
                "INSERT INTO documents (collection, id, map_id, data, updated_at) "
                "VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(collection, id) DO UPDATE SET "
                "map_id=excluded.map_id, data=excluded.data, updated_at=excluded.updated_at",
                (collection, doc_id, map_id, payload, time.time()),
            )

    def get(self, collection: str, doc_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT data FROM documents WHERE collection=? AND id=?", (collection, doc_id)
            ).fetchone()
        return json.loads(row["data"]) if row else None

    def list(self, collection: str, map_id: str | None = None) -> list[dict[str, Any]]:
        with self._lock:
            if map_id is None:
                rows = self._conn.execute(
                    "SELECT data FROM documents WHERE collection=? ORDER BY rowid", (collection,)
                ).fetchall()
            else:
                rows = self._conn.execute(
                    "SELECT data FROM documents WHERE collection=? AND map_id=? ORDER BY rowid",
                    (collection, map_id),
                ).fetchall()
        return [json.loads(r["data"]) for r in rows]

    def delete(self, collection: str, doc_id: str) -> bool:
        with self._lock:
            cur = self._conn.execute(
                "DELETE FROM documents WHERE collection=? AND id=?", (collection, doc_id)
            )
        return cur.rowcount > 0

    def count(self, collection: str) -> int:
        with self._lock:
            return self._conn.execute(
                "SELECT COUNT(*) FROM documents WHERE collection=?", (collection,)
            ).fetchone()[0]

    def clear(self) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM documents")
            self._conn.execute("DELETE FROM events")
            self._conn.execute("DELETE FROM meta")

    # ------------------------------------------------------------------
    # Events
    # ------------------------------------------------------------------

    def append_event(self, event: dict[str, Any]) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO events (id, ts, type, severity, robot_id, task_id, data) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    event["id"],
                    event["ts"],
                    event["type"],
                    event["severity"],
                    event.get("robot_id"),
                    event.get("task_id"),
                    json.dumps(event, separators=(",", ":")),
                ),
            )

    def events(
        self,
        limit: int = 200,
        before_id: int | None = None,
        robot_id: str | None = None,
        task_id: str | None = None,
    ) -> list[dict[str, Any]]:
        clauses, args = [], []
        if before_id is not None:
            clauses.append("id < ?")
            args.append(before_id)
        if robot_id:
            clauses.append("robot_id = ?")
            args.append(robot_id)
        if task_id:
            clauses.append("task_id = ?")
            args.append(task_id)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        with self._lock:
            rows = self._conn.execute(
                f"SELECT data FROM events {where} ORDER BY id DESC LIMIT ?", (*args, limit)
            ).fetchall()
        return [json.loads(r["data"]) for r in rows]

    def max_event_id(self) -> int:
        with self._lock:
            row = self._conn.execute("SELECT MAX(id) FROM events").fetchone()
        return int(row[0] or 0)

    def trim_events(self, keep: int = 50_000) -> None:
        with self._lock:
            self._conn.execute(
                "DELETE FROM events WHERE id <= (SELECT MAX(id) FROM events) - ?", (keep,)
            )

    # ------------------------------------------------------------------
    # Meta
    # ------------------------------------------------------------------

    def get_meta(self, key: str) -> str | None:
        with self._lock:
            row = self._conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row["value"] if row else None

    def set_meta(self, key: str, value: str) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO meta (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, value),
            )

    def close(self) -> None:
        with self._lock:
            self._conn.close()
