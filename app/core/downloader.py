import json
import os
import subprocess
import yt_dlp
import asyncio
from pathlib import Path
from datetime import datetime
from typing import Optional
from urllib.request import urlopen, Request
from urllib.error import URLError, HTTPError
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


def check_sponsorblock_api(video_id: str) -> bool:
    """Check if SponsorBlock has any segments for this video."""
    try:
        url = f"https://sponsor.ajay.app/api/skipSegments?videoID={video_id}"
        req = Request(url, headers={"User-Agent": "yt-plex/1.0"})
        with urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
            return len(data) > 0
    except (HTTPError, URLError, json.JSONDecodeError):
        return False
    except Exception:
        return False


def find_sponsors_with_gemini(vtt_content: str, api_key: str) -> list[dict]:
    """Use Gemini AI to identify sponsor segments from subtitles."""
    import google.generativeai as genai

    genai.configure(api_key=api_key)
    model = genai.GenerativeModel('gemini-2.0-flash')

    prompt = """You are analyzing YouTube video subtitles to find sponsor/ad segments.

Identify all sponsored ad reads — segments where the host is promoting a product or service.

Include the full segment from when they transition INTO the ad read to when they return to regular content. Err on the side of starting a few seconds early rather than late.

Return JSON only, no other text:
[{"sponsor": "Brand Name", "start_seconds": 123.4, "end_seconds": 189.2}]

If no sponsors found, return: []

Subtitles:
""" + vtt_content

    response = model.generate_content(prompt)
    # Strip markdown code fences if present
    text = response.text.strip()
    if text.startswith("```"):
        text = re.sub(r'^```(?:json)?\s*', '', text)
        text = re.sub(r'\s*```$', '', text)
    return json.loads(text)


def cut_segments(video_path: str, segments: list[dict]) -> bool:
    """Use ffmpeg to cut sponsor segments from the video."""
    if not segments:
        return False

    video = Path(video_path)
    if not video.exists():
        logger.error(f"Video file not found: {video_path}")
        return False

    # Get video duration using ffprobe
    try:
        result = subprocess.run(
            ['ffprobe', '-v', 'quiet', '-show_entries', 'format=duration',
             '-of', 'default=noprint_wrappers=1:nokey=1', str(video)],
            capture_output=True, text=True, timeout=30
        )
        duration = float(result.stdout.strip())
    except Exception as e:
        logger.error(f"Failed to get video duration: {e}")
        return False

    # Sort segments by start time
    segments = sorted(segments, key=lambda s: s['start_seconds'])

    # Build list of keep-segments (everything NOT in a sponsor segment)
    keep_parts = []
    current_pos = 0.0
    for seg in segments:
        start = seg['start_seconds']
        end = seg['end_seconds']
        if start > current_pos:
            keep_parts.append((current_pos, start))
        current_pos = max(current_pos, end)
    if current_pos < duration:
        keep_parts.append((current_pos, duration))

    if not keep_parts:
        logger.warning("No content would remain after cutting — skipping")
        return False

    # Build ffmpeg filter_complex for segment selection
    temp_path = video.with_suffix('.tmp.mp4')
    filter_parts = []
    concat_inputs = []
    for i, (start, end) in enumerate(keep_parts):
        filter_parts.append(
            f"[0:v]trim=start={start}:end={end},setpts=PTS-STARTPTS[v{i}];"
            f"[0:a]atrim=start={start}:end={end},asetpts=PTS-STARTPTS[a{i}]"
        )
        concat_inputs.append(f"[v{i}][a{i}]")

    filter_complex = ";".join(filter_parts)
    filter_complex += f";{''.join(concat_inputs)}concat=n={len(keep_parts)}:v=1:a=1[outv][outa]"

    cmd = [
        'ffmpeg', '-y', '-i', str(video),
        '-filter_complex', filter_complex,
        '-map', '[outv]', '-map', '[outa]',
        '-c:v', 'libx264', '-preset', 'fast', '-crf', '18',
        '-c:a', 'aac', '-b:a', '192k',
        str(temp_path)
    ]

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
        if result.returncode != 0:
            logger.error(f"ffmpeg failed: {result.stderr[-500:]}")
            temp_path.unlink(missing_ok=True)
            return False

        # Replace original with cut version
        temp_path.replace(video)
        logger.info(f"Successfully cut {len(segments)} sponsor segment(s) from {video.name}")
        return True

    except subprocess.TimeoutExpired:
        logger.error("ffmpeg timed out")
        temp_path.unlink(missing_ok=True)
        return False
    except Exception as e:
        logger.error(f"ffmpeg error: {e}")
        temp_path.unlink(missing_ok=True)
        return False


def gemini_sponsorblock_fallback(video_id: str, video_path: str) -> None:
    """Check SponsorBlock API first; if no data, use Gemini AI to find and cut sponsors."""
    api_key = settings.gemini_api_key
    if not api_key:
        logger.warning("Gemini API key not configured, skipping AI sponsorblock fallback")
        return

    # Check if SponsorBlock already has data for this video
    if check_sponsorblock_api(video_id):
        logger.info(f"SponsorBlock has data for {video_id}, skipping Gemini fallback")
        return

    logger.info(f"No SponsorBlock data for {video_id}, using Gemini AI fallback")

    # Find the VTT subtitle file
    # yt-dlp writes subtitles as "Title [id].en.vtt" alongside "Title [id].mp4"
    video = Path(video_path)
    stem = video.stem  # e.g. "Title [id]"
    vtt_path = None
    # Check common patterns: stem.en.vtt, stem.vtt, stem.*.vtt
    for candidate in [
        video.parent / f"{stem}.en.vtt",
        video.parent / f"{stem}.vtt",
    ]:
        if candidate.exists():
            vtt_path = candidate
            break
    if not vtt_path:
        for f in video.parent.glob(f"{stem}.*.vtt"):
            vtt_path = f
            break

    if not vtt_path:
        logger.warning(f"No VTT subtitle file found for {video_path}, skipping Gemini fallback")
        return

    try:
        vtt_content = vtt_path.read_text(encoding='utf-8')
    except Exception as e:
        logger.error(f"Failed to read VTT file: {e}")
        return

    try:
        segments = find_sponsors_with_gemini(vtt_content, api_key)
    except Exception as e:
        logger.error(f"Gemini AI sponsor detection failed: {e}")
        return

    if not segments:
        logger.info(f"Gemini found no sponsor segments in {video_id}")
        return

    sponsor_names = [s.get('sponsor', 'Unknown') for s in segments]
    logger.info(f"Gemini found {len(segments)} sponsor segment(s) in {video_id}: {', '.join(sponsor_names)}")

    cut_segments(video_path, segments)


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
        # Check if Gemini SponsorBlock fallback is enabled for this channel
        use_gemini = None
        if channel:
            use_gemini = channel.get('use_gemini_sponsorblock')  # NULL, 0, or 1
        if use_gemini is None:
            # Fall back to global setting
            global_setting = await db.get_setting('use_gemini_sponsorblock', 'false')
            use_gemini = global_setting == 'true'
        else:
            use_gemini = bool(use_gemini)

        if use_gemini and result.get('file_path'):
            await asyncio.get_event_loop().run_in_executor(
                None,
                lambda: gemini_sponsorblock_fallback(video_id, result['file_path'])
            )

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
