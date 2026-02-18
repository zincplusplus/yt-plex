"""Scan channels via yt-dlp, filter videos, insert new items into queue.db.

Also manages source configuration in SQLite (`data/queue.db`) with
one-time migration from legacy `data/sources.json`.
"""

import json
import logging
import re
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import yt_dlp

from videoqueue import read_queue_ids, enqueue_pending

logger = logging.getLogger(__name__)

SHORTS_MAX_DURATION = 60
DATA_DIR = Path("data")
SOURCES_FILE = DATA_DIR / "sources.json"
DB_FILE = DATA_DIR / "queue.db"

_SOURCE_KEYS = [
    "id",
    "url",
    "name",
    "sponsorblock",
    "gemini_fallback",
    "start_after",
    "title_include",
    "title_exclude",
    "description_include",
    "description_exclude",
    "min_minutes",
    "max_minutes",
    "retention_days",
]


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _connect() -> sqlite3.Connection:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_FILE, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def _init_sources_storage():
    with _connect() as conn:
        conn.execute("PRAGMA busy_timeout=30000")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS schema_migrations (
              name TEXT PRIMARY KEY,
              applied_at TEXT NOT NULL
            )
            """
        )
        exists = conn.execute(
            "SELECT 1 FROM schema_migrations WHERE name = 'sources_v1'"
        ).fetchone()
        if not exists:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS sources (
                  id TEXT PRIMARY KEY,
                  url TEXT NOT NULL UNIQUE,
                  name TEXT NOT NULL,
                  sponsorblock INTEGER,
                  gemini_fallback INTEGER,
                  start_after INTEGER,
                  title_include TEXT,
                  title_exclude TEXT,
                  description_include TEXT,
                  description_exclude TEXT,
                  min_minutes INTEGER,
                  max_minutes INTEGER,
                  retention_days INTEGER,
                  created_at TEXT NOT NULL,
                  updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_sources_name ON sources(name);
                CREATE INDEX IF NOT EXISTS idx_sources_updated ON sources(updated_at DESC);
                """
            )
            conn.execute(
                "INSERT INTO schema_migrations(name, applied_at) VALUES (?, ?)",
                ("sources_v1", _now()),
            )
        has_sponsorblock = conn.execute(
            "SELECT 1 FROM pragma_table_info('sources') WHERE name = 'sponsorblock'"
        ).fetchone()
        if not has_sponsorblock:
            conn.execute("ALTER TABLE sources ADD COLUMN sponsorblock INTEGER")
        has_gemini_fallback = conn.execute(
            "SELECT 1 FROM pragma_table_info('sources') WHERE name = 'gemini_fallback'"
        ).fetchone()
        if not has_gemini_fallback:
            conn.execute("ALTER TABLE sources ADD COLUMN gemini_fallback INTEGER")
        conn.commit()


def _normalize_source(d: dict) -> dict:
    out = {k: d.get(k) for k in _SOURCE_KEYS}
    out["id"] = str(out.get("id") or uuid4())
    out["url"] = (out.get("url") or "").strip()
    out["name"] = (out.get("name") or "").strip()
    sb = out.get("sponsorblock")
    if sb is not None:
        out["sponsorblock"] = bool(sb)
    gf = out.get("gemini_fallback")
    if gf is not None:
        out["gemini_fallback"] = bool(gf)
    return out


