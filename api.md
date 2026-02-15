# yt-plex API Reference

Base URL: `http://localhost:8080`

## Pages

### `GET /`
Serves the web UI (`templates/index.html`).

## Sources

### `GET /api/sources`
Returns all configured sources.

**Response:** `200 OK`
```json
[
  {
    "url": "https://www.youtube.com/@Channel",
    "name": "Channel Name",
    "start_after": 1700000000,
    "title_include": "regex",
    "title_exclude": "regex",
    "description_include": "regex",
    "description_exclude": "regex",
    "retention_days": 30
  }
]
```
All fields except `url` and `name` are optional. `start_after` is a Unix timestamp auto-set when the source is added.

### `POST /api/sources`
Add a new channel. Resolves the channel name via yt-dlp, fetches the most recent N videos, queues them, and auto-sets `start_after` to the oldest queued video's timestamp.

**Request body:**
```json
{
  "url": "https://www.youtube.com/@Channel",
  "latest": 2,
  "title_include": "regex",
  "title_exclude": "regex",
  "description_include": "regex",
  "description_exclude": "regex",
  "retention_days": 30
}
```
Only `url` is required. `latest` defaults to 1 (number of recent videos to queue on add).

**Response:** `200 OK`
```json
{
  "url": "https://www.youtube.com/@Channel",
  "name": "Channel Name",
  "start_after": 1700000000,
  "queued": 2
}
```

**Errors:**
- `400` — invalid URL or could not resolve
- `409` — source already exists

### `DELETE /api/sources/{index}?delete_files=true`
Remove a source by its array index. Optionally delete downloaded files and mark queue entries as deleted.

**Query params:**
- `delete_files` (bool, optional) — if `true`, removes the channel's download directory and marks its queue entries as deleted

**Response:** `200 OK`
```json
{ "removed": { "url": "...", "name": "..." }, "files_deleted": 0 }
```

**Errors:**
- `404` — index out of range

## Queue

### `GET /api/queue`
Returns the parsed queue (from `data/queue.md`).

**Response:** `200 OK`
```json
[
  {
    "line_num": 0,
    "status": "pending",
    "date": "2025-06-15",
    "channel": "Channel Name",
    "title": "Video Title",
    "video_id": "dQw4w9WgXcQ",
    "raw": "- [ ] 2025-06-15 | Channel Name | Video Title | dQw4w9WgXcQ"
  }
]
```
Status is one of: `pending`, `downloading`, `processing`, `done`, `download_failed`, `process_failed`, `deleted`.

### `POST /api/scan-now`
Trigger an immediate scan of all sources.

**Response:** `200 OK`
```json
{ "added": 3 }
```

### `POST /api/queue/{video_id}/download-now`
Move a pending video to the top of the download queue for immediate download.

**Response:** `200 OK`
```json
{ "queued": "video_id" }
```

**Errors:**
- `404` — no pending entry with that video ID

### `POST /api/queue/{video_id}/retry`
Retry a failed video. `download_failed` (`[!]`) goes back to `pending` (`[ ]`), `process_failed` (`[e]`) goes back to `processing` (`[p]`).

**Response:** `200 OK`
```json
{ "retried": "video_id", "new_status": "pending" }
```

**Errors:**
- `404` — no failed entry with that video ID

### `DELETE /api/queue/{video_id}`
Delete a video's downloaded files and mark as deleted in the queue.

**Response:** `200 OK`
```json
{ "deleted": "video_id" }
```

**Errors:**
- `404` — video not found in queue

## Settings

### `GET /api/settings`
Returns current effective values for all settings (settings.json > env vars > defaults).

**Response:** `200 OK`
```json
{
  "scan_interval": 360,
  "sleep_between_downloads": 5,
  "sponsorblock": true,
  "preferred_resolution": "1080p",
  "output_template": "%(channel)s/%(id)s/%(title)s.%(ext)s",
  "retention_days": 14,
  "gemini_api_key": "",
  "downloads_host_path": ""
}
```

### `PUT /api/settings`
Update one or more settings. Partial updates accepted — only provided keys are changed.

**Request body** (all fields optional):
```json
{
  "scan_interval": 120,
  "sponsorblock": false,
  "preferred_resolution": "720p"
}
```

**Response:** `200 OK` — returns the full settings object (same shape as GET).

**Errors:**
- `400` — invalid value for a setting key, or no valid settings provided
