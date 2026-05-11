"""
nayantra/agent/history.py

Mission history persistence layer.

Stores every MissionResult in a local SQLite database so that:
  - Users can review past commands and their outcomes
  - The system can detect repeated failures on the same command
  - Analytics / dashboards can query mission success rates over time

Schema:
  missions (id TEXT PK, command TEXT, summary TEXT, success INT,
            step_count INT, created_at REAL)
  steps    (mission_id TEXT FK, step_index INT, tool TEXT,
            status TEXT, duration_ms REAL, error TEXT, result_json TEXT)

Usage:
    store = MissionStore()          # uses default DB path
    await store.save(mission)
    recent = await store.recent(limit=20)
    stats  = await store.stats()
"""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
import time
from pathlib import Path
from typing import Any

from nayantra.agent.models import MissionResult

logger = logging.getLogger("rmf.history")

DEFAULT_DB_PATH = Path(__file__).resolve().parents[2] / "data" / "missions.db"


class MissionStore:
    """Async-friendly SQLite store for mission history."""

    def __init__(self, db_path: Path | None = None) -> None:
        self._db_path = db_path or DEFAULT_DB_PATH
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    # ------------------------------------------------------------------
    # Schema initialisation
    # ------------------------------------------------------------------

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS missions (
                    id          TEXT PRIMARY KEY,
                    command     TEXT NOT NULL,
                    summary     TEXT,
                    success     INTEGER NOT NULL DEFAULT 0,
                    step_count  INTEGER NOT NULL DEFAULT 0,
                    created_at  REAL NOT NULL
                );

                CREATE TABLE IF NOT EXISTS steps (
                    mission_id  TEXT NOT NULL REFERENCES missions(id) ON DELETE CASCADE,
                    step_index  INTEGER NOT NULL,
                    tool        TEXT NOT NULL,
                    status      TEXT NOT NULL,
                    duration_ms REAL,
                    error       TEXT,
                    result_json TEXT,
                    PRIMARY KEY (mission_id, step_index)
                );

                CREATE INDEX IF NOT EXISTS idx_missions_created ON missions(created_at DESC);
                CREATE INDEX IF NOT EXISTS idx_missions_success  ON missions(success);
            """)
        logger.debug(f"Mission DB initialised at {self._db_path}")

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    # ------------------------------------------------------------------
    # Write
    # ------------------------------------------------------------------

    async def save(self, mission: MissionResult) -> None:
        """
        Persist a completed mission and all its steps.

        Concurrent saves are safe: each call runs in its own thread and
        SQLite WAL mode serialises writes at the DB level. The `with conn`
        block makes each save atomic (commit on success, rollback on error).
        """
        await asyncio.to_thread(self._save_sync, mission)

    def _save_sync(self, mission: MissionResult) -> None:
        with self._connect() as conn:
            conn.execute(
                """INSERT OR REPLACE INTO missions
                   (id, command, summary, success, step_count, created_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    mission.mission_id,
                    mission.command,
                    mission.summary,
                    int(mission.success),
                    len(mission.steps),
                    time.time(),
                ),
            )
            for step in mission.steps:
                conn.execute(
                    """INSERT OR REPLACE INTO steps
                       (mission_id, step_index, tool, status, duration_ms, error, result_json)
                       VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (
                        mission.mission_id,
                        step.step_index,
                        step.tool,
                        step.status.value,
                        step.duration_ms,
                        step.error,
                        json.dumps(step.result) if step.result else None,
                    ),
                )
        logger.debug(f"Saved mission {mission.mission_id[:8]}… ({len(mission.steps)} steps)")

    # ------------------------------------------------------------------
    # Read
    # ------------------------------------------------------------------

    async def recent(self, limit: int = 50) -> list[dict[str, Any]]:
        """Return the most recent missions (lightweight, no steps)."""
        return await asyncio.to_thread(self._recent_sync, limit)

    def _recent_sync(self, limit: int) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM missions ORDER BY created_at DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(r) for r in rows]

    async def get(self, mission_id: str) -> dict[str, Any] | None:
        """Return a full mission record including all steps."""
        return await asyncio.to_thread(self._get_sync, mission_id)

    def _get_sync(self, mission_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM missions WHERE id = ?", (mission_id,)).fetchone()
            if not row:
                return None
            mission = dict(row)
            steps = conn.execute(
                "SELECT * FROM steps WHERE mission_id = ? ORDER BY step_index",
                (mission_id,),
            ).fetchall()
            mission["steps"] = [dict(s) for s in steps]
        return mission

    async def search(self, query: str, limit: int = 20) -> list[dict[str, Any]]:
        """Full-text search over mission commands."""
        return await asyncio.to_thread(self._search_sync, query, limit)

    def _search_sync(self, query: str, limit: int) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM missions WHERE command LIKE ? ORDER BY created_at DESC LIMIT ?",
                (f"%{query}%", limit),
            ).fetchall()
        return [dict(r) for r in rows]

    # ------------------------------------------------------------------
    # Analytics
    # ------------------------------------------------------------------

    async def stats(self) -> dict[str, Any]:
        """Return aggregate statistics for the dashboard."""
        return await asyncio.to_thread(self._stats_sync)

    def _stats_sync(self) -> dict[str, Any]:
        with self._connect() as conn:
            total = conn.execute("SELECT COUNT(*) FROM missions").fetchone()[0]
            success = conn.execute("SELECT COUNT(*) FROM missions WHERE success=1").fetchone()[0]
            failed = total - success

            # Most-used tools
            top_tools = conn.execute("""
                SELECT tool, COUNT(*) as cnt
                FROM steps GROUP BY tool ORDER BY cnt DESC LIMIT 5
            """).fetchall()

            # Average mission duration (sum of step durations)
            avg_ms = conn.execute("""
                SELECT AVG(total_ms) FROM (
                    SELECT mission_id, SUM(duration_ms) as total_ms
                    FROM steps GROUP BY mission_id
                )
            """).fetchone()[0]

            # Recent failure rate (last 24h)
            cutoff = time.time() - 86400
            recent_total = conn.execute(
                "SELECT COUNT(*) FROM missions WHERE created_at > ?", (cutoff,)
            ).fetchone()[0]
            recent_failed = conn.execute(
                "SELECT COUNT(*) FROM missions WHERE created_at > ? AND success=0",
                (cutoff,),
            ).fetchone()[0]

        return {
            "total_missions": total,
            "successful": success,
            "failed": failed,
            "success_rate_pct": round(success / total * 100, 1) if total > 0 else 0.0,
            "avg_duration_ms": round(avg_ms or 0, 1),
            "top_tools": [{"tool": r[0], "count": r[1]} for r in top_tools],
            "last_24h_total": recent_total,
            "last_24h_failed": recent_failed,
        }

    async def delete(self, mission_id: str) -> bool:
        """Delete a mission record and its steps."""
        return await asyncio.to_thread(self._delete_sync, mission_id)

    def _delete_sync(self, mission_id: str) -> bool:
        with self._connect() as conn:
            cur = conn.execute("DELETE FROM missions WHERE id = ?", (mission_id,))
        return cur.rowcount > 0

    async def clear(self) -> None:
        """Delete all stored missions. Irreversible."""
        await asyncio.to_thread(self._clear_sync)

    def _clear_sync(self) -> None:
        with self._connect() as conn:
            conn.execute("DELETE FROM steps")
            conn.execute("DELETE FROM missions")
        logger.warning("Mission history cleared")
