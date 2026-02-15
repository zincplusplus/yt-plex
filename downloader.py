"""Download videos from the queue via yt-dlp."""

import logging
from pathlib import Path

import yt_dlp

logger = logging.getLogger(__name__)


def build_format_selector(resolution: str) -> str:
    """Build yt-dlp format string for H.264 at the given resolution ceiling."""
    height_map = {"1080p": 1080, "720p": 720, "480p": 480, "360p": 360}
    height = height_map.get(resolution, 1080)
    return (
        f"bestvideo[vcodec^=avc1][height<={height}]+bestaudio[acodec^=mp4a]"
        f"/best[vcodec^=avc1][height<={height}]"
    )


def download_video(video_id: str, downloads_dir: str, resolution: str, output_template: str) -> dict:
    """Download a single video. Returns result dict."""
    output_dir = Path(downloads_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    full_template = str(output_dir / output_template)

    url = f"https://www.youtube.com/watch?v={video_id}"

    ydl_opts = {
        "format": build_format_selector(resolution),
        "outtmpl": full_template,
        "merge_output_format": "mp4",
        "quiet": True,
        "no_warnings": True,
        "sleep_interval": 3,
        "max_sleep_interval": 10,
        "sleep_interval_requests": 1,
        "writesubtitles": True,
        "writeautomaticsub": True,
        "subtitleslangs": ["en"],
        "writethumbnail": True,
        "writeinfojson": True,
        "postprocessors": [
            {"key": "FFmpegEmbedSubtitle"},
        ],
    }

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            filename = ydl.prepare_filename(info)
            final_path = Path(filename).with_suffix(".mp4")
            if not final_path.exists():
                final_path = Path(filename)
            return {
                "success": True,
                "file_path": str(final_path),
                "title": info.get("title"),
                "description": info.get("description", ""),
                "upload_date": info.get("upload_date", ""),
                "channel": info.get("channel") or info.get("uploader", ""),
                "duration": info.get("duration"),
                "video_id": info.get("id", video_id),
            }
    except Exception as e:
        error_str = str(e)
        rate_limited = any(s in error_str.lower() for s in [
            "http error 429", "sign in to confirm", "http error 403",
        ])
        return {"success": False, "error": error_str, "rate_limited": rate_limited}
