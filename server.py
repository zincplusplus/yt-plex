"""FastAPI server for managing sources, queue, and settings."""

import asyncio
import json
import logging
import os
import re
import shutil
from typing import Any
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator, model_validator

import runtime_state
from videoqueue import (
    parse_queue,
    mark_deleted,
    prioritize_pending,
    get_queue_events,
    retry_failed,
)
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

SAFE_REGEX_MAX_LEN = 200
SOURCE_RETENTION_MAX_DAYS = 3650
SETTINGS_RETENTION_MAX_DAYS = 3650
SCAN_INTERVAL_MAX_MIN = 1440
SLEEP_BETWEEN_MAX_SEC = 3600
OUTPUT_TEMPLATE_MAX_LEN = 500
GEMINI_KEY_MAX_LEN = 512
DOWNLOADS_HOST_PATH_MAX_LEN = 1024
_RESOLUTIONS = {"360p", "480p", "720p", "1080p"}

_RE_BACKREF = re.compile(r"\\[1-9]")
_RE_LOOKAROUND = re.compile(r"\(\?([=!]|<[=!])")
_RE_NESTED_QUANT = re.compile(r"\([^)]*[+*{][^)]*\)[+*{]")


def _error_response(
    status_code: int,
    code: str,
    message: str,
    fields: list[dict[str, str]] | None = None,
) -> JSONResponse:
    payload: dict[str, Any] = {"error": {"code": code, "message": message}}
    if fields:
        payload["error"]["fields"] = fields
    return JSONResponse(status_code=status_code, content=payload)


def _http_error(status_code: int, code: str, message: str):
    raise HTTPException(
        status_code=status_code,
        detail={"code": code, "message": message},
    )


def _validate_safe_regex(field_name: str, pattern: str | None) -> str | None:
    if pattern is None:
        return None
    text = pattern.strip()
    if not text:
        return None
    if len(text) > SAFE_REGEX_MAX_LEN:
        raise ValueError(f"{field_name} is too long (max {SAFE_REGEX_MAX_LEN} chars)")
    if _RE_BACKREF.search(text):
        raise ValueError(f"{field_name} cannot use backreferences")
    if _RE_LOOKAROUND.search(text):
        raise ValueError(f"{field_name} cannot use lookaround assertions")
    if _RE_NESTED_QUANT.search(text):
        raise ValueError(f"{field_name} cannot use nested quantified groups")
    try:
        re.compile(text, re.IGNORECASE)
    except re.error as e:
        raise ValueError(f"{field_name} is not a valid regex: {e}") from e
    return text


def _delete_video_files(video_id: str) -> bool:
    """Find and delete the download folder for a video_id. Returns True if deleted."""
    if not DOWNLOADS_DIR.exists():
        return False
    for channel_dir in DOWNLOADS_DIR.iterdir():
        if not channel_dir.is_dir():
            continue
        video_dir = channel_dir / video_id
        if video_dir.exists() and video_dir.is_dir():
            shutil.rmtree(video_dir)
            return True
    return False


