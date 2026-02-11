from fastapi import APIRouter
from pydantic import BaseModel
from typing import Optional

from ..core import database as db
from ..core.config import settings
from ..services.cleanup import get_disk_usage, cleanup_old_videos
from ..services.channel_scanner import scan_all_channels

router = APIRouter(prefix="/api/settings", tags=["settings"])


class GlobalSettings(BaseModel):
    default_download_format: Optional[str] = None
    default_sponsorblock_categories: Optional[str] = None
    default_subtitle_langs: Optional[str] = None
    default_scan_interval_hours: Optional[int] = None
    default_delete_after_days: Optional[int] = None
    use_gemini_sponsorblock: Optional[bool] = None


@router.get("")
async def get_settings():
    """Get all settings."""
    db_settings = await db.get_all_settings()

    return {
        "default_download_format": db_settings.get(
            "default_download_format",
            settings.default_download_format
        ),
        "default_sponsorblock_categories": db_settings.get(
            "default_sponsorblock_categories",
            settings.default_sponsorblock_categories
        ),
        "default_subtitle_langs": db_settings.get(
            "default_subtitle_langs",
            settings.default_subtitle_langs
        ),
        "default_scan_interval_hours": int(db_settings.get(
            "default_scan_interval_hours",
            settings.default_scan_interval_hours
        )),
        "default_delete_after_days": int(db_settings.get(
            "default_delete_after_days",
            settings.default_delete_after_days
        )),
        "use_gemini_sponsorblock": db_settings.get(
            "use_gemini_sponsorblock", "false"
        ) == "true",
    }


@router.put("")
async def update_settings(update: GlobalSettings):
    """Update global settings."""
    update_data = update.model_dump(exclude_unset=True)

    for key, value in update_data.items():
        if value is not None:
            if isinstance(value, bool):
                await db.set_setting(key, "true" if value else "false")
            else:
                await db.set_setting(key, str(value))

    return {"message": "Settings updated successfully"}


@router.get("/stats")
async def get_stats():
    """Get dashboard statistics."""
    stats = await db.get_stats()
    disk = await get_disk_usage()

    return {
        **stats,
        "disk_usage_gb": disk['total_size_gb'],
        "file_count": disk['file_count']
    }


@router.post("/scan-all")
async def trigger_scan_all():
    """Manually trigger a scan of all channels."""
    added = await scan_all_channels()
    return {"message": f"Scan complete. Added {added} videos to queue."}


@router.post("/cleanup")
async def trigger_cleanup():
    """Manually trigger cleanup of old videos."""
    deleted = await cleanup_old_videos()
    return {"message": f"Cleanup complete. Deleted {deleted} videos."}