def _migrate_legacy_sources_if_needed():
    _init_sources_storage()
    with _connect() as conn:
        count = conn.execute("SELECT COUNT(*) FROM sources").fetchone()[0]
        if count > 0:
            logger.info(f"Sources migration: skipped (sources table already populated: {count})")
            return
        if not SOURCES_FILE.exists():
            logger.info("Sources migration: skipped (no legacy data/sources.json found)")
            return
        text = SOURCES_FILE.read_text().strip()
        if not text:
            logger.info("Sources migration: skipped (legacy data/sources.json is empty)")
            return
        try:
            legacy = json.loads(text)
        except json.JSONDecodeError:
            logger.warning("sources.json is invalid JSON; skipping migration")
            return
        now = _now()
        imported = 0
        for raw in legacy:
            s = _normalize_source(raw if isinstance(raw, dict) else {})
            if not s["url"] or not s["name"]:
                continue
            cur = conn.execute(
                """
                INSERT OR IGNORE INTO sources(
                    id, url, name, sponsorblock, gemini_fallback, start_after, title_include, title_exclude,
                    description_include, description_exclude, min_minutes, max_minutes,
                    retention_days, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    s["id"],
                    s["url"],
                    s["name"],
                    s.get("sponsorblock"),
                    s.get("gemini_fallback"),
                    s.get("start_after"),
                    s.get("title_include"),
                    s.get("title_exclude"),
                    s.get("description_include"),
                    s.get("description_exclude"),
                    s.get("min_minutes"),
                    s.get("max_minutes"),
                    s.get("retention_days"),
                    now,
                    now,
                ),
            )
            imported += cur.rowcount
        conn.commit()
        logger.info(f"Sources migration: imported {imported} source(s) from data/sources.json")


def load_sources() -> list[dict]:
    _migrate_legacy_sources_if_needed()
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT id, url, name, sponsorblock, start_after, title_include, title_exclude,
                   gemini_fallback,
                   description_include, description_exclude, min_minutes, max_minutes,
                   retention_days
            FROM sources
            ORDER BY lower(name) ASC
            """
        ).fetchall()
    out: list[dict] = []
    for r in rows:
        d = {k: r[k] for k in _SOURCE_KEYS}
        if d.get("sponsorblock") is not None:
            d["sponsorblock"] = bool(d["sponsorblock"])
        if d.get("gemini_fallback") is not None:
            d["gemini_fallback"] = bool(d["gemini_fallback"])
        # Keep payload lean by omitting null optional fields.
        d = {k: v for k, v in d.items() if v is not None}
        out.append(d)
    return out


def save_sources(sources: list[dict]):
    _migrate_legacy_sources_if_needed()
    normalized = [_normalize_source(s if isinstance(s, dict) else {}) for s in sources]
    now = _now()
    with _connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        existing = {r["id"] for r in conn.execute("SELECT id FROM sources").fetchall()}
        incoming = {s["id"] for s in normalized}

        for stale in existing - incoming:
            conn.execute("DELETE FROM sources WHERE id = ?", (stale,))

        for s in normalized:
            conn.execute(
                """
                INSERT INTO sources(
                    id, url, name, sponsorblock, gemini_fallback, start_after, title_include, title_exclude,
                    description_include, description_exclude, min_minutes, max_minutes,
                    retention_days, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    url=excluded.url,
                    name=excluded.name,
                    sponsorblock=excluded.sponsorblock,
                    gemini_fallback=excluded.gemini_fallback,
                    start_after=excluded.start_after,
                    title_include=excluded.title_include,
                    title_exclude=excluded.title_exclude,
                    description_include=excluded.description_include,
                    description_exclude=excluded.description_exclude,
                    min_minutes=excluded.min_minutes,
                    max_minutes=excluded.max_minutes,
                    retention_days=excluded.retention_days,
                    updated_at=excluded.updated_at
                """,
                (
                    s["id"],
                    s["url"],
                    s["name"],
                    s.get("sponsorblock"),
                    s.get("gemini_fallback"),
                    s.get("start_after"),
                    s.get("title_include"),
                    s.get("title_exclude"),
                    s.get("description_include"),
                    s.get("description_exclude"),
                    s.get("min_minutes"),
                    s.get("max_minutes"),
                    s.get("retention_days"),
                    now,
                    now,
                ),
            )
        conn.commit()


def get_source_by_id(source_id: str) -> dict | None:
    sources = load_sources()
    for s in sources:
        if s.get("id") == source_id:
            return s
    return None