def _parse_iso_utc(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None


def _seconds_until(ts: str | None) -> int | None:
    dt = _parse_iso_utc(ts)
    if not dt:
        return None
    now = datetime.now(timezone.utc)
    return max(0, int((dt - now).total_seconds()))


def _queue_counts() -> dict[str, int]:
    counts = {
        "pending": 0,
        "downloading": 0,
        "processing": 0,
        "done": 0,
        "download_failed": 0,
        "process_failed": 0,
        "deleted": 0,
    }
    for entry in parse_queue():
        status = entry.get("status")
        if status in counts:
            counts[status] += 1
    counts["total"] = sum(counts.values())
    counts["active"] = counts["downloading"] + counts["processing"]
    counts["failed"] = counts["download_failed"] + counts["process_failed"]
    return counts


def _heartbeat_age_seconds(ts: str | None) -> int | None:
    dt = _parse_iso_utc(ts)
    if not dt:
        return None
    now = datetime.now(timezone.utc)
    return max(0, int((now - dt).total_seconds()))


@app.exception_handler(RequestValidationError)
async def request_validation_exception_handler(_: Request, exc: RequestValidationError):
    fields = []
    for err in exc.errors():
        loc = ".".join(str(x) for x in err.get("loc", []) if x != "__root__")
        fields.append({"field": loc or "body", "message": err.get("msg", "invalid value")})
    return _error_response(400, "validation_error", "Invalid request payload", fields)


@app.exception_handler(HTTPException)
async def http_exception_handler(_: Request, exc: HTTPException):
    detail = exc.detail
    if isinstance(detail, dict):
        code = str(detail.get("code", "http_error"))
        message = str(detail.get("message", "Request failed"))
        fields = detail.get("fields")
    else:
        code = "http_error"
        message = str(detail) if detail is not None else "Request failed"
        fields = None
    return _error_response(exc.status_code, code, message, fields)


@app.get("/", response_class=HTMLResponse)
async def index():
    html_file = TEMPLATES_DIR / "index.html"
    if not html_file.exists():
        return HTMLResponse("<h1>yt-plex</h1><p>Template not found</p>", status_code=500)
    return HTMLResponse(html_file.read_text())


# --- Observability ---


@app.get("/healthz")
async def healthz():
    return {"ok": True}


@app.get("/readyz")
async def readyz():
    try:
        _queue_counts()
        load_sources()
        runtime = runtime_state.snapshot()
        workers = runtime.get("workers", {})
        worker_status = {}
        all_alive = True
        for worker in ("scanner", "downloader", "processor", "cleanup"):
            hb = workers.get(worker, {}).get("last_heartbeat")
            age = _heartbeat_age_seconds(hb)
            alive = age is not None and age <= 180
            worker_status[worker] = {
                "last_heartbeat": hb,
                "heartbeat_age_seconds": age,
                "alive": alive,
            }
            if not alive:
                all_alive = False
        code = 200 if all_alive else 503
        return JSONResponse({"ok": all_alive, "workers": worker_status}, status_code=code)
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=503)


@app.get("/api/system/status")
async def system_status():
    runtime = runtime_state.snapshot()
    queue = _queue_counts()
    scanner = runtime.get("scanner", {})
    cleanup = runtime.get("cleanup", {})
    workers = runtime.get("workers", {})

    worker_health = {}
    for worker, data in workers.items():
        hb = data.get("last_heartbeat")
        worker_health[worker] = {
            **data,
            "heartbeat_age_seconds": _heartbeat_age_seconds(hb),
        }

    return {
        "now": runtime.get("now"),
        "started_at": runtime.get("started_at"),
        "queue": queue,
        "scanner": {
            **scanner,
            "next_scan_in_seconds": _seconds_until(scanner.get("next_scan_at")),
        },
        "cleanup": {
            **cleanup,
            "next_cleanup_in_seconds": _seconds_until(cleanup.get("next_cleanup_at")),
        },
        "workers": worker_health,
    }


@app.get("/metrics", response_class=PlainTextResponse)
async def metrics():
    runtime = runtime_state.snapshot()
    queue = _queue_counts()
    scanner = runtime.get("scanner", {})
    workers = runtime.get("workers", {})

    lines = []
    for status, count in queue.items():
        lines.append(f'ytplex_queue_items{{status="{status}"}} {count}')

    next_scan = _seconds_until(scanner.get("next_scan_at"))
    if next_scan is not None:
        lines.append(f"ytplex_next_scan_in_seconds {next_scan}")

    for worker, data in workers.items():
        age = _heartbeat_age_seconds(data.get("last_heartbeat"))
        if age is not None:
            lines.append(f'ytplex_worker_heartbeat_age_seconds{{worker="{worker}"}} {age}')
        active = 1 if data.get("active_video_id") else 0
        lines.append(f'ytplex_worker_active{{worker="{worker}"}} {active}')

    started = _parse_iso_utc(runtime.get("started_at"))
    now = _parse_iso_utc(runtime.get("now"))
    if started and now:
        uptime = int((now - started).total_seconds())
        lines.append(f"ytplex_uptime_seconds {max(0, uptime)}")

    return "\n".join(lines) + "\n"


# --- Sources ---


