import json
import os
import yt_dlp
import asyncio
from pathlib import Path
from datetime import datetime
from typing import Optional
import logging
import re

from .config import settings
from . import database as db

logger = logging.getLogger(__name__)


def sanitize_filename(name: str) -> str:
    """Remove invalid characters from filename."""
    return re.sub(r'[<>:"/\\|?*]', '', name)[:200]


def get_channel_info(url: str) -> dict:
    """Extract channel information from URL."""
    ydl_opts = {
        'quiet': True,
        'no_warnings': True,
        'extract_flat': True,
        'playlist_items': '0',
    }

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        try:
            info = ydl.extract_info(url, download=False)
            return {
                'name': info.get('channel') or info.get('uploader') or info.get('title', 'Unknown'),
                'channel_id': info.get('channel_id') or info.get('uploader_id') or info.get('id'),
                'url': info.get('channel_url') or info.get('uploader_url') or url,
            }
        except Exception as e:
            logger.error(f"Error extracting channel info: {e}")
            raise


def get_channel_videos(channel_url: str, limit: int = 50) -> list[dict]:
    """Get recent videos from a channel."""
    ydl_opts = {
        'quiet': True,
        'no_warnings': True,
        'extract_flat': True,
        'playlist_items': f'1:{limit}',
    }

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        try:
            # Handle different URL formats
            if '/videos' not in channel_url:
                if channel_url.endswith('/'):
                    channel_url = channel_url + 'videos'
                else:
                    channel_url = channel_url + '/videos'

            info = ydl.extract_info(channel_url, download=False)
            entries = info.get('entries', [])

            videos = []
            for entry in entries:
                if entry:
                    videos.append({
                        'video_id': entry.get('id'),
                        'title': entry.get('title'),
                        'url': entry.get('url') or f"https://www.youtube.com/watch?v={entry.get('id')}",
                    })
            return videos
        except Exception as e:
            logger.error(f"Error extracting channel videos: {e}")
            raise


def get_video_info(video_url: str) -> dict:
    """Get detailed video information."""
    ydl_opts = {
        'quiet': True,
        'no_warnings': True,
    }

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        try:
            info = ydl.extract_info(video_url, download=False)
            return {
                'video_id': info.get('id'),
                'title': info.get('title'),
                'description': info.get('description', ''),
                'channel': info.get('channel') or info.get('uploader'),
                'channel_id': info.get('channel_id'),
                'duration': info.get('duration'),
                'upload_date': info.get('upload_date'),
            }
        except Exception as e:
            logger.error(f"Error extracting video info: {e}")
            raise


def download_video(
    video_url: str,
    output_dir: Path,
    download_format: str = None,
    sponsorblock_categories: list[str] = None,
    subtitle_langs: list[str] = None,
    channel_name: str = None,
) -> dict:
    """Download a video with specified options."""
    download_format = download_format or settings.default_download_format
    sponsorblock_categories = sponsorblock_categories or settings.default_sponsorblock_categories.split(',')
    subtitle_langs = subtitle_langs or settings.default_subtitle_langs.split(',')

    # Create channel subdirectory
    if channel_name:
        output_dir = output_dir / sanitize_filename(channel_name)
    output_dir.mkdir(parents=True, exist_ok=True)

    output_template = str(output_dir / '%(title)s [%(id)s].%(ext)s')

    ydl_opts = {
        'format': download_format,
        'outtmpl': output_template,
        'merge_output_format': 'mp4',
        'postprocessors': [],
        'quiet': False,
        'no_warnings': False,
    }

    # Add SponsorBlock
    if sponsorblock_categories:
        ydl_opts['postprocessors'].append({
            'key': 'SponsorBlock',
            'categories': sponsorblock_categories,
        })
        ydl_opts['postprocessors'].append({
            'key': 'ModifyChapters',
            'remove_sponsor_segments': sponsorblock_categories,
            'force_keyframes': True,
        })

    # Add subtitles
    if subtitle_langs:
        ydl_opts['writesubtitles'] = True
        ydl_opts['subtitleslangs'] = subtitle_langs
        ydl_opts['postprocessors'].append({
            'key': 'FFmpegEmbedSubtitle',
        })

    # Add metadata
    ydl_opts['postprocessors'].append({
        'key': 'FFmpegMetadata',
    })

    # Add thumbnail
    ydl_opts['writethumbnail'] = True
    ydl_opts['postprocessors'].append({
        'key': 'FFmpegThumbnailsConvertor',
        'format': 'jpg',
    })

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        try:
            info = ydl.extract_info(video_url, download=True)
            # Get the actual file path
            filename = ydl.prepare_filename(info)
            # Handle extension change from merge
            final_path = Path(filename).with_suffix('.mp4')
            if not final_path.exists():
                final_path = Path(filename)

            return {
                'success': True,
                'video_id': info.get('id'),
                'title': info.get('title'),
                'file_path': str(final_path),
            }
        except Exception as e:
            logger.error(f"Error downloading video: {e}")
            return {
                'success': False,
                'error': str(e),
            }


async def process_download_queue():
    """Process pending downloads from the queue."""
    pending = await db.get_videos_by_status('pending', limit=1)

    if not pending:
        return

    video = pending[0]
    video_id = video['video_id']
    channel_id = video['channel_id']

    logger.info(f"Processing download: {video['title']} ({video_id})")

    # Update status to downloading
    await db.update_video(video_id, status='downloading')

    # Get channel settings
    channel = await db.get_channel(channel_id) if channel_id else None
    channel_name = channel.get('name') if channel else None

    download_format = channel.get('download_format') if channel else settings.default_download_format
    sponsorblock = (channel.get('sponsorblock_remove') if channel else settings.default_sponsorblock_categories) or ''
    subtitle_langs = (channel.get('subtitle_langs') if channel else settings.default_subtitle_langs) or 'en'

    await db.create_log_entry("downloading", video['title'], json.dumps({
        "video_id": video_id,
        "channel_name": channel_name,
        "format": download_format,
        "subtitle_langs": subtitle_langs
    }))

    result = await asyncio.get_event_loop().run_in_executor(
        None,
        lambda: download_video(
            f"https://www.youtube.com/watch?v={video_id}",
            settings.downloads_dir,
            download_format=download_format,
            sponsorblock_categories=[s.strip() for s in sponsorblock.split(',') if s.strip()],
            subtitle_langs=[s.strip() for s in subtitle_langs.split(',') if s.strip()],
            channel_name=channel_name,
        )
    )

    if result['success']:
        await db.update_video(
            video_id,
            status='completed',
            file_path=result['file_path'],
            downloaded_at=datetime.utcnow().isoformat()
        )
        file_size = None
        if result.get('file_path') and os.path.exists(result['file_path']):
            file_size = os.path.getsize(result['file_path'])
        await db.create_log_entry("downloaded", video['title'], json.dumps({
            "video_id": video_id,
            "channel_name": channel_name,
            "file_path": result['file_path'],
            "file_size": file_size,
            "format": download_format
        }))
        logger.info(f"Download completed: {video['title']}")
    else:
        error_msg = result.get('error', 'Unknown error')
        await db.update_video(
            video_id,
            status='failed',
            error_message=error_msg
        )
        await db.create_log_entry("failed", video['title'], json.dumps({
            "video_id": video_id,
            "channel_name": channel_name,
            "error": error_msg
        }))
        logger.error(f"Download failed: {video['title']} - {error_msg}")
