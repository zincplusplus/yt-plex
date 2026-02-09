import json
import logging
import asyncio
from datetime import datetime, timedelta

from ..core import database as db
from ..core.downloader import get_channel_videos, get_video_info
from .video_filter import check_video_filters

logger = logging.getLogger(__name__)


async def scan_channel(channel: dict) -> int:
    """
    Scan a channel for new videos and add matching ones to the queue.

    Returns:
        Number of videos added to queue
    """
    channel_id = channel['id']
    channel_url = channel['url']
    channel_name = channel['name']
    download_since = channel.get('download_since')

    # If download_since is set, fetch more videos to go back further in time
    fetch_limit = 200 if download_since else 30

    logger.info(f"Scanning channel: {channel_name} (limit={fetch_limit})")

    # Parse the date cutoff once
    since_date = None
    if download_since:
        try:
            since_date = datetime.strptime(download_since, '%Y-%m-%d')
        except ValueError:
            logger.warning(f"Invalid download_since date '{download_since}' for channel {channel_name}")

    # Build active filters for scan_start log
    active_filters = {}
    if channel.get('title_filter'):
        active_filters['title_filter'] = channel['title_filter']
    if channel.get('description_filter'):
        active_filters['description_filter'] = channel['description_filter']
    if channel.get('description_exclude'):
        active_filters['description_exclude'] = channel['description_exclude']
    if download_since:
        active_filters['download_since'] = download_since

    await db.create_log_entry("scan_start", channel_name, json.dumps({
        "url": channel_url,
        "fetch_limit": fetch_limit,
        "filters": active_filters
    }))

    try:
        # Get recent videos from channel
        videos = await asyncio.get_event_loop().run_in_executor(
            None,
            lambda: get_channel_videos(channel_url, limit=fetch_limit)
        )

        added_count = 0
        queued_videos = []
        filtered_out = []
        already_tracked = 0
        too_old = 0

        for video_data in videos:
            video_id = video_data['video_id']

            # Check if video already exists in database
            existing = await db.get_video(video_id)
            if existing:
                already_tracked += 1
                continue

            title = video_data['title']

            # Quick filter on title first
            title_filter = channel.get('title_filter')
            if title_filter:
                passes, reason = check_video_filters(title, '', title_filter=title_filter)
                if not passes:
                    logger.debug(f"Video filtered out: {title} - {reason}")
                    filtered_out.append({"title": title, "reason": reason})
                    continue

            # If we have description filters or a date cutoff, fetch full video info
            description = ''
            desc_filter = channel.get('description_filter')
            desc_exclude = channel.get('description_exclude')
            needs_full_info = desc_filter or desc_exclude or since_date

            if needs_full_info:
                try:
                    video_info = await asyncio.get_event_loop().run_in_executor(
                        None,
                        lambda vid=video_id: get_video_info(f"https://www.youtube.com/watch?v={vid}")
                    )
                    description = video_info.get('description', '')

                    # Check date cutoff
                    if since_date:
                        upload_date_str = video_info.get('upload_date')  # format: YYYYMMDD
                        if upload_date_str:
                            try:
                                upload_date = datetime.strptime(upload_date_str, '%Y%m%d')
                                if upload_date < since_date:
                                    logger.debug(f"Video too old: {title} ({upload_date_str})")
                                    too_old += 1
                                    continue
                            except ValueError:
                                pass  # Can't parse date, include it anyway

                except Exception as e:
                    logger.warning(f"Could not fetch video info for {video_id}: {e}")

                # Check description filters
                if desc_filter or desc_exclude:
                    passes, reason = check_video_filters(
                        title,
                        description,
                        description_filter=desc_filter,
                        description_exclude=desc_exclude
                    )
                    if not passes:
                        logger.debug(f"Video filtered out: {title} - {reason}")
                        filtered_out.append({"title": title, "reason": reason})
                        continue

            # Add video to queue
            await db.create_video(
                channel_id=channel_id,
                video_id=video_id,
                title=title,
                description=description
            )
            added_count += 1
            queued_videos.append({"title": title, "video_id": video_id})
            logger.info(f"Added to queue: {title}")

        # Update last scanned time
        await db.update_channel(channel_id, last_scanned_at=datetime.utcnow().isoformat())

        await db.create_log_entry("scan_done", channel_name, json.dumps({
            "total_found": len(videos),
            "queued_videos": queued_videos,
            "filtered_out": filtered_out,
            "already_tracked": already_tracked,
            "too_old": too_old
        }))

        return added_count

    except Exception as e:
        logger.error(f"Error scanning channel {channel_name}: {e}")
        return 0


async def scan_all_channels():
    """Scan all enabled channels that are due for scanning."""
    channels = await db.get_enabled_channels()

    now = datetime.utcnow()
    total_added = 0

    for channel in channels:
        # Check if channel is due for scanning
        last_scanned = channel.get('last_scanned_at')
        scan_interval = channel.get('scan_interval_hours', 6)

        if last_scanned:
            try:
                last_scanned_dt = datetime.fromisoformat(last_scanned)
                next_scan = last_scanned_dt + timedelta(hours=scan_interval)
                if now < next_scan:
                    continue  # Not due yet
            except ValueError:
                pass  # Invalid date, scan anyway

        added = await scan_channel(channel)
        total_added += added

    if total_added > 0:
        logger.info(f"Channel scan complete. Added {total_added} videos to queue.")

    return total_added
