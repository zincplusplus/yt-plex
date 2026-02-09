import json
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from typing import Optional
import asyncio

from ..core import database as db
from ..core.downloader import get_video_info, process_download_queue

router = APIRouter(prefix="/api/downloads", tags=["downloads"])


class ManualDownload(BaseModel):
    url: str


@router.get("")
async def list_downloads(status: Optional[str] = None, limit: int = 50):
    """List downloads, optionally filtered by status."""
    if status:
        videos = await db.get_videos_by_status(status, limit=limit)
    else:
        videos = await db.get_recent_videos(limit=limit)
    return {"videos": videos}


@router.get("/queue")
async def get_queue():
    """Get current download queue status."""
    pending = await db.get_videos_by_status('pending', limit=100)
    downloading = await db.get_videos_by_status('downloading', limit=10)
    return {
        "pending": pending,
        "downloading": downloading,
        "pending_count": len(pending),
        "downloading_count": len(downloading)
    }


@router.post("/manual")
async def add_manual_download(request: ManualDownload):
    """Add a video to the download queue by URL."""
    try:
        # Get video info
        video_info = await asyncio.get_event_loop().run_in_executor(
            None,
            lambda: get_video_info(request.url)
        )

        video_id = video_info['video_id']

        # Check if already exists
        existing = await db.get_video(video_id)
        if existing:
            if existing['status'] == 'completed':
                raise HTTPException(status_code=400, detail="Video already downloaded")
            elif existing['status'] == 'pending':
                raise HTTPException(status_code=400, detail="Video already in queue")
            elif existing['status'] == 'downloading':
                raise HTTPException(status_code=400, detail="Video is currently downloading")
            elif existing['status'] == 'failed':
                # Retry failed download
                await db.update_video(video_id, status='pending', error_message=None)
                return {"message": "Retrying failed download", "video_id": video_id}

        # Add to queue
        await db.create_video(
            channel_id=None,
            video_id=video_id,
            title=video_info['title'],
            description=video_info.get('description', '')
        )

        await db.create_log_entry("queued", video_info['title'], json.dumps({
            "video_id": video_id,
            "source": "manual",
            "url": request.url
        }))

        return {
            "message": "Video added to queue",
            "video_id": video_id,
            "title": video_info['title']
        }

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Could not fetch video info: {e}")


@router.post("/{video_id}/retry")
async def retry_download(video_id: str):
    """Retry a failed download."""
    video = await db.get_video(video_id)
    if not video:
        raise HTTPException(status_code=404, detail="Video not found")

    if video['status'] != 'failed':
        raise HTTPException(status_code=400, detail=f"Cannot retry video with status: {video['status']}")

    await db.update_video(video_id, status='pending', error_message=None)
    return {"message": "Download queued for retry"}


@router.post("/{video_id}/cancel")
async def cancel_download(video_id: str):
    """Cancel a pending download."""
    video = await db.get_video(video_id)
    if not video:
        raise HTTPException(status_code=404, detail="Video not found")

    if video['status'] != 'pending':
        raise HTTPException(status_code=400, detail=f"Cannot cancel video with status: {video['status']}")

    await db.delete_video(video_id)
    return {"message": "Download cancelled"}


@router.delete("/{video_id}")
async def delete_video(video_id: str):
    """Delete a video record and its file."""
    video = await db.get_video(video_id)
    if not video:
        raise HTTPException(status_code=404, detail="Video not found")

    # Delete file if exists
    file_path = video.get('file_path')
    if file_path:
        import os
        if os.path.exists(file_path):
            os.remove(file_path)

    await db.delete_video(video_id)
    return {"message": "Video deleted"}


@router.post("/process")
async def trigger_process():
    """Manually trigger download queue processing."""
    await process_download_queue()
    return {"message": "Queue processing triggered"}
