"""SQLite-backed queue management.

Replaces queue.md with data/queue.db for transactional updates and event history.
Automatically imports queue.md on first run if present.
"""

from __future__ import annotations

import asyncio
import re
import sqlite3
from datetime import datetime
from pathlib import Path

DATA_DIR = Path("data")
QUEUE_FILE = DATA_DIR / "queue.md"  # legacy import source
DB_FILE = DATA_DIR / "queue.db"

STATUS_CHAR_MAP = {
    " ": "pending",
    "↓": "downloading",
    "p": "processing",
    "x": "done",
    "!": "download_failed",
    "e": "process_failed",
    "d": "deleted",
}

_VALID_STATUSES = {
    "pending",
    "downloading",
    "processing",
    "done",
    "download_failed",
    "process_failed",
    "deleted",
}

QUEUE_RE = re.compile(r"^- \[( |↓|p|x|!|e|d)\] (.+)$")

# Kept for compatibility with existing imports.
queue_lock = asyncio.Lock()


def _now() -> str:
    return datetime.utcnow().replace(microsecond=0).isoformat() + "Z"


def _connect() -> sqlite3.Connection:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_FILE, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def _init_db():
    with _connect() as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=30000")
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS queue_items (
              video_id TEXT PRIMARY KEY,
              status TEXT NOT NULL,
              upload_date TEXT,
              channel TEXT NOT NULL,
              title TEXT NOT NULL,
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL,
              priority INTEGER NOT NULL DEFAULT 0,
              attempt_download INTEGER NOT NULL DEFAULT 0,
              attempt_process INTEGER NOT NULL DEFAULT 0,
              last_error TEXT
            );

            CREATE TABLE IF NOT EXISTS queue_events (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              video_id TEXT NOT NULL,
              at TEXT NOT NULL,
              actor TEXT NOT NULL,
              from_status TEXT,
              to_status TEXT,
              message TEXT
            );

            CREATE INDEX IF NOT EXISTS idx_queue_status_priority_created
              ON queue_items(status, priority DESC, created_at ASC);
            CREATE INDEX IF NOT EXISTS idx_queue_channel_status
              ON queue_items(channel, status);
            CREATE INDEX IF NOT EXISTS idx_queue_updated
              ON queue_items(updated_at DESC);
            CREATE INDEX IF NOT EXISTS idx_events_video_at
              ON queue_events(video_id, at DESC);
            CREATE INDEX IF NOT EXISTS idx_events_at
              ON queue_events(at DESC);
            """
        )


def _append_event(
    conn: sqlite3.Connection,
    video_id: str,
    actor: str,
    from_status: str | None,
    to_status: str | None,
    message: str | None = None,
):
    conn.execute(
        """
        INSERT INTO queue_events(video_id, at, actor, from_status, to_status, message)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (video_id, _now(), actor, from_status, to_status, message),
    )


def _import_legacy_queue_if_needed():
    if not QUEUE_FILE.exists():
        return

    with _connect() as conn:
        count = conn.execute("SELECT COUNT(*) FROM queue_items").fetchone()[0]
        if count > 0:
            return

        lines = QUEUE_FILE.read_text().splitlines()
        for line in lines:
            m = QUEUE_RE.match(line)
            if not m:
                continue
            status_char = m.group(1)
            status = STATUS_CHAR_MAP.get(status_char, "pending")
            parts = [p.strip() for p in m.group(2).split("|")]
            if len(parts) < 4:
                continue
            upload_date = parts[0]
            channel = parts[1]
            title = " | ".join(parts[2:-1])
            video_id = parts[-1]
            if not video_id:
                continue
            ts = _now()
            conn.execute(
                """
                INSERT OR IGNORE INTO queue_items(
                    video_id, status, upload_date, channel, title, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (video_id, status, upload_date, channel, title, ts, ts),
            )
            _append_event(conn, video_id, "migration", None, status, "Imported from queue.md")


def init_queue_db():
    _init_db()
    _import_legacy_queue_if_needed()


def parse_queue() -> list[dict]:
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT video_id, status, upload_date, channel, title,
                   created_at, updated_at, last_error,
                   attempt_download, attempt_process
            FROM queue_items
            ORDER BY created_at ASC
            """
        ).fetchall()

    entries: list[dict] = []
    for i, r in enumerate(rows):
        date = r["upload_date"] or ""
        entries.append(
            {
                "line_num": i,
                "status": r["status"],
                "date": date,
                "channel": r["channel"],
                "title": r["title"],
                "video_id": r["video_id"],
                "created_at": r["created_at"],
                "updated_at": r["updated_at"],
                "last_error": r["last_error"],
                "attempt_download": r["attempt_download"],
                "attempt_process": r["attempt_process"],
            }
        )
    return entries


