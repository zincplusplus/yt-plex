from fastapi import APIRouter

from ..core import database as db

router = APIRouter(prefix="/api/logs", tags=["logs"])


@router.get("")
async def list_logs(limit: int = 200):
    """Get activity log entries, newest first."""
    entries = await db.get_log_entries(limit=limit)
    return {"entries": entries}


@router.delete("")
async def clear_logs():
    """Clear all activity log entries."""
    await db.clear_log()
    return {"message": "Activity log cleared"}