def find_source_by_url(url: str) -> dict | None:
    needle = url.strip()
    if not needle:
        return None
    sources = load_sources()
    for s in sources:
        if s.get("url") == needle:
            return s
    return None


def resolve_source_identifier(identifier: str) -> dict | None:
    # Primary: stable ID
    by_id = get_source_by_id(identifier)
    if by_id:
        return by_id
    # Compatibility path: numeric index from sorted source list.
    if identifier.isdigit():
        idx = int(identifier)
        sources = load_sources()
        if 0 <= idx < len(sources):
            return sources[idx]
    return None


def upsert_source(source: dict) -> dict:
    base = load_sources()
    incoming = _normalize_source(source)
    if not incoming["id"]:
        incoming["id"] = str(uuid4())
    replaced = False
    for i, s in enumerate(base):
        if s.get("id") == incoming["id"]:
            merged = {**s, **{k: v for k, v in incoming.items() if v is not None}}
            base[i] = merged
            replaced = True
            break
    if not replaced:
        base.append({k: v for k, v in incoming.items() if v is not None})
    save_sources(base)
    current = get_source_by_id(incoming["id"])
    return current if current else {k: v for k, v in incoming.items() if v is not None}


def delete_source(source_id: str) -> dict | None:
    sources = load_sources()
    keep = []
    removed = None
    for s in sources:
        if s.get("id") == source_id:
            removed = s
            continue
        keep.append(s)
    if removed is None:
        return None
    save_sources(keep)
    return removed


def get_sources_revision() -> str:
    _migrate_legacy_sources_if_needed()
    with _connect() as conn:
        row = conn.execute(
            "SELECT COALESCE(MAX(updated_at), '') AS rev FROM sources"
        ).fetchone()
    return str(row["rev"] or "")


def append_to_queue(date: str, channel: str, title: str, video_id: str):
    """Insert a pending queue item into SQLite."""
    enqueue_pending(date, channel, title, video_id, actor="scanner")


def matches_filter(text: str, pattern: str) -> bool:
    if not pattern:
        return False
    if not text:
        return False
    try:
        return bool(re.search(pattern, text, re.IGNORECASE))
    except re.error:
        return False


def scan_channel(url: str, limit: int = 50) -> list[dict]:
    """Flat-extract recent videos from a channel."""
    scan_url = url
    if "/videos" not in scan_url and "playlist" not in scan_url.lower():
        scan_url = scan_url.rstrip("/") + "/videos"

    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "extract_flat": True,
        "playlist_items": f"1:{limit}",
    }

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(scan_url, download=False)
        entries = info.get("entries", [])
        return [
            {
                "video_id": e.get("id"),
                "title": e.get("title"),
                "duration": e.get("duration"),
            }
            for e in entries
            if e and e.get("id")
        ]


def get_video_metadata(video_id: str) -> dict:
    """Fetch full metadata for a single video."""
    url = f"https://www.youtube.com/watch?v={video_id}"
    ydl_opts = {"quiet": True, "no_warnings": True}

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=False)
        return {
            "title": info.get("title", ""),
            "description": info.get("description", ""),
            "channel": info.get("channel") or info.get("uploader") or "",
            "duration": info.get("duration"),
            "upload_date": info.get("upload_date"),  # YYYYMMDD
            "timestamp": info.get("timestamp"),  # Unix timestamp
            "is_live": info.get("is_live") or info.get("was_live", False),
        }


