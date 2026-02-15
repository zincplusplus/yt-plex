"""Entry point: four service loops + web server."""

import asyncio
import json
import logging
import os
import shutil
import time
from datetime import datetime, timedelta
from pathlib import Path

import uvicorn
from dotenv import load_dotenv

import settings
from videoqueue import (
    parse_queue, get_pending, get_by_status, queue_lock,
    mark_downloading, mark_processing, mark_done, mark_deleted,
    mark_failed, mark_pending, mark_process_failed, reset_interrupted,
)

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("yt-plex")

DOWNLOADS_DIR = os.getenv("DOWNLOADS_DIR", "./downloads")
DATA_DIR = Path("data")
SOURCES_FILE = DATA_DIR / "sources.json"

ENV_DEFAULTS = {
    "scan_interval": os.getenv("SCAN_INTERVAL", "360"),
    "sleep_between_downloads": os.getenv("SLEEP_BETWEEN_DOWNLOADS", "5"),
    "sponsorblock": os.getenv("SPONSORBLOCK", "true"),
    "preferred_resolution": os.getenv("PREFERRED_RESOLUTION", "1080p"),
    "output_template": os.getenv("OUTPUT_TEMPLATE",
        "%(channel)s/%(id)s/%(title)s.%(ext)s"),
    "retention_days": os.getenv("RETENTION_DAYS", "14"),
    "gemini_api_key": os.getenv("GEMINI_API_KEY", ""),
}


async def scanner_loop():
    """Periodically scan all sources for new videos."""
    from scanner import scan_all

    while True:
        try:
            loop = asyncio.get_running_loop()
            added = await loop.run_in_executor(None, scan_all)
            if added:
                logger.info(f"Scanner: {added} new videos queued")
        except Exception as e:
            logger.error(f"Scanner error: {e}")

        # Sleep, but wake early if sources.json changes
        scan_interval = int(settings.get("scan_interval", ENV_DEFAULTS["scan_interval"]))
        last_mtime = SOURCES_FILE.stat().st_mtime if SOURCES_FILE.exists() else 0
        deadline = time.time() + scan_interval * 60
        while time.time() < deadline:
            await asyncio.sleep(10)
            try:
                current_mtime = SOURCES_FILE.stat().st_mtime if SOURCES_FILE.exists() else 0
                if current_mtime > last_mtime:
                    logger.info("sources.json changed — triggering scan")
                    break
                last_mtime = current_mtime
            except OSError:
                pass


async def download_loop():
    """Watch for [ ] pending entries, download them one at a time."""
    from downloader import download_video

    # Reset any interrupted downloads on startup
    await reset_interrupted()

    fail_counts: dict[str, int] = {}

    while True:
        pending = get_pending()
        if not pending:
            await asyncio.sleep(5)
            continue

        entry = pending[0]
        vid = entry["video_id"]

        resolution = settings.get("preferred_resolution", ENV_DEFAULTS["preferred_resolution"])
        output_template = settings.get("output_template", ENV_DEFAULTS["output_template"])
        sleep_between = int(settings.get("sleep_between_downloads", ENV_DEFAULTS["sleep_between_downloads"]))

        await mark_downloading(vid)

        try:
            loop = asyncio.get_running_loop()
            result = await loop.run_in_executor(
                None, download_video, vid, DOWNLOADS_DIR, resolution, output_template
            )

            if result["success"]:
                logger.info(f"Downloaded: {result.get('title')} -> {result.get('file_path')}")
                await mark_processing(vid)
                fail_counts.pop(vid, None)
            else:
                logger.error(f"Download failed for {vid}: {result.get('error')}")
                fail_counts[vid] = fail_counts.get(vid, 0) + 1
                if fail_counts[vid] >= 3:
                    logger.warning(f"Marking {vid} as failed after 3 consecutive failures")
                    await mark_failed(vid)
                    fail_counts.pop(vid, None)
                else:
                    await mark_pending(vid)
                if result.get("rate_limited"):
                    logger.warning("Rate limited — backing off")
                await asyncio.sleep(60)

        except Exception as e:
            logger.error(f"Download error for {vid}: {e}")
            fail_counts[vid] = fail_counts.get(vid, 0) + 1
            if fail_counts[vid] >= 3:
                logger.warning(f"Marking {vid} as failed after 3 consecutive failures")
                await mark_failed(vid)
                fail_counts.pop(vid, None)
            else:
                await mark_pending(vid)
            await asyncio.sleep(60)

        await asyncio.sleep(sleep_between)


