import json
import logging
import os
from datetime import datetime, timedelta
from pathlib import Path

from ..core import database as db
from ..core.config import settings

logger = logging.getLogger(__name__)


async def cleanup_old_videos():
    """Delete videos that are older than their retention period."""
    global_delete_days = settings.default_delete_after_days

    # Get all completed videos with download dates
    videos = await db.get_videos_for_cleanup(global_delete_days)

    now = datetime.utcnow()
    deleted_count = 0
    deleted_videos = []
    freed_bytes = 0

    for video in videos:
        downloaded_at_str = video.get('downloaded_at')
        if not downloaded_at_str:
            continue

        try:
            downloaded_at = datetime.fromisoformat(downloaded_at_str)
        except ValueError:
            continue

        # Determine retention period (channel-specific or global)
        delete_after = video.get('channel_delete_days') or global_delete_days
        if delete_after is None or delete_after <= 0:
            continue  # No auto-delete

        expiry_date = downloaded_at + timedelta(days=delete_after)

        if now >= expiry_date:
            video_id = video['video_id']
            file_path = video.get('file_path')
            age_days = (now - downloaded_at).days

            # Delete the file
            if file_path and os.path.exists(file_path):
                try:
                    file_size = os.path.getsize(file_path)
                    freed_bytes += file_size
                    os.remove(file_path)
                    logger.info(f"Deleted file: {file_path}")

                    # Also try to delete associated subtitle files
                    base_path = Path(file_path).with_suffix('')
                    for ext in ['.srt', '.vtt', '.ass', '.en.srt', '.en.vtt', '.jpg', '.webp', '.png']:
                        sub_file = Path(str(base_path) + ext)
                        if sub_file.exists():
                            freed_bytes += sub_file.stat().st_size
                            os.remove(sub_file)

                except Exception as e:
                    logger.error(f"Error deleting file {file_path}: {e}")

            # Update video status
            await db.update_video(video_id, status='deleted', file_path=None)
            deleted_count += 1
            deleted_videos.append({
                "title": video.get('title', 'Unknown'),
                "age_days": age_days,
                "max_days": delete_after
            })

    if deleted_count > 0:
        logger.info(f"Cleanup complete. Deleted {deleted_count} videos.")
        await db.create_log_entry("cleanup", "Cleanup completed", json.dumps({
            "deleted_videos": deleted_videos,
            "total_deleted": deleted_count,
            "freed_bytes": freed_bytes
        }))

    return deleted_count


async def get_disk_usage() -> dict:
    """Get disk usage statistics for the downloads directory."""
    downloads_dir = settings.downloads_dir

    if not downloads_dir.exists():
        return {'total_size': 0, 'file_count': 0}

    total_size = 0
    file_count = 0

    for path in downloads_dir.rglob('*'):
        if path.is_file():
            total_size += path.stat().st_size
            file_count += 1

    return {
        'total_size': total_size,
        'total_size_gb': round(total_size / (1024**3), 2),
        'file_count': file_count
    }