def get_pending() -> list[dict]:
    return [e for e in parse_queue() if e["status"] == "pending"]


def get_by_status(status: str) -> list[dict]:
    if status not in _VALID_STATUSES:
        return []
    return [e for e in parse_queue() if e["status"] == status]


def read_queue_ids() -> set[str]:
    with _connect() as conn:
        rows = conn.execute("SELECT video_id FROM queue_items").fetchall()
    return {r["video_id"] for r in rows}


def enqueue_pending(upload_date: str, channel: str, title: str, video_id: str, actor: str = "scanner") -> bool:
    safe_channel = channel.replace("|", "-")
    safe_title = title.replace("|", "-")
    ts = _now()

    with _connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        cur = conn.execute(
            """
            INSERT OR IGNORE INTO queue_items(
                video_id, status, upload_date, channel, title, created_at, updated_at, priority
            ) VALUES (?, 'pending', ?, ?, ?, ?, ?, 0)
            """,
            (video_id, upload_date, safe_channel, safe_title, ts, ts),
        )
        if cur.rowcount == 1:
            _append_event(conn, video_id, actor, None, "pending", "Queued")
            conn.commit()
            return True
        conn.rollback()
        return False


def claim_next_pending(actor: str = "downloader") -> dict | None:
    with _connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            """
            SELECT video_id, status, upload_date, channel, title
            FROM queue_items
            WHERE status = 'pending'
            ORDER BY priority DESC, created_at ASC
            LIMIT 1
            """
        ).fetchone()
        if not row:
            conn.rollback()
            return None

        conn.execute(
            """
            UPDATE queue_items
            SET status = 'downloading', updated_at = ?, priority = 0
            WHERE video_id = ?
            """,
            (_now(), row["video_id"]),
        )
        _append_event(conn, row["video_id"], actor, "pending", "downloading", "Claimed for download")
        conn.commit()

        return {
            "video_id": row["video_id"],
            "status": "downloading",
            "date": row["upload_date"] or "",
            "channel": row["channel"],
            "title": row["title"],
        }


def prioritize_pending(video_id: str, actor: str = "api") -> bool:
    with _connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT status FROM queue_items WHERE video_id = ?",
            (video_id,),
        ).fetchone()
        if not row or row["status"] != "pending":
            conn.rollback()
            return False

        ts = _now()
        conn.execute("UPDATE queue_items SET priority = 0 WHERE status = 'pending'")
        conn.execute(
            "UPDATE queue_items SET priority = 1, updated_at = ? WHERE video_id = ?",
            (ts, video_id),
        )
        _append_event(conn, video_id, actor, "pending", "pending", "Moved to top of pending queue")
        conn.commit()
        return True


def _set_status(video_id: str, new_status: str, actor: str = "system", message: str | None = None) -> bool:
    if new_status not in _VALID_STATUSES:
        return False
    with _connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT status FROM queue_items WHERE video_id = ?",
            (video_id,),
        ).fetchone()
        if not row:
            conn.rollback()
            return False
        old_status = row["status"]
        conn.execute(
            "UPDATE queue_items SET status = ?, updated_at = ?, priority = 0 WHERE video_id = ?",
            (new_status, _now(), video_id),
        )
        _append_event(conn, video_id, actor, old_status, new_status, message)
        conn.commit()
        return True


