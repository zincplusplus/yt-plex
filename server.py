"""FastAPI server for managing sources, queue, and settings."""

import asyncio
import json
import logging
import os
import shutil
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from videoqueue import parse_queue, mark_deleted, mark_pending, mark_processing
from scanner import load_sources, save_sources
import settings

logger = logging.getLogger(__name__)

DATA_DIR = Path("data")
SOURCES_FILE = DATA_DIR / "sources.json"
DOWNLOADS_DIR = Path(os.getenv("DOWNLOADS_DIR", "./downloads"))

app = FastAPI(title="yt-plex")

STATIC_DIR = Path("static")
if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


TEMPLATES_DIR = Path("templates")


@app.get("/", response_class=HTMLResponse)
async def index():
    html_file = TEMPLATES_DIR / "index.html"
    if not html_file.exists():
        return HTMLResponse("<h1>yt-plex</h1><p>Template not found</p>", status_code=500)
    return HTMLResponse(html_file.read_text())


# --- Sources ---


class AddSourceRequest(BaseModel):
    url: str
    latest: int = 1
    title_include: str | None = None
    title_exclude: str | None = None
    description_include: str | None = None
    description_exclude: str | None = None
    retention_days: int | None = None


@app.get("/api/sources")
async def get_sources():
    return load_sources()


@app.post("/api/sources")
async def add_source(req: AddSourceRequest):
    """Add a channel. Fetches last N videos, queues them, sets start_after."""
    url = req.url.strip()
    if not url:
        raise HTTPException(400, "URL required")

    sources = load_sources()

    # Check for duplicates early
    for s in sources:
        if s["url"] == url:
            raise HTTPException(409, "Source already exists")

    try:
        from scanner import add_source_with_videos
        loop = asyncio.get_running_loop()
        source, queued = await loop.run_in_executor(
            None, add_source_with_videos, url, req.latest
        )
    except Exception as e:
        raise HTTPException(400, f"Could not resolve URL: {e}")

    # Re-check for duplicates (resolved URL may differ)
    sources = load_sources()
    for s in sources:
        if s["url"] == source["url"]:
            raise HTTPException(409, "Source already exists")

    # Add optional filters
    if req.title_include:
        source["title_include"] = req.title_include
    if req.title_exclude:
        source["title_exclude"] = req.title_exclude
    if req.description_include:
        source["description_include"] = req.description_include
    if req.description_exclude:
        source["description_exclude"] = req.description_exclude
    if req.retention_days is not None:
        source["retention_days"] = req.retention_days

    sources.append(source)
    save_sources(sources)

    return {**source, "queued": queued}


@app.delete("/api/sources/{index}")
async def delete_source(index: int, delete_files: bool = False):
    sources = load_sources()
    if index < 0 or index >= len(sources):
        raise HTTPException(404, "Source not found")
    removed = sources.pop(index)
    save_sources(sources)

    name = removed.get("name", "")
    files_deleted = 0

    if name:
        if delete_files:
            channel_dir = DOWNLOADS_DIR / name
            if channel_dir.exists() and channel_dir.is_dir():
                files_deleted = sum(1 for f in channel_dir.rglob("*") if f.is_file())
                shutil.rmtree(channel_dir)

        for entry in parse_queue():
            if entry["channel"] == name and entry["status"] in ("pending", "done", "download_failed", "process_failed"):
                await mark_deleted(entry["video_id"])

    return {"removed": removed, "files_deleted": files_deleted}


# --- Queue ---


@app.get("/api/queue")
async def get_queue():
    return parse_queue()


@app.post("/api/scan-now")
async def scan_now():
    """Trigger an immediate scan of all sources."""
    from scanner import scan_all
    loop = asyncio.get_running_loop()
    added = await loop.run_in_executor(None, scan_all)
    return {"added": added}


