"""SQLite-backed runtime state shared by API and worker processes."""

from __future__ import annotations

from datetime import datetime, timezone
import json
import sqlite3
from pathlib import Path
from typing import Any

DATA_DIR = Path("data")
DB_FILE = DATA_DIR / "queue.db"
STATE_KEY = "state"


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _default_state() -> dict[str, Any]:
    return {
        "started_at": _utcnow_iso(),
        "workers": {
            "scanner": {"last_heartbeat": None, "active_video_id": None},
            "downloader": {"last_heartbeat": None, "active_video_id": None},
            "processor": {"last_heartbeat": None, "active_video_id": None},
            "cleanup": {"last_heartbeat": None, "active_video_id": None},
        },
        "scanner": {
            "last_scan_at": None,
            "next_scan_at": None,
            "last_scan_added": 0,
            "last_scan_error": None,
            "is_scanning": False,
            "scan_started_at": None,
        },
        "cleanup": {
            "last_cleanup_at": None,
            "next_cleanup_at": None,
            "last_cleanup_deleted": 0,
            "last_cleanup_error": None,
        },
    }


def _connect() -> sqlite3.Connection:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_FILE, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def _init_db():
    with _connect() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS runtime_state (
              key TEXT PRIMARY KEY,
              value_json TEXT NOT NULL,
              updated_at TEXT NOT NULL
            )
            """
        )
        row = conn.execute(
            "SELECT 1 FROM runtime_state WHERE key = ?",
            (STATE_KEY,),
        ).fetchone()
        if not row:
            conn.execute(
                "INSERT INTO runtime_state(key, value_json, updated_at) VALUES (?, ?, ?)",
                (STATE_KEY, json.dumps(_default_state()), _utcnow_iso()),
            )
        conn.commit()


def _load_state(conn: sqlite3.Connection) -> dict[str, Any]:
    row = conn.execute(
        "SELECT value_json FROM runtime_state WHERE key = ?",
        (STATE_KEY,),
    ).fetchone()
    if not row:
        data = _default_state()
        conn.execute(
            "INSERT INTO runtime_state(key, value_json, updated_at) VALUES (?, ?, ?)",
            (STATE_KEY, json.dumps(data), _utcnow_iso()),
        )
        return data
    try:
        return json.loads(row["value_json"])
    except json.JSONDecodeError:
        data = _default_state()
        conn.execute(
            "UPDATE runtime_state SET value_json = ?, updated_at = ? WHERE key = ?",
            (json.dumps(data), _utcnow_iso(), STATE_KEY),
        )
        return data


def _save_state(conn: sqlite3.Connection, data: dict[str, Any]):
    conn.execute(
        "UPDATE runtime_state SET value_json = ?, updated_at = ? WHERE key = ?",
        (json.dumps(data), _utcnow_iso(), STATE_KEY),
    )


def _mutate(mutator):
    with _connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        data = _load_state(conn)
        mutator(data)
        _save_state(conn, data)
        conn.commit()


def touch_worker(worker: str):
    def _apply(data: dict[str, Any]):
        data.setdefault("workers", {})
        data["workers"].setdefault(worker, {})
        data["workers"][worker]["last_heartbeat"] = _utcnow_iso()
    _mutate(_apply)


def set_worker(worker: str, **fields: Any):
    def _apply(data: dict[str, Any]):
        data.setdefault("workers", {})
        data["workers"].setdefault(worker, {})
        data["workers"][worker].update(fields)
    _mutate(_apply)


def set_component(name: str, **fields: Any):
    def _apply(data: dict[str, Any]):
        data.setdefault(name, {})
        data[name].update(fields)
    _mutate(_apply)


def snapshot() -> dict[str, Any]:
    with _connect() as conn:
        data = _load_state(conn)
    data["now"] = _utcnow_iso()
    return data


_init_db()
