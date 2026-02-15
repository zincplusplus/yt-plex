"""Scan channels via yt-dlp, filter videos, append new ones to queue.md."""

import json
import logging
import re
from datetime import datetime
from pathlib import Path

import yt_dlp

from videoqueue import read_queue_ids

logger = logging.getLogger(__name__)

SHORTS_MAX_DURATION = 60
DATA_DIR = Path("data")
SOURCES_FILE = DATA_DIR / "sources.json"
QUEUE_FILE = DATA_DIR / "queue.md"


def load_sources() -> list[dict]:
    if not SOURCES_FILE.exists():
        return []
    return json.loads(SOURCES_FILE.read_text())


def save_sources(sources: list[dict]):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    SOURCES_FILE.write_text(json.dumps(sources, indent=2))


def append_to_queue(date: str, channel: str, title: str, video_id: str):
    """Append a pending line to queue.md."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    safe_title = title.replace("|", "-")
    line = f"- [ ] {date} | {channel} | {safe_title} | {video_id}\n"
    with open(QUEUE_FILE, "a") as f:
        f.write(line)


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
        if start_after and meta.get("timestamp"):
            if meta["timestamp"] <= start_after:
                logger.debug(f"Skipped {vid}: before start_after")
                continue

        # 5. Shorts / livestream
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


def add_source_with_videos(url: str, latest: int = 1) -> tuple[dict, int]:
    """Add a source: resolve name, fetch last N videos, queue them, set start_after.

    Returns (source_dict, videos_queued_count).
    """
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

    # Fetch last N videos
    videos = scan_channel(resolved_url, limit=max(latest, 10))

    # Take the most recent N (videos come newest-first from flat extract)
    recent = videos[:latest]

    # Fetch full metadata for each to get timestamps
    existing_ids = read_queue_ids()
    queued = []
    oldest_timestamp = None

    # Reverse so oldest is queued first (newest appears as "recently added" in Plex)
    for v in reversed(recent):
        vid = v["video_id"]
        if vid in existing_ids:
            continue

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
        if ts and (oldest_timestamp is None or ts < oldest_timestamp):
            oldest_timestamp = ts

    # Build source config with start_after set to oldest queued video's timestamp
    source = {"url": resolved_url, "name": name}
    if oldest_timestamp:
        source["start_after"] = oldest_timestamp

    return source, len(queued)