def scan_source(source: dict, existing_ids: set[str]) -> int:
    """Scan one source, append new videos to queue. Returns count added.

    Filtering pipeline (in order):
    1. Already in queue (dedup)
    2. Title filters (from flat extract - no extra API call)
    3. Full metadata fetch (only if start_after or description filters needed)
    4. start_after check
    5. Shorts / livestream
    6. Description filters
    """
    url = source["url"]
    name = source.get("name", url)
    start_after = source.get("start_after")
    needs_desc_filter = bool(source.get("description_include") or source.get("description_exclude"))

    limit = 200 if start_after else 50

    logger.info(f"Scanning: {name}")
    try:
        videos = scan_channel(url, limit=limit)
    except Exception as e:
        logger.error(f"Scan failed for {name}: {e}")
        return 0

    # Reverse so oldest videos are queued/downloaded first;
    # newest downloads last and appears as "recently added" in Plex
    videos.reverse()

    added = 0
    for v in videos:
        vid = v["video_id"]

        # 1. Dedup
        if vid in existing_ids:
            continue

        # 2. Title filters (cheap, from flat extract)
        title = v.get("title", "")
        ti = source.get("title_include")
        if ti and not matches_filter(title, ti):
            logger.debug(f"Skipped {vid}: title_include")
            continue
        te = source.get("title_exclude")
        if te and matches_filter(title, te):
            logger.debug(f"Skipped {vid}: title_exclude")
            continue

        # 3. Full metadata fetch (if start_after or description filters)
        needs_meta = bool(start_after) or needs_desc_filter
        meta = None
        if needs_meta:
            try:
                meta = get_video_metadata(vid)
            except Exception as e:
                logger.warning(f"Could not fetch metadata for {vid}: {e}")
                continue
        else:
            meta = {
                "title": title,
                "description": "",
                "channel": name,
                "duration": v.get("duration"),
                "upload_date": None,
                "timestamp": None,
                "is_live": False,
            }

        # 4. start_after check
        if start_after:
            ts = meta.get("timestamp")
            if ts is None and meta.get("upload_date"):
                try:
                    from datetime import timezone
                    dt = datetime.strptime(meta["upload_date"], "%Y%m%d").replace(tzinfo=timezone.utc)
                    ts = int(dt.timestamp())
                except (ValueError, TypeError):
                    pass
            if ts is None:
                logger.debug(f"Skipped {vid}: no timestamp, can't compare to start_after")
                continue
            if ts <= start_after:
                logger.debug(f"Skipped {vid}: before start_after")
                continue

        # 5. Shorts / livestream / duration
        duration = meta["duration"] if meta.get("duration") is not None else v.get("duration")
        if duration is not None and duration < SHORTS_MAX_DURATION:
            logger.debug(f"Skipped {vid}: short")
            continue
        if "#shorts" in (meta.get("title") or title).lower():
            logger.debug(f"Skipped {vid}: short")
            continue
        if meta.get("is_live"):
            logger.debug(f"Skipped {vid}: livestream")
            continue
        min_min = source.get("min_minutes")
        max_min = source.get("max_minutes")
        if duration is not None and min_min and duration < min_min * 60:
            logger.debug(f"Skipped {vid}: too short ({duration}s < {min_min}m)")
            continue
        if duration is not None and max_min and duration > max_min * 60:
            logger.debug(f"Skipped {vid}: too long ({duration}s > {max_min}m)")
            continue

        # 6. Description filters
        if meta:
            desc = meta.get("description", "")
            di = source.get("description_include")
            if di and not matches_filter(desc, di):
                logger.debug(f"Skipped {vid}: description_include")
                continue
            de = source.get("description_exclude")
            if de and matches_filter(desc, de):
                logger.debug(f"Skipped {vid}: description_exclude")
                continue

        upload_date = meta.get("upload_date")
        if upload_date:
            date_str = f"{upload_date[:4]}-{upload_date[4:6]}-{upload_date[6:8]}"
        else:
            date_str = datetime.now().strftime("%Y-%m-%d")

        channel_name = meta.get("channel") or name
        final_title = meta.get("title") or title or "Unknown"

        append_to_queue(date_str, channel_name, final_title, vid)
        existing_ids.add(vid)
        added += 1
        logger.info(f"Queued: {final_title} ({vid})")

    logger.info(f"Scan complete for {name}: {added} new")
    return added