@app.post("/api/queue/{video_id}/download-now")
async def download_now(video_id: str):
    """Move a pending video to the top of the queue for immediate download."""
    entries = parse_queue()
    for e in entries:
        if e["video_id"] == video_id and e["status"] == "pending":
            # Move to top by removing and re-inserting at the beginning of pending entries
            from videoqueue import QUEUE_FILE, queue_lock
            async with queue_lock:
                lines = QUEUE_FILE.read_text().splitlines()
                # Find and remove this line
                target_line = None
                target_idx = None
                for i, line in enumerate(lines):
                    if video_id in line and line.startswith("- [ ]"):
                        target_line = line
                        target_idx = i
                        break
                if target_line is not None and target_idx is not None:
                    lines.pop(target_idx)
                    # Find the first pending entry and insert before it
                    inserted = False
                    for i, line in enumerate(lines):
                        if line.startswith("- [ ]"):
                            lines.insert(i, target_line)
                            inserted = True
                            break
                    if not inserted:
                        lines.insert(0, target_line)
                    QUEUE_FILE.write_text("\n".join(lines) + "\n")
            return {"queued": video_id}
    raise HTTPException(404, "Pending video not found")


@app.post("/api/queue/{video_id}/retry")
async def retry_video(video_id: str):
    """Retry a failed video. [!] -> [ ], [e] -> [p]."""
    entries = parse_queue()
    for e in entries:
        if e["video_id"] == video_id:
            if e["status"] == "download_failed":
                await mark_pending(video_id)
                return {"retried": video_id, "new_status": "pending"}
            elif e["status"] == "process_failed":
                await mark_processing(video_id)
                return {"retried": video_id, "new_status": "processing"}
    raise HTTPException(404, "Failed video not found")


@app.delete("/api/queue/{video_id}")
async def delete_video(video_id: str):
    """Delete a video's files and mark as deleted."""
    entries = parse_queue()
    found = False
    for e in entries:
        if e["video_id"] == video_id:
            found = True
            break

    if not found:
        raise HTTPException(404, "Video not found in queue")

    # Delete files
    if DOWNLOADS_DIR.exists():
        deleted_dirs = set()
        for f in DOWNLOADS_DIR.rglob(f"*{video_id}*"):
            if f.is_file():
                parent = f.parent
                if parent != DOWNLOADS_DIR and parent.parent != DOWNLOADS_DIR:
                    if parent not in deleted_dirs:
                        shutil.rmtree(parent)
                        deleted_dirs.add(parent)
                else:
                    f.unlink()

    await mark_deleted(video_id)
    return {"deleted": video_id}


# --- Settings ---

_ENV_DEFAULTS = {
    "scan_interval": int(os.getenv("SCAN_INTERVAL", "360")),
    "sleep_between_downloads": int(os.getenv("SLEEP_BETWEEN_DOWNLOADS", "5")),
    "sponsorblock": os.getenv("SPONSORBLOCK", "true").lower() == "true",
    "preferred_resolution": os.getenv("PREFERRED_RESOLUTION", "1080p"),
    "output_template": os.getenv("OUTPUT_TEMPLATE",
        "%(channel)s/%(id)s/%(title)s.%(ext)s"),
    "retention_days": int(os.getenv("RETENTION_DAYS", "14")),
    "gemini_api_key": os.getenv("GEMINI_API_KEY", ""),
    "downloads_host_path": os.getenv("DOWNLOADS_HOST_PATH", ""),
}

_SETTINGS_TYPES = {
    "scan_interval": int,
    "sleep_between_downloads": int,
    "sponsorblock": lambda v: v if isinstance(v, bool) else str(v).lower() == "true",
    "preferred_resolution": str,
    "output_template": str,
    "retention_days": int,
    "gemini_api_key": str,
    "downloads_host_path": str,
}


@app.get("/api/settings")
async def get_settings():
    result = {"downloads_dir": str(DOWNLOADS_DIR)}
    for key, default in _ENV_DEFAULTS.items():
        raw = settings.get(key, str(default))
        coerce = _SETTINGS_TYPES.get(key, str)
        try:
            result[key] = coerce(raw)
        except (ValueError, TypeError):
            result[key] = default
    return result


@app.put("/api/settings")
async def update_settings(req: Request):
    body = await req.json()
    updates = {}
    for key, coerce in _SETTINGS_TYPES.items():
        if key in body:
            try:
                updates[key] = coerce(body[key])
            except (ValueError, TypeError):
                raise HTTPException(400, f"Invalid value for {key}")
    if not updates:
        raise HTTPException(400, "No valid settings provided")
    settings.save(updates)
    return await get_settings()