def record_download_success(video_id: str, actor: str = "downloader") -> bool:
    with _connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT status FROM queue_items WHERE video_id = ?",
            (video_id,),
        ).fetchone()
        if not row:
            conn.rollback()
            return False
        old_status = row["status"]
        conn.execute(
            """
            UPDATE queue_items
            SET status = 'processing',
                updated_at = ?,
                priority = 0,
                attempt_download = 0,
                last_error = NULL
            WHERE video_id = ?
            """,
            (_now(), video_id),
        )
        _append_event(conn, video_id, actor, old_status, "processing", "Download complete")
        conn.commit()
        return True


def record_download_failure(video_id: str, error: str, actor: str = "downloader") -> str | None:
    with _connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT status, attempt_download FROM queue_items WHERE video_id = ?",
            (video_id,),
        ).fetchone()
        if not row:
            conn.rollback()
            return None

        attempts = int(row["attempt_download"]) + 1
        new_status = "download_failed" if attempts >= 3 else "pending"
        msg = (error or "download failed")[:800]

        conn.execute(
            """
            UPDATE queue_items
            SET status = ?,
                updated_at = ?,
                priority = 0,
                attempt_download = ?,
                last_error = ?
            WHERE video_id = ?
            """,
            (new_status, _now(), attempts, msg, video_id),
        )
        _append_event(conn, video_id, actor, row["status"], new_status, msg)
        conn.commit()
        return new_status


def record_process_success(video_id: str, actor: str = "processor") -> bool:
    with _connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT status FROM queue_items WHERE video_id = ?",
            (video_id,),
        ).fetchone()
        if not row:
            conn.rollback()
            return False
        old_status = row["status"]
        conn.execute(
            """
            UPDATE queue_items
            SET status = 'done',
                updated_at = ?,
                attempt_process = 0,
                last_error = NULL
            WHERE video_id = ?
            """,
            (_now(), video_id),
        )
        _append_event(conn, video_id, actor, old_status, "done", "Post-processing complete")
        conn.commit()
        return True


def record_process_failure(video_id: str, error: str, actor: str = "processor") -> str | None:
    with _connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT status, attempt_process FROM queue_items WHERE video_id = ?",
            (video_id,),
        ).fetchone()
        if not row:
            conn.rollback()
            return None

        attempts = int(row["attempt_process"]) + 1
        new_status = "process_failed" if attempts >= 3 else "processing"
        msg = (error or "post-process failed")[:800]

        conn.execute(
            """
            UPDATE queue_items
            SET status = ?,
                updated_at = ?,
                attempt_process = ?,
                last_error = ?
            WHERE video_id = ?
            """,
            (new_status, _now(), attempts, msg, video_id),
        )
        _append_event(conn, video_id, actor, row["status"], new_status, msg)
        conn.commit()
        return new_status


def get_queue_events(limit: int = 100) -> list[dict]:
    limit = max(1, min(limit, 1000))
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT e.id, e.video_id, e.at, e.actor, e.from_status, e.to_status, e.message,
                   q.title
            FROM queue_events e
            LEFT JOIN queue_items q ON q.video_id = e.video_id
            ORDER BY e.id DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()

    return [
        {
            "id": r["id"],
            "video_id": r["video_id"],
            "title": r["title"],
            "at": r["at"],
            "actor": r["actor"],
            "from_status": r["from_status"],
            "to_status": r["to_status"],
            "message": r["message"],
        }
        for r in rows
    ]


