"""Entry point: four service loops + web server."""

import asyncio
import json
import logging
import os
import shutil
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import uvicorn

import settings
import runtime_state
from videoqueue import (
    parse_queue, get_by_status, claim_next_pending,
    mark_deleted, reset_interrupted,
    record_download_success, record_download_failure,
    record_process_success, record_process_failure,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("yt-plex")

DOWNLOADS_DIR = Path(os.getenv("DOWNLOADS_DIR", "./downloads"))
DATA_DIR = Path("data")
SOURCES_FILE = DATA_DIR / "sources.json"

ENV_DEFAULTS = {
    "scan_interval": os.getenv("SCAN_INTERVAL", "30"),
    "sleep_between_downloads": os.getenv("SLEEP_BETWEEN_DOWNLOADS", "5"),
    "sponsorblock": os.getenv("SPONSORBLOCK", "true"),
    "preferred_resolution": os.getenv("PREFERRED_RESOLUTION", "1080p"),
    "output_template": os.getenv("OUTPUT_TEMPLATE",
        "%(channel)s/%(id)s/%(title)s.%(ext)s"),
    "retention_days": os.getenv("RETENTION_DAYS", "7"),
    "gemini_api_key": os.getenv("GEMINI_API_KEY", ""),
    "cleanup_timezone": os.getenv("CLEANUP_TIMEZONE", "Europe/Amsterdam"),
}


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _in_seconds_iso(seconds: int) -> str:
    return (
        datetime.now(timezone.utc) + timedelta(seconds=seconds)
    ).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _from_ts_iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _log_event(level: int, event: str, **fields):
    payload = {"event": event, **fields}
    logger.log(level, json.dumps(payload, sort_keys=True, ensure_ascii=True))


def _coerce_bool(value, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _source_sponsorblock_enabled(channel: str, default: bool) -> bool:
    """Return per-source sponsorblock override for a channel, else default."""
    try:
        from scanner import load_sources
        needle = (channel or "").strip().casefold()
        if not needle:
            return default
        for source in load_sources():
            name = str(source.get("name", "")).strip().casefold()
            if name == needle and "sponsorblock" in source:
                return _coerce_bool(source.get("sponsorblock"), default)
    except Exception:
        pass
    return default


def _source_gemini_fallback_enabled(channel: str, default: bool) -> bool:
    """Return per-source Gemini fallback override for a channel, else default."""
    try:
        from scanner import load_sources
        needle = (channel or "").strip().casefold()
        if not needle:
            return default
        for source in load_sources():
            name = str(source.get("name", "")).strip().casefold()
            if name == needle and "gemini_fallback" in source:
                return _coerce_bool(source.get("gemini_fallback"), default)
    except Exception:
        pass
    return default


async def scanner_loop():
    """Periodically scan all sources for new videos."""
    from scanner import scan_all, get_sources_revision

    while True:
        scan_interval = int(settings.get("scan_interval", ENV_DEFAULTS["scan_interval"]))
        cycle_started = time.time()
        deadline = cycle_started + scan_interval * 60
        runtime_state.set_component("scanner", next_scan_at=_from_ts_iso(deadline))
        runtime_state.touch_worker("scanner")
        runtime_state.set_component(
            "scanner",
            is_scanning=True,
            scan_started_at=_utcnow_iso(),
        )
        try:
            loop = asyncio.get_running_loop()
            added = await loop.run_in_executor(None, scan_all)
            runtime_state.set_component(
                "scanner",
                last_scan_at=_utcnow_iso(),
                last_scan_added=added,
                last_scan_error=None,
                is_scanning=False,
                scan_started_at=None,
            )
            if added:
                _log_event(logging.INFO, "scan_complete", added=added)
        except Exception as e:
            runtime_state.set_component(
                "scanner",
                last_scan_at=_utcnow_iso(),
                last_scan_error=str(e),
                is_scanning=False,
                scan_started_at=None,
            )
            _log_event(logging.ERROR, "scan_error", error=str(e))

        # Sleep until next cadence tick, but wake early if sources.json changes
        last_rev = get_sources_revision()
        while time.time() < deadline:
            await asyncio.sleep(10)
            runtime_state.touch_worker("scanner")
            try:
                current_rev = get_sources_revision()
                if current_rev != last_rev:
                    runtime_state.set_component("scanner", next_scan_at=_utcnow_iso())
                    _log_event(logging.INFO, "scan_triggered", reason="sources_changed")
                    break
                last_rev = current_rev
            except OSError:
                pass


async def download_loop():
    """Watch for [ ] pending entries, download them one at a time."""
    from downloader import download_video

    # Reset any interrupted downloads on startup
    await reset_interrupted()
    runtime_state.touch_worker("downloader")

    while True:
        runtime_state.touch_worker("downloader")
        entry = claim_next_pending(actor="downloader")
        if not entry:
            await asyncio.sleep(5)
            continue

        vid = entry["video_id"]
        runtime_state.set_worker("downloader", active_video_id=vid)
        _log_event(logging.INFO, "download_claimed", video_id=vid)

        resolution = settings.get("preferred_resolution", ENV_DEFAULTS["preferred_resolution"])
        output_template = settings.get("output_template", ENV_DEFAULTS["output_template"])
        sleep_between = int(settings.get("sleep_between_downloads", ENV_DEFAULTS["sleep_between_downloads"]))

        try:
            loop = asyncio.get_running_loop()
            result = await loop.run_in_executor(
                None, download_video, vid, DOWNLOADS_DIR, resolution, output_template
            )

            if result["success"]:
                _log_event(
                    logging.INFO,
                    "download_complete",
                    video_id=vid,
                    title=result.get("title"),
                    file_path=result.get("file_path"),
                )
                record_download_success(vid)
            else:
                _log_event(
                    logging.ERROR,
                    "download_failed",
                    video_id=vid,
                    error=result.get("error"),
                )
                new_status = record_download_failure(vid, result.get("error", "download failed"))
                if new_status == "download_failed":
                    _log_event(logging.WARNING, "download_marked_failed", video_id=vid, attempts=3)
                if result.get("rate_limited"):
                    _log_event(logging.WARNING, "download_rate_limited", video_id=vid)
                await asyncio.sleep(60)

        except Exception as e:
            _log_event(logging.ERROR, "download_error", video_id=vid, error=str(e))
            new_status = record_download_failure(vid, str(e))
            if new_status == "download_failed":
                _log_event(logging.WARNING, "download_marked_failed", video_id=vid, attempts=3)
            await asyncio.sleep(60)
        finally:
            runtime_state.set_worker("downloader", active_video_id=None)

        await asyncio.sleep(sleep_between)


async def post_process_loop():
    """Watch for [p] processing entries, post-process them one at a time."""
    from processor import post_process_one

    while True:
        runtime_state.touch_worker("processor")
        processing = get_by_status("processing")
        if not processing:
            await asyncio.sleep(5)
            continue

        entry = processing[0]
        vid = entry["video_id"]
        runtime_state.set_worker("processor", active_video_id=vid)
        _log_event(logging.INFO, "process_claimed", video_id=vid)

        sponsorblock = settings.get("sponsorblock", ENV_DEFAULTS["sponsorblock"])
        global_sponsorblock = _coerce_bool(sponsorblock, True)
        use_sponsorblock = _source_sponsorblock_enabled(entry.get("channel", ""), global_sponsorblock)
        gemini_api_key = settings.get("gemini_api_key", ENV_DEFAULTS["gemini_api_key"])
        global_gemini_enabled = bool((gemini_api_key or "").strip())
        use_gemini_fallback = _source_gemini_fallback_enabled(entry.get("channel", ""), global_gemini_enabled)
        effective_gemini_key = gemini_api_key if use_gemini_fallback else ""

        try:
            success = await post_process_one(vid, DOWNLOADS_DIR, use_sponsorblock, effective_gemini_key)
            if success:
                record_process_success(vid)
                _log_event(logging.INFO, "process_complete", video_id=vid)
            else:
                new_status = record_process_failure(vid, "post-process returned false")
                if new_status == "process_failed":
                    _log_event(logging.WARNING, "process_marked_failed", video_id=vid, attempts=3)
                await asyncio.sleep(30)
        except Exception as e:
            _log_event(logging.ERROR, "process_error", video_id=vid, error=str(e))
            new_status = record_process_failure(vid, str(e))
            if new_status == "process_failed":
                _log_event(logging.WARNING, "process_marked_failed", video_id=vid, attempts=3)
            await asyncio.sleep(30)
        finally:
            runtime_state.set_worker("processor", active_video_id=None)


_VIDEO_EXTS = {".mp4", ".mkv", ".webm", ".mov", ".avi", ".flv", ".ts"}


def _find_video_dir(video_id: str) -> Path | None:
    """Locate the download folder for a video_id, or None."""
    if not DOWNLOADS_DIR.exists():
        return None
    for channel_dir in DOWNLOADS_DIR.iterdir():
        if not channel_dir.is_dir():
            continue
        video_dir = channel_dir / video_id
        if video_dir.exists() and video_dir.is_dir():
            return video_dir
    return None


def _has_video_file(video_dir: Path) -> bool:
    """Check if a folder contains any video files."""
    return any(f.suffix.lower() in _VIDEO_EXTS for f in video_dir.iterdir() if f.is_file())


async def cleanup_loop():
    """Periodically clean up done videos.

    Two passes:
    1. Orphan cleanup — if Plex deleted the video file but left the folder,
       remove the folder and mark [d].
    2. Retention cleanup — delete videos older than retention_days.
    """

    while True:
        runtime_state.touch_worker("cleanup")
        deleted_count = 0
        try:
            global_retention = int(settings.get("retention_days", ENV_DEFAULTS["retention_days"]))

            sources = []
            if SOURCES_FILE.exists():
                try:
                    sources = json.loads(SOURCES_FILE.read_text())
                except Exception:
                    pass

            # Build a map of channels with explicit source-level retention overrides
            source_retention_map: dict[str, int] = {}
            for s in sources:
                name = s.get("name", "")
                if name and s.get("retention_days") is not None:
                    source_retention_map[name] = int(s["retention_days"])

            entries = parse_queue()
            now = datetime.now()

            for entry in entries:
                if entry["status"] != "done":
                    continue

                vid = entry["video_id"]
                video_dir = _find_video_dir(vid)

                # Pass 1: orphan cleanup — folder missing or video file gone
                if not video_dir or not _has_video_file(video_dir):
                    if video_dir:
                        shutil.rmtree(video_dir)
                        _log_event(logging.INFO, "cleanup_removed_empty_folder", path=str(video_dir))
                    _log_event(logging.INFO, "cleanup_mark_deleted", video_id=vid, reason="orphan")
                    await mark_deleted(vid, actor="cleanup", reason="File missing — removed by Plex or an external process")
                    deleted_count += 1
                    continue

                # Pass 2: retention cleanup
                channel = entry["channel"]
                if channel in source_retention_map:
                    retention = source_retention_map[channel]
                    retention_label = f"Removed by source retention policy: older than {retention} days"
                else:
                    retention = global_retention
                    retention_label = f"Removed by global retention policy: older than {retention} days"
                cutoff = now - timedelta(days=retention)

                try:
                    entry_date = datetime.strptime(entry["date"], "%Y-%m-%d")
                except ValueError:
                    continue

                if entry_date > cutoff:
                    continue

                if video_dir:
                    shutil.rmtree(video_dir)
                    _log_event(logging.INFO, "cleanup_retention_delete_folder", path=str(video_dir))

                await mark_deleted(vid, actor="cleanup", reason=retention_label)
                _log_event(logging.INFO, "cleanup_mark_deleted", video_id=vid, reason="retention")
                deleted_count += 1

        except Exception as e:
            runtime_state.set_component(
                "cleanup",
                last_cleanup_at=_utcnow_iso(),
                last_cleanup_deleted=deleted_count,
                last_cleanup_error=str(e),
            )
            _log_event(logging.ERROR, "cleanup_error", error=str(e))
        else:
            runtime_state.set_component(
                "cleanup",
                last_cleanup_at=_utcnow_iso(),
                last_cleanup_deleted=deleted_count,
                last_cleanup_error=None,
            )
            _log_event(logging.INFO, "cleanup_complete", deleted=deleted_count)

        # Sleep until next 4 AM in the configured timezone
        tz_name = settings.get("cleanup_timezone", ENV_DEFAULTS["cleanup_timezone"])
        try:
            tz = ZoneInfo(tz_name)
        except Exception:
            tz = ZoneInfo("Europe/Amsterdam")
        now_tz = datetime.now(tz)
        target = now_tz.replace(hour=4, minute=0, second=0, microsecond=0)
        if now_tz >= target:
            target += timedelta(days=1)
        sleep_seconds = (target - now_tz).total_seconds()
        runtime_state.set_component("cleanup", next_cleanup_at=target.isoformat())
        elapsed = 0.0
        while elapsed < sleep_seconds:
            chunk = min(60.0, sleep_seconds - elapsed)
            await asyncio.sleep(chunk)
            runtime_state.touch_worker("cleanup")
            elapsed += chunk


async def main():
    """Start all services."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    run_api = _env_bool("RUN_API", True)
    run_workers = _env_bool("RUN_WORKERS", True)
    if not run_api and not run_workers:
        raise RuntimeError("At least one of RUN_API or RUN_WORKERS must be true")

    logger.info("Starting yt-plex")
    logger.info(f"  Mode: api={run_api}, workers={run_workers}")
    logger.info(f"  Downloads: {DOWNLOADS_DIR}")
    logger.info(f"  Scan interval: {settings.get('scan_interval', ENV_DEFAULTS['scan_interval'])}min")
    logger.info(f"  SponsorBlock: {settings.get('sponsorblock', ENV_DEFAULTS['sponsorblock'])}")
    logger.info(f"  Resolution: {settings.get('preferred_resolution', ENV_DEFAULTS['preferred_resolution'])}")
    logger.info(f"  Retention: {settings.get('retention_days', ENV_DEFAULTS['retention_days'])} days")
    gemini = settings.get("gemini_api_key", ENV_DEFAULTS["gemini_api_key"])
    logger.info(f"  Gemini fallback: {'enabled' if gemini else 'disabled'}")

    tasks = []
    if run_api:
        from server import app as web_app
        config = uvicorn.Config(web_app, host="0.0.0.0", port=8080, log_level="info")
        server = uvicorn.Server(config)
        tasks.append(server.serve())
    if run_workers:
        tasks.extend(
            [
                scanner_loop(),
                download_loop(),
                post_process_loop(),
                cleanup_loop(),
            ]
        )

    await asyncio.gather(*tasks)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, asyncio.CancelledError):
        logger.info("Shutting down")