class AddSourceRequest(BaseModel):
    url: str = Field(min_length=1, max_length=500)
    latest: int = Field(default=1, ge=1, le=200)
    title_include: str | None = Field(default=None, max_length=SAFE_REGEX_MAX_LEN)
    title_exclude: str | None = Field(default=None, max_length=SAFE_REGEX_MAX_LEN)
    description_include: str | None = Field(default=None, max_length=SAFE_REGEX_MAX_LEN)
    description_exclude: str | None = Field(default=None, max_length=SAFE_REGEX_MAX_LEN)
    min_minutes: int | None = Field(default=None, ge=1, le=24 * 60)
    max_minutes: int | None = Field(default=None, ge=1, le=24 * 60)
    retention_days: int | None = Field(default=None, ge=1, le=SOURCE_RETENTION_MAX_DAYS)

    @field_validator("url")
    @classmethod
    def validate_url(cls, v: str) -> str:
        value = v.strip()
        if not value:
            raise ValueError("url is required")
        if not (value.startswith("http://") or value.startswith("https://")):
            raise ValueError("url must start with http:// or https://")
        return value

    @field_validator(
        "title_include",
        "title_exclude",
        "description_include",
        "description_exclude",
    )
    @classmethod
    def validate_regex_fields(cls, v: str | None, info):
        return _validate_safe_regex(str(info.field_name), v)

    @model_validator(mode="after")
    def validate_min_max(self):
        if self.min_minutes is not None and self.max_minutes is not None and self.min_minutes > self.max_minutes:
            raise ValueError("min_minutes cannot be greater than max_minutes")
        return self


@app.get("/api/sources")
async def get_sources():
    return load_sources()


@app.post("/api/sources")
async def add_source(req: AddSourceRequest):
    """Add a channel. Fetches last N videos, queues them, sets start_after."""
    url = req.url

    sources = load_sources()

    # Check for duplicates early
    for s in sources:
        if s["url"] == url:
            _http_error(409, "source_exists", "Source already exists")

    try:
        from scanner import add_source_with_videos
        filters = {}
        if req.title_include:
            filters["title_include"] = req.title_include
        if req.title_exclude:
            filters["title_exclude"] = req.title_exclude
        if req.description_include:
            filters["description_include"] = req.description_include
        if req.description_exclude:
            filters["description_exclude"] = req.description_exclude
        if req.min_minutes is not None:
            filters["min_minutes"] = req.min_minutes
        if req.max_minutes is not None:
            filters["max_minutes"] = req.max_minutes
        loop = asyncio.get_running_loop()
        source, queued = await loop.run_in_executor(
            None, add_source_with_videos, url, req.latest, filters
        )
    except Exception as e:
        _http_error(400, "source_resolve_failed", f"Could not resolve URL: {e}")

    # Re-check for duplicates (resolved URL may differ)
    sources = load_sources()
    for s in sources:
        if s["url"] == source["url"]:
            _http_error(409, "source_exists", "Source already exists")

    # Add optional filters
    if req.title_include:
        source["title_include"] = req.title_include
    if req.title_exclude:
        source["title_exclude"] = req.title_exclude
    if req.description_include:
        source["description_include"] = req.description_include
    if req.description_exclude:
        source["description_exclude"] = req.description_exclude
    if req.min_minutes is not None:
        source["min_minutes"] = req.min_minutes
    if req.max_minutes is not None:
        source["max_minutes"] = req.max_minutes
    if req.retention_days is not None:
        source["retention_days"] = req.retention_days

    sources.append(source)
    sources.sort(key=lambda s: s.get("name", "").lower())
    save_sources(sources)

    return {**source, "queued": queued}


class EditSourceRequest(BaseModel):
    title_include: str | None = Field(default=None, max_length=SAFE_REGEX_MAX_LEN)
    title_exclude: str | None = Field(default=None, max_length=SAFE_REGEX_MAX_LEN)
    description_include: str | None = Field(default=None, max_length=SAFE_REGEX_MAX_LEN)
    description_exclude: str | None = Field(default=None, max_length=SAFE_REGEX_MAX_LEN)
    min_minutes: int | None = Field(default=None, ge=1, le=24 * 60)
    max_minutes: int | None = Field(default=None, ge=1, le=24 * 60)
    retention_days: int | None = Field(default=None, ge=1, le=SOURCE_RETENTION_MAX_DAYS)

    model_config = {"extra": "forbid"}

    @field_validator(
        "title_include",
        "title_exclude",
        "description_include",
        "description_exclude",
    )
    @classmethod
    def validate_regex_fields(cls, v: str | None, info):
        return _validate_safe_regex(str(info.field_name), v)

    @model_validator(mode="after")
    def validate_min_max(self):
        if self.min_minutes is not None and self.max_minutes is not None and self.min_minutes > self.max_minutes:
            raise ValueError("min_minutes cannot be greater than max_minutes")
        return self


