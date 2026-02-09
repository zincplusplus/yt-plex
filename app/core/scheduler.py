import logging
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger

from .config import settings
from .downloader import process_download_queue
from ..services.channel_scanner import scan_all_channels
from ..services.cleanup import cleanup_old_videos

logger = logging.getLogger(__name__)

scheduler = AsyncIOScheduler()


def init_scheduler():
    """Initialize and start the scheduler with all jobs."""

    # Process download queue every minute
    scheduler.add_job(
        process_download_queue,
        trigger=IntervalTrigger(seconds=settings.download_queue_interval_seconds),
        id='process_downloads',
        name='Process download queue',
        replace_existing=True,
    )

    # Scan channels for new videos (runs every hour, but respects per-channel intervals)
    scheduler.add_job(
        scan_all_channels,
        trigger=IntervalTrigger(hours=1),
        id='scan_channels',
        name='Scan channels for new videos',
        replace_existing=True,
    )

    # Cleanup old videos daily
    scheduler.add_job(
        cleanup_old_videos,
        trigger=IntervalTrigger(hours=settings.cleanup_interval_hours),
        id='cleanup_videos',
        name='Cleanup old videos',
        replace_existing=True,
    )

    scheduler.start()
    logger.info("Scheduler started with all jobs")


def shutdown_scheduler():
    """Shutdown the scheduler."""
    scheduler.shutdown(wait=False)
    logger.info("Scheduler stopped")
