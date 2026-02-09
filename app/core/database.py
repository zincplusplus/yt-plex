import aiosqlite
from pathlib import Path
from datetime import datetime
from typing import Optional
from .config import settings

DATABASE_PATH = settings.data_dir / "yt-plex.db"


async def get_db() -> aiosqlite.Connection:
    db = await aiosqlite.connect(DATABASE_PATH)
    db.row_factory = aiosqlite.Row
    return db


async def init_db():
    DATABASE_PATH.parent.mkdir(parents=True, exist_ok=True)

    async with aiosqlite.connect(DATABASE_PATH) as db:
        await db.executescript("""
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT
            );

            CREATE TABLE IF NOT EXISTS channels (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT,
                url TEXT UNIQUE,
                channel_id TEXT,
                enabled INTEGER DEFAULT 1,
                scan_interval_hours INTEGER DEFAULT 6,
                download_format TEXT DEFAULT 'bestvideo[ext=mp4][height<=1080]+bestaudio[ext=m4a]/best[ext=mp4]/best',
                sponsorblock_remove TEXT DEFAULT 'sponsor,selfpromo,interaction',
                subtitle_langs TEXT DEFAULT 'en',
                title_filter TEXT,
                description_filter TEXT,
                description_exclude TEXT,
                download_since TEXT,        -- YYYY-MM-DD, only download videos after this date
                delete_after_days INTEGER,
                last_scanned_at TEXT,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS videos (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                channel_id INTEGER,
                video_id TEXT UNIQUE,
                title TEXT,
                description TEXT,
                status TEXT DEFAULT 'pending',
                file_path TEXT,
                error_message TEXT,
                downloaded_at TEXT,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (channel_id) REFERENCES channels(id)
            );

            CREATE INDEX IF NOT EXISTS idx_videos_status ON videos(status);
            CREATE INDEX IF NOT EXISTS idx_videos_channel ON videos(channel_id);

            CREATE TABLE IF NOT EXISTS activity_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_type TEXT NOT NULL,
                title TEXT NOT NULL,
                detail TEXT,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            );
            CREATE INDEX IF NOT EXISTS idx_activity_log_created ON activity_log(created_at);
        """)
        await db.commit()


# Channel operations
async def create_channel(name: str, url: str, channel_id: str, **kwargs) -> int:
    async with aiosqlite.connect(DATABASE_PATH) as db:
        columns = ["name", "url", "channel_id"] + list(kwargs.keys())
        placeholders = ["?"] * len(columns)
        values = [name, url, channel_id] + list(kwargs.values())

        cursor = await db.execute(
            f"INSERT INTO channels ({', '.join(columns)}) VALUES ({', '.join(placeholders)})",
            values
        )
        await db.commit()
        return cursor.lastrowid


async def get_channel(channel_id: int) -> Optional[dict]:
    async with aiosqlite.connect(DATABASE_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("SELECT * FROM channels WHERE id = ?", (channel_id,))
        row = await cursor.fetchone()
        return dict(row) if row else None


async def get_channel_by_url(url: str) -> Optional[dict]:
    async with aiosqlite.connect(DATABASE_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("SELECT * FROM channels WHERE url = ?", (url,))
        row = await cursor.fetchone()
        return dict(row) if row else None


async def get_all_channels() -> list[dict]:
    async with aiosqlite.connect(DATABASE_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("SELECT * FROM channels ORDER BY created_at DESC")
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]


async def get_enabled_channels() -> list[dict]:
    async with aiosqlite.connect(DATABASE_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("SELECT * FROM channels WHERE enabled = 1")
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]


async def update_channel(channel_id: int, **kwargs) -> bool:
    if not kwargs:
        return False
    async with aiosqlite.connect(DATABASE_PATH) as db:
        set_clause = ", ".join(f"{k} = ?" for k in kwargs.keys())
        values = list(kwargs.values()) + [channel_id]
        await db.execute(f"UPDATE channels SET {set_clause} WHERE id = ?", values)
        await db.commit()
        return True


async def delete_channel(channel_id: int) -> bool:
    async with aiosqlite.connect(DATABASE_PATH) as db:
        await db.execute("DELETE FROM videos WHERE channel_id = ?", (channel_id,))
        await db.execute("DELETE FROM channels WHERE id = ?", (channel_id,))
        await db.commit()
        return True


# Video operations
async def create_video(channel_id: int, video_id: str, title: str, description: str = None) -> int:
    async with aiosqlite.connect(DATABASE_PATH) as db:
        cursor = await db.execute(
            "INSERT OR IGNORE INTO videos (channel_id, video_id, title, description) VALUES (?, ?, ?, ?)",
            (channel_id, video_id, title, description)
        )
        await db.commit()
        return cursor.lastrowid


async def get_video(video_id: str) -> Optional[dict]:
    async with aiosqlite.connect(DATABASE_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("SELECT * FROM videos WHERE video_id = ?", (video_id,))
        row = await cursor.fetchone()
        return dict(row) if row else None


async def get_videos_by_status(status: str, limit: int = 100) -> list[dict]:
    async with aiosqlite.connect(DATABASE_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT v.*, c.name as channel_name FROM videos v LEFT JOIN channels c ON v.channel_id = c.id WHERE v.status = ? ORDER BY v.created_at ASC LIMIT ?",
            (status, limit)
        )
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]


async def get_recent_videos(limit: int = 50) -> list[dict]:
    async with aiosqlite.connect(DATABASE_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            """SELECT v.*, c.name as channel_name
               FROM videos v
               LEFT JOIN channels c ON v.channel_id = c.id
               ORDER BY v.created_at DESC LIMIT ?""",
            (limit,)
        )
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]


async def update_video(video_id: str, **kwargs) -> bool:
    if not kwargs:
        return False
    async with aiosqlite.connect(DATABASE_PATH) as db:
        set_clause = ", ".join(f"{k} = ?" for k in kwargs.keys())
        values = list(kwargs.values()) + [video_id]
        await db.execute(f"UPDATE videos SET {set_clause} WHERE video_id = ?", values)
        await db.commit()
        return True


async def get_videos_for_cleanup(delete_after_days: int) -> list[dict]:
    async with aiosqlite.connect(DATABASE_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            """SELECT v.*, c.delete_after_days as channel_delete_days
               FROM videos v
               LEFT JOIN channels c ON v.channel_id = c.id
               WHERE v.status = 'completed' AND v.downloaded_at IS NOT NULL""",
        )
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]