@app.put("/api/sources/{index}")
async def edit_source(index: int, req: EditSourceRequest):
    sources = load_sources()
    if index < 0 or index >= len(sources):
        _http_error(404, "source_not_found", "Source not found")

    source = sources[index]
    editable = ["title_include", "title_exclude", "description_include",
                "description_exclude", "min_minutes", "max_minutes",
                "retention_days"]

    for field in editable:
        if field not in req.model_fields_set:
            continue
        value = getattr(req, field)
        if value is None or value == "":
            source.pop(field, None)
        else:
            source[field] = value

    sources.sort(key=lambda s: s.get("name", "").lower())
    save_sources(sources)
    return source


@app.delete("/api/sources/{index}")
async def delete_source(index: int, delete_files: bool = False):
    sources = load_sources()
    if index < 0 or index >= len(sources):
        _http_error(404, "source_not_found", "Source not found")
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
    entries = parse_queue()
    entries.sort(key=lambda e: e["date"], reverse=True)
    return entries


@app.get("/api/queue/events")
async def queue_events(limit: int = 100):
    if limit < 1 or limit > 1000:
        _http_error(400, "invalid_limit", "limit must be between 1 and 1000")
    return get_queue_events(limit)


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
    if prioritize_pending(video_id, actor="api"):
        return {"queued": video_id}
    _http_error(404, "pending_video_not_found", "Pending video not found")


@app.post("/api/queue/{video_id}/retry")
async def retry_video(video_id: str):
    """Retry a failed video. [!] -> [ ], [e] -> [p]."""
    new_status = retry_failed(video_id, actor="api")
    if new_status:
        return {"retried": video_id, "new_status": new_status}
    _http_error(404, "failed_video_not_found", "Failed video not found")


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
        _http_error(404, "video_not_found", "Video not found in queue")

    # Delete files
    _delete_video_files(video_id)

    await mark_deleted(video_id)
    return {"deleted": video_id}


# --- Settings ---

_ENV_DEFAULTS = {
    "scan_interval": int(os.getenv("SCAN_INTERVAL", "30")),
    "sleep_between_downloads": int(os.getenv("SLEEP_BETWEEN_DOWNLOADS", "5")),
    "sponsorblock": os.getenv("SPONSORBLOCK", "true").lower() == "true",
    "preferred_resolution": os.getenv("PREFERRED_RESOLUTION", "1080p"),
    "output_template": os.getenv("OUTPUT_TEMPLATE",
        "%(channel)s/%(id)s/%(title)s.%(ext)s"),
    "retention_days": int(os.getenv("RETENTION_DAYS", "7")),
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


class SettingsUpdateRequest(BaseModel):
    scan_interval: int | None = Field(default=None, ge=1, le=SCAN_INTERVAL_MAX_MIN)
    sleep_between_downloads: int | None = Field(default=None, ge=0, le=SLEEP_BETWEEN_MAX_SEC)
    sponsorblock: bool | None = None
    preferred_resolution: str | None = None
    output_template: str | None = Field(default=None, min_length=1, max_length=OUTPUT_TEMPLATE_MAX_LEN)
    retention_days: int | None = Field(default=None, ge=1, le=SETTINGS_RETENTION_MAX_DAYS)
    gemini_api_key: str | None = Field(default=None, max_length=GEMINI_KEY_MAX_LEN)
    downloads_host_path: str | None = Field(default=None, max_length=DOWNLOADS_HOST_PATH_MAX_LEN)

    model_config = {"extra": "forbid"}

    @field_validator("preferred_resolution")
    @classmethod
    def validate_resolution(cls, v: str | None) -> str | None:
        if v is None:
            return None
        value = v.strip()
        if value not in _RESOLUTIONS:
            raise ValueError(f"preferred_resolution must be one of {sorted(_RESOLUTIONS)}")
        return value

    @field_validator("output_template")
    @classmethod
    def validate_output_template(cls, v: str | None) -> str | None:
        if v is None:
            return None
        value = v.strip()
        if not value:
            raise ValueError("output_template cannot be empty")
        return value

    @field_validator("gemini_api_key", "downloads_host_path")
    @classmethod
    def strip_optional_text(cls, v: str | None) -> str | None:
        if v is None:
            return None
        return v.strip()


@app.put("/api/settings")
async def update_settings(req: SettingsUpdateRequest):
    updates = req.model_dump(exclude_none=True)
    if not updates:
        _http_error(400, "no_settings_provided", "No valid settings provided")
    settings.save(updates)
    return await get_settings()
