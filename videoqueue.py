"""Queue parsing and status management for data/queue.md.

All status writes go through the asyncio lock to prevent concurrent corruption.
"""

import asyncio
import re
from pathlib import Path

DATA_DIR = Path("data")
QUEUE_FILE = DATA_DIR / "queue.md"

# Matches all queue statuses: pending( ), downloading(↓), processing(p),
# done(x), download_failed(!), process_failed(e), deleted(d)
QUEUE_RE = re.compile(r"^- \[( |↓|p|x|!|e|d)\] (.+)$")

STATUS_MAP = {
    " ": "pending",
    "↓": "downloading",
    "p": "processing",
    "x": "done",
    "!": "download_failed",
    "e": "process_failed",
    "d": "deleted",
}

CHAR_MAP = {v: k for k, v in STATUS_MAP.items()}

queue_lock = asyncio.Lock()


def parse_queue() -> list[dict]:
    """Parse queue.md into structured entries."""
    if not QUEUE_FILE.exists():
        return []
    entries = []
    for i, line in enumerate(QUEUE_FILE.read_text().splitlines()):
        m = QUEUE_RE.match(line)
        if not m:
            continue
        status_char = m.group(1)
        parts = [p.strip() for p in m.group(2).split("|")]
        if len(parts) < 4:
            continue
        entries.append({
            "line_num": i,
            "status": STATUS_MAP.get(status_char, "pending"),
            "date": parts[0],
            "channel": parts[1],
            "title": parts[2],
            "video_id": parts[3],
            "raw": line,
        })
    return entries


def get_pending() -> list[dict]:
    return [e for e in parse_queue() if e["status"] == "pending"]


def get_by_status(status: str) -> list[dict]:
    return [e for e in parse_queue() if e["status"] == status]


def read_queue_ids() -> set[str]:
    """Return set of video IDs already in queue.md."""
    if not QUEUE_FILE.exists():
        return set()
    ids = set()
    for line in QUEUE_FILE.read_text().splitlines():
        parts = line.split("|")
        if len(parts) >= 4:
            ids.add(parts[-1].strip())
    return ids


def _update_status_sync(video_id: str, new_char: str):
    if not QUEUE_FILE.exists():
        return
    lines = QUEUE_FILE.read_text().splitlines()
    for i, line in enumerate(lines):
        if video_id in line and line.startswith("- ["):
            lines[i] = f"- [{new_char}]{line[5:]}"
            break
    QUEUE_FILE.write_text("\n".join(lines) + "\n")


async def mark_done(video_id: str):
    async with queue_lock:
        _update_status_sync(video_id, "x")


async def mark_deleted(video_id: str):
    async with queue_lock:
        _update_status_sync(video_id, "d")


async def mark_pending(video_id: str):
    async with queue_lock:
        _update_status_sync(video_id, " ")


async def mark_failed(video_id: str):
    async with queue_lock:
        _update_status_sync(video_id, "!")


async def mark_downloading(video_id: str):
    async with queue_lock:
        _update_status_sync(video_id, "↓")


async def mark_processing(video_id: str):
    async with queue_lock:
        _update_status_sync(video_id, "p")


async def mark_process_failed(video_id: str):
    async with queue_lock:
        _update_status_sync(video_id, "e")


async def mark_cancelled(video_id: str):
    async with queue_lock:
        _update_status_sync(video_id, "d")


async def reset_interrupted():
    """On startup, reset any [↓] downloading entries back to [ ] pending."""
    async with queue_lock:
        if not QUEUE_FILE.exists():
            return
        lines = QUEUE_FILE.read_text().splitlines()
        changed = False
        for i, line in enumerate(lines):
            if line.startswith("- [↓]"):
                lines[i] = "- [ ]" + line[5:]
                changed = True
        if changed:
            QUEUE_FILE.write_text("\n".join(lines) + "\n")