def get_dead_letter(
    limit: int = 200,
    status: str | None = None,
    channel: str | None = None,
    query: str | None = None,
) -> list[dict]:
    allowed = {"download_failed", "process_failed"}
    if status and status not in allowed:
        return []
    limit = max(1, min(limit, 1000))

    sql = """
        SELECT video_id, status, upload_date, channel, title, updated_at,
               attempt_download, attempt_process, last_error
        FROM queue_items
        WHERE status IN ('download_failed', 'process_failed')
    """
    params: list[str | int] = []
    if status:
        sql += " AND status = ?"
        params.append(status)
    if channel:
        sql += " AND channel = ?"
        params.append(channel)
    if query:
        sql += " AND (LOWER(title) LIKE ? OR LOWER(video_id) LIKE ?)"
        q = f"%{query.lower()}%"
        params.extend([q, q])
    sql += " ORDER BY updated_at DESC LIMIT ?"
    params.append(limit)

    with _connect() as conn:
        rows = conn.execute(sql, tuple(params)).fetchall()

    return [
        {
            "video_id": r["video_id"],
            "status": r["status"],
            "date": r["upload_date"] or "",
            "channel": r["channel"],
            "title": r["title"],
            "updated_at": r["updated_at"],
            "attempt_download": int(r["attempt_download"] or 0),
            "attempt_process": int(r["attempt_process"] or 0),
            "last_error": r["last_error"],
        }
        for r in rows
    ]


def retry_failed(video_id: str, actor: str = "api") -> str | None:
    with _connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT status FROM queue_items WHERE video_id = ?",
            (video_id,),
        ).fetchone()
        if not row:
            conn.rollback()
            return None

        old_status = row["status"]
        if old_status == "download_failed":
            new_status = "pending"
            conn.execute(
                """
                UPDATE queue_items
                SET status = 'pending', updated_at = ?, priority = 0,
                    attempt_download = 0, last_error = NULL
                WHERE video_id = ?
                """,
                (_now(), video_id),
            )
        elif old_status == "process_failed":
            new_status = "processing"
            conn.execute(
                """
                UPDATE queue_items
                SET status = 'processing', updated_at = ?,
                    attempt_process = 0, last_error = NULL
                WHERE video_id = ?
                """,
                (_now(), video_id),
            )
        else:
            conn.rollback()
            return None

        _append_event(conn, video_id, actor, old_status, new_status, "Manual retry")
        conn.commit()
        return new_status


def bulk_retry_failed(video_ids: list[str], actor: str = "api") -> list[dict]:
    results: list[dict] = []
    for vid in video_ids:
        new_status = retry_failed(vid, actor=actor)
        results.append(
            {
                "video_id": vid,
                "ok": bool(new_status),
                "new_status": new_status,
            }
        )
    return results


def bulk_mark_deleted(video_ids: list[str], actor: str = "api") -> list[dict]:
    results: list[dict] = []
    for vid in video_ids:
        ok = _set_status(vid, "deleted", actor=actor, message="Bulk delete")
        results.append({"video_id": vid, "ok": ok})
    return results


async def mark_done(video_id: str):
    _set_status(video_id, "done")


async def mark_deleted(video_id: str, actor: str = "system", reason: str | None = None):
    with _connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT status FROM queue_items WHERE video_id = ?", (video_id,)
        ).fetchone()
        if not row:
            conn.rollback()
            return
        old_status = row["status"]
        conn.execute(
            "UPDATE queue_items SET status = 'deleted', updated_at = ?, priority = 0, last_error = ? WHERE video_id = ?",
            (_now(), reason, video_id),
        )
        _append_event(conn, video_id, actor, old_status, "deleted", reason)
        conn.commit()


async def mark_pending(video_id: str):
    _set_status(video_id, "pending")


async def mark_failed(video_id: str):
    _set_status(video_id, "download_failed")


async def mark_downloading(video_id: str):
    _set_status(video_id, "downloading")


async def mark_processing(video_id: str):
    _set_status(video_id, "processing")


async def mark_process_failed(video_id: str):
    _set_status(video_id, "process_failed")


async def mark_cancelled(video_id: str):
    _set_status(video_id, "deleted")


async def reset_interrupted():
    with _connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        rows = conn.execute(
            "SELECT video_id FROM queue_items WHERE status = 'downloading'"
        ).fetchall()
        ts = _now()
        conn.execute(
            "UPDATE queue_items SET status = 'pending', updated_at = ?, priority = 0 WHERE status = 'downloading'",
            (ts,),
        )
        for r in rows:
            _append_event(
                conn,
                r["video_id"],
                "system",
                "downloading",
                "pending",
                "Reset interrupted download on startup",
            )
        conn.commit()


# Initialize eagerly so every process can rely on DB presence.
init_queue_db()
