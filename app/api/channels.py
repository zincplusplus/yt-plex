import json
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from typing import Optional
import asyncio

from ..core import database as db
from ..core.downloader import get_channel_info
from ..services.channel_scanner import scan_channel

router = APIRouter(prefix="/api/channels", tags=["channels"])


class ChannelCreate(BaseModel):
    url: str
    enabled: bool = True
    scan_interval_hours: int = 6
    download_format: Optional[str] = None
    sponsorblock_remove: Optional[str] = "sponsor,selfpromo,interaction"
    subtitle_langs: Optional[str] = "en"
    title_filter: Optional[str] = None
    description_filter: Optional[str] = None
    description_exclude: Optional[str] = None
    download_since: Optional[str] = None  # YYYY-MM-DD
    delete_after_days: Optional[int] = None


class ChannelUpdate(BaseModel):
    name: Optional[str] = None
    enabled: Optional[bool] = None
    scan_interval_hours: Optional[int] = None
    download_format: Optional[str] = None
    sponsorblock_remove: Optional[str] = None
    subtitle_langs: Optional[str] = None
    title_filter: Optional[str] = None
    description_filter: Optional[str] = None
    description_exclude: Optional[str] = None
    download_since: Optional[str] = None  # YYYY-MM-DD
    delete_after_days: Optional[int] = None


@router.get("")
async def list_channels():
    """List all channels."""
    channels = await db.get_all_channels()
    return {"channels": channels}


@router.post("")
async def add_channel(channel: ChannelCreate):
    """Add a new channel."""
    # Check if channel already exists
    existing = await db.get_channel_by_url(channel.url)
    if existing:
        raise HTTPException(status_code=400, detail="Channel already exists")

    # Fetch channel info
    try:
        info = await asyncio.get_event_loop().run_in_executor(
            None,
            lambda: get_channel_info(channel.url)
        )
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Could not fetch channel info: {e}")

    # Create channel
    channel_data = channel.model_dump(exclude_unset=True)
    channel_data.pop('url')

    channel_id = await db.create_channel(
        name=info['name'],
        url=info['url'],
        channel_id=info['channel_id'],
        **channel_data
    )

    # Log channel add
    filters = {}
    if channel.title_filter:
        filters['title_filter'] = channel.title_filter
    if channel.description_filter:
        filters['description_filter'] = channel.description_filter
    if channel.description_exclude:
        filters['description_exclude'] = channel.description_exclude
    if channel.download_since:
        filters['download_since'] = channel.download_since

    await db.create_log_entry("channel_add", info['name'], json.dumps({
        "url": info['url'],
        "format": channel.download_format,
        "filters": filters
    }))

    # Scan in background — don't block the response
    new_channel = await db.get_channel(channel_id)
    if new_channel:
        asyncio.create_task(scan_channel(new_channel))

    return {"id": channel_id, "name": info['name'], "message": f"Channel added. Scanning for videos..."}


@router.get("/{channel_id}")
async def get_channel(channel_id: int):
    """Get a specific channel."""
    channel = await db.get_channel(channel_id)
    if not channel:
        raise HTTPException(status_code=404, detail="Channel not found")
    return channel


@router.put("/{channel_id}")
async def update_channel(channel_id: int, update: ChannelUpdate):
    """Update a channel."""
    channel = await db.get_channel(channel_id)
    if not channel:
        raise HTTPException(status_code=404, detail="Channel not found")

    update_data = update.model_dump(exclude_unset=True)
    if update_data:
        await db.update_channel(channel_id, **update_data)

    return {"message": "Channel updated successfully"}


@router.delete("/{channel_id}")
async def delete_channel(channel_id: int):
    """Delete a channel and its videos."""
    channel = await db.get_channel(channel_id)
    if not channel:
        raise HTTPException(status_code=404, detail="Channel not found")

    await db.delete_channel(channel_id)
    return {"message": "Channel deleted successfully"}


@router.post("/{channel_id}/scan")
async def trigger_scan(channel_id: int):
    """Manually trigger a scan for a channel."""
    channel = await db.get_channel(channel_id)
    if not channel:
        raise HTTPException(status_code=404, detail="Channel not found")

    asyncio.create_task(scan_channel(channel))
    return {"message": "Scan started. New videos will appear in the queue."}