async def delete_video(video_id: str) -> bool:
    async with aiosqlite.connect(DATABASE_PATH) as db:
        await db.execute("DELETE FROM videos WHERE video_id = ?", (video_id,))
        await db.commit()
        return True


# Settings operations
async def get_setting(key: str, default: str = None) -> Optional[str]:
    async with aiosqlite.connect(DATABASE_PATH) as db:
        cursor = await db.execute("SELECT value FROM settings WHERE key = ?", (key,))
        row = await cursor.fetchone()
        return row[0] if row else default


async def set_setting(key: str, value: str) -> bool:
    async with aiosqlite.connect(DATABASE_PATH) as db:
        await db.execute(
            "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
            (key, value)
        )
        await db.commit()
        return True


async def get_all_settings() -> dict:
    async with aiosqlite.connect(DATABASE_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("SELECT key, value FROM settings")
        rows = await cursor.fetchall()
        return {row["key"]: row["value"] for row in rows}


# Stats
async def get_stats() -> dict:
    async with aiosqlite.connect(DATABASE_PATH) as db:
        total_channels = (await (await db.execute("SELECT COUNT(*) FROM channels")).fetchone())[0]
        enabled_channels = (await (await db.execute("SELECT COUNT(*) FROM channels WHERE enabled = 1")).fetchone())[0]
        total_videos = (await (await db.execute("SELECT COUNT(*) FROM videos")).fetchone())[0]
        completed_videos = (await (await db.execute("SELECT COUNT(*) FROM videos WHERE status = 'completed'")).fetchone())[0]
        pending_videos = (await (await db.execute("SELECT COUNT(*) FROM videos WHERE status = 'pending'")).fetchone())[0]
        downloading_videos = (await (await db.execute("SELECT COUNT(*) FROM videos WHERE status = 'downloading'")).fetchone())[0]
        failed_videos = (await (await db.execute("SELECT COUNT(*) FROM videos WHERE status = 'failed'")).fetchone())[0]

        return {
            "total_channels": total_channels,
            "enabled_channels": enabled_channels,
            "total_videos": total_videos,
            "completed_videos": completed_videos,
            "pending_videos": pending_videos,
            "downloading_videos": downloading_videos,
            "failed_videos": failed_videos
        }


# Activity log operations
async def create_log_entry(event_type: str, title: str, detail: str = None):
    async with aiosqlite.connect(DATABASE_PATH) as db:
        await db.execute(
            "INSERT INTO activity_log (event_type, title, detail) VALUES (?, ?, ?)",
            (event_type, title, detail)
        )
        await db.commit()


async def get_log_entries(limit: int = 200) -> list[dict]:
    async with aiosqlite.connect(DATABASE_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT * FROM activity_log ORDER BY created_at DESC LIMIT ?",
            (limit,)
        )
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]


async def clear_log():
    async with aiosqlite.connect(DATABASE_PATH) as db:
        await db.execute("DELETE FROM activity_log")
        await db.commit()