async def post_process_loop():
    """Watch for [p] processing entries, post-process them one at a time."""
    from postprocessor import post_process_one

    fail_counts: dict[str, int] = {}

    while True:
        processing = get_by_status("processing")
        if not processing:
            await asyncio.sleep(5)
            continue

        entry = processing[0]
        vid = entry["video_id"]

        sponsorblock = settings.get("sponsorblock", ENV_DEFAULTS["sponsorblock"])
        use_sponsorblock = str(sponsorblock).lower() == "true"
        gemini_api_key = settings.get("gemini_api_key", ENV_DEFAULTS["gemini_api_key"])

        try:
            success = await post_process_one(vid, DOWNLOADS_DIR, use_sponsorblock, gemini_api_key)
            if success:
                await mark_done(vid)
                fail_counts.pop(vid, None)
                logger.info(f"Post-processing complete: {vid}")
            else:
                fail_counts[vid] = fail_counts.get(vid, 0) + 1
                if fail_counts[vid] >= 3:
                    logger.warning(f"Marking {vid} as process_failed after 3 failures")
                    await mark_process_failed(vid)
                    fail_counts.pop(vid, None)
                await asyncio.sleep(30)
        except Exception as e:
            logger.error(f"Post-process error for {vid}: {e}")
            fail_counts[vid] = fail_counts.get(vid, 0) + 1
            if fail_counts[vid] >= 3:
                logger.warning(f"Marking {vid} as process_failed after 3 failures")
                await mark_process_failed(vid)
                fail_counts.pop(vid, None)
            await asyncio.sleep(30)


async def cleanup_loop():
    """Periodically delete files older than retention_days, mark [d]."""

    while True:
        try:
            global_retention = int(settings.get("retention_days", ENV_DEFAULTS["retention_days"]))

            sources = []
            if SOURCES_FILE.exists():
                try:
                    sources = json.loads(SOURCES_FILE.read_text())
                except Exception:
                    pass

            retention_map = {}
            for s in sources:
                name = s.get("name", "")
                days = s.get("retention_days", global_retention)
                retention_map[name] = days

            entries = parse_queue()
            downloads = Path(DOWNLOADS_DIR)
            now = datetime.now()

            for entry in entries:
                if entry["status"] != "done":
                    continue

                retention = retention_map.get(entry["channel"], global_retention)
                cutoff = now - timedelta(days=retention)

                try:
                    entry_date = datetime.strptime(entry["date"], "%Y-%m-%d")
                except ValueError:
                    continue

                if entry_date > cutoff:
                    continue

                vid = entry["video_id"]
                deleted = False
                if downloads.exists():
                    deleted_dirs = set()
                    for f in downloads.rglob(f"*{vid}*"):
                        if f.is_file():
                            parent = f.parent
                            if parent != downloads and parent.parent != downloads:
                                if parent not in deleted_dirs:
                                    shutil.rmtree(parent)
                                    logger.info(f"Cleanup: deleted folder {parent}")
                                    deleted_dirs.add(parent)
                            else:
                                f.unlink()
                                logger.info(f"Cleanup: deleted {f}")
                            deleted = True

                if deleted:
                    await mark_deleted(vid)
                    logger.info(f"Cleanup: marked {vid} as deleted")

        except Exception as e:
            logger.error(f"Cleanup error: {e}")

        # Run once per day
        await asyncio.sleep(86400)


async def main():
    """Start all services."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    from server import app as web_app

    config = uvicorn.Config(web_app, host="0.0.0.0", port=8080, log_level="info")
    server = uvicorn.Server(config)

    logger.info("Starting yt-plex")
    logger.info(f"  Downloads: {DOWNLOADS_DIR}")
    logger.info(f"  Scan interval: {settings.get('scan_interval', ENV_DEFAULTS['scan_interval'])}min")
    logger.info(f"  SponsorBlock: {settings.get('sponsorblock', ENV_DEFAULTS['sponsorblock'])}")
    logger.info(f"  Resolution: {settings.get('preferred_resolution', ENV_DEFAULTS['preferred_resolution'])}")
    logger.info(f"  Retention: {settings.get('retention_days', ENV_DEFAULTS['retention_days'])} days")
    gemini = settings.get("gemini_api_key", ENV_DEFAULTS["gemini_api_key"])
    logger.info(f"  Gemini fallback: {'enabled' if gemini else 'disabled'}")

    await asyncio.gather(
        server.serve(),
        scanner_loop(),
        download_loop(),
        post_process_loop(),
        cleanup_loop(),
    )


if __name__ == "__main__":
    asyncio.run(main())