def scan_all():
    """Scan all sources."""
    sources = load_sources()
    if not sources:
        logger.info("No sources configured")
        return 0

    existing_ids = read_queue_ids()
    total = 0
    for source in sources:
        total += scan_source(source, existing_ids)
    return total


def add_source_with_videos(url: str, latest: int = 1,
                           filters: dict | None = None) -> tuple[dict, int]:
    """Add a source: resolve name, fetch last N videos, queue them, set start_after.

    Applies title/description filters before selecting videos.
    Returns (source_dict, videos_queued_count).
    """
    filters = filters or {}
    ti = filters.get("title_include")
    te = filters.get("title_exclude")
    di = filters.get("description_include")
    de = filters.get("description_exclude")

    # Resolve channel name
    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "extract_flat": True,
        "playlist_items": "0",
    }
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=False)
        name = info.get("channel") or info.get("uploader") or info.get("title") or url
        resolved_url = info.get("channel_url") or info.get("uploader_url") or url

    # Fetch more videos to have enough after filtering
    fetch_limit = max(latest * 5, 20) if (ti or te or di or de) else max(latest, 10)
    videos = scan_channel(resolved_url, limit=fetch_limit)

    # Filter and collect up to `latest` matching videos
    existing_ids = read_queue_ids()
    queued = []
    oldest_timestamp = None
    matched = []

    for v in videos:
        if len(matched) >= latest:
            break

        vid = v["video_id"]
        if vid in existing_ids:
            continue

        title = v.get("title", "")

        # Title filters (cheap, from flat extract)
        if ti and not matches_filter(title, ti):
            continue
        if te and matches_filter(title, te):
            continue

        # Skip shorts / duration filters (from flat extract)
        duration = v.get("duration")
        if duration is not None and duration < SHORTS_MAX_DURATION:
            continue
        min_min = filters.get("min_minutes")
        max_min = filters.get("max_minutes")
        if duration is not None and min_min and duration < min_min * 60:
            continue
        if duration is not None and max_min and duration > max_min * 60:
            continue

        # Description filters require full metadata
        if di or de:
            try:
                meta = get_video_metadata(vid)
            except Exception as e:
                logger.warning(f"Could not fetch metadata for {vid}: {e}")
                continue
            desc = meta.get("description", "")
            if di and not matches_filter(desc, di):
                continue
            if de and matches_filter(desc, de):
                continue
            v["_meta"] = meta

        matched.append(v)

    # Queue matched videos (reversed so oldest first for Plex ordering)
    for v in reversed(matched):
        vid = v["video_id"]

        meta = v.get("_meta")
        if not meta:
            try:
                meta = get_video_metadata(vid)
            except Exception as e:
                logger.warning(f"Could not fetch metadata for {vid}: {e}")
                continue

        upload_date = meta.get("upload_date")
        if upload_date:
            date_str = f"{upload_date[:4]}-{upload_date[4:6]}-{upload_date[6:8]}"
        else:
            date_str = datetime.now().strftime("%Y-%m-%d")

        channel_name = meta.get("channel") or name
        title = meta.get("title") or v.get("title", "Unknown")

        append_to_queue(date_str, channel_name, title, vid)
        existing_ids.add(vid)
        queued.append(vid)
        logger.info(f"Queued: {title} ({vid})")

        ts = meta.get("timestamp")
        if ts is None and upload_date:
            try:
                from datetime import timezone
                dt = datetime.strptime(upload_date, "%Y%m%d").replace(tzinfo=timezone.utc)
                ts = int(dt.timestamp())
            except (ValueError, TypeError):
                pass
        if ts and (oldest_timestamp is None or ts < oldest_timestamp):
            oldest_timestamp = ts

    # Build source config with start_after set to oldest queued video's timestamp
    # Fall back to current time so scanner never queues old videos
    source = {"url": resolved_url, "name": name}
    source["start_after"] = oldest_timestamp or int(time.time())
    source["id"] = str(uuid4())

    return source, len(queued)
