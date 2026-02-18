# yt-plex API Reference

Base URL: `http://localhost:8080`

## Authentication

Mutating endpoints require API keys when either key is configured:
- `YT_PLEX_OPERATOR_KEY` — can run scans, retries, prioritization, and source edits
- `YT_PLEX_ADMIN_KEY` — operator permissions + delete + settings updates

Headers (either form):
- `X-API-Key: <token>`
- `Authorization: Bearer <token>`

Optional audit actor header:
- `X-Actor: <name>` (stored in queue events/audit logs)

## Error Contract

Validation and request errors return:

```json
{
  "error": {
    "code": "validation_error",
    "message": "Invalid request payload",
    "fields": [
      { "field": "body.latest", "message": "Input should be less than or equal to 200" }
    ]
  }
}
```

Notes:
- Invalid payloads return `400` with the schema above.
- Most `404` and `409` errors also return the same `error` envelope (`code` + `message`).

## Pages

### `GET /`
Serves the web UI (`templates/index.html`).

## Observability

### `GET /healthz`
Basic liveness probe.

**Response:** `200 OK`
```json
{ "ok": true }
```

### `GET /readyz`
Readiness probe. Validates queue/source reads and worker heartbeats.

**Response:** `200 OK` when ready, `503` when not ready.
```json
{
  "ok": true,
  "workers": {
    "scanner": {
      "last_heartbeat": "2026-02-16T21:20:00Z",
      "heartbeat_age_seconds": 3,
      "alive": true
    }
  }
}
```

### `GET /api/system/status`
Runtime snapshot for reactive UI dashboards.

**Response:** `200 OK`
```json
{
  "now": "2026-02-16T21:20:03Z",
  "started_at": "2026-02-16T20:00:00Z",
  "queue": {
    "pending": 2,
    "downloading": 1,
    "processing": 0,
    "done": 14,
    "download_failed": 1,
    "process_failed": 0,
    "deleted": 3,
    "total": 21,
    "active": 1,
    "failed": 1
  },
  "scanner": {
    "last_scan_at": "2026-02-16T21:00:00Z",
    "next_scan_at": "2026-02-16T21:30:00Z",
    "next_scan_in_seconds": 598,
    "last_scan_added": 1,
    "last_scan_error": null
  },
  "cleanup": {
    "last_cleanup_at": "2026-02-16T00:10:00Z",
    "next_cleanup_at": "2026-02-17T00:10:00Z",
    "next_cleanup_in_seconds": 10234,
    "last_cleanup_deleted": 4,
    "last_cleanup_error": null
  },
  "workers": {
    "downloader": {
      "last_heartbeat": "2026-02-16T21:20:01Z",
      "active_video_id": "dQw4w9WgXcQ",
      "heartbeat_age_seconds": 2
    }
  }
}
```

### `GET /metrics`
Prometheus-style text metrics.

**Response:** `200 OK` (`text/plain`)
```text
ytplex_queue_items{status="pending"} 2
ytplex_queue_items{status="failed"} 1
ytplex_next_scan_in_seconds 598
ytplex_worker_heartbeat_age_seconds{worker="scanner"} 3
ytplex_worker_active{worker="downloader"} 1
ytplex_uptime_seconds 4800
```

## Sources

### `GET /api/sources`
Returns all configured sources.

**Response:** `200 OK`
```json
[
  {
    "id": "9bb8b3cb-03e4-4c47-8eb9-0d38f3af2d7f",
    "url": "https://www.youtube.com/@Channel",
    "name": "Channel Name",
    "sponsorblock": true,
    "gemini_fallback": true,
    "start_after": 1700000000,
    "title_include": "regex",
    "title_exclude": "regex",
    "description_include": "regex",
    "description_exclude": "regex",
    "min_minutes": 5,
    "max_minutes": 60,
    "retention_days": 30
  }
]
```
All fields except `url` and `name` are optional. `start_after` is a Unix timestamp auto-set when the source is added.
`id` is a stable source identifier used for edit/delete APIs.

### `POST /api/sources`
Add a new channel. Resolves the channel name via yt-dlp, fetches the most recent N videos, queues them, and auto-sets `start_after` to the oldest queued video's timestamp.

**Request body:**
```json
{
  "url": "https://www.youtube.com/@Channel",
  "latest": 2,
  "sponsorblock": true,
  "gemini_fallback": true,
  "title_include": "regex",
  "title_exclude": "regex",
  "description_include": "regex",
  "description_exclude": "regex",
  "min_minutes": 5,
  "max_minutes": 60,
  "retention_days": 30
}
```
Only `url` is required. `latest` defaults to 1 (number of recent videos to queue on add).

Validation rules:
- `url`: required, `http://` or `https://`, max 500 chars
- `latest`: `1..200`
- `sponsorblock`: optional bool; when omitted, global setting is used
- `gemini_fallback`: optional bool; when omitted, global Gemini key availability is used
- `min_minutes`/`max_minutes`: `1..1440` and `min_minutes <= max_minutes`
- `retention_days`: `1..3650`
- regex fields (`title_*`, `description_*`):
  - max 200 chars
  - must compile
  - disallow backreferences, lookarounds, and nested quantified groups

**Response:** `200 OK`
```json
{
  "id": "9bb8b3cb-03e4-4c47-8eb9-0d38f3af2d7f",
  "url": "https://www.youtube.com/@Channel",
  "name": "Channel Name",
  "start_after": 1700000000,
  "queued": 2
}
```

**Errors:**
- `400` — invalid URL or could not resolve
- `409` — source already exists

### `PUT /api/sources/{source_id}`
Edit a source's filters and retention. Only provided fields are updated; `null` or empty string clears the field. `url`, `name`, and `start_after` are not editable.

`source_id` should be the source `id` from `GET /api/sources`.
Compatibility: numeric `source_id` values are treated as legacy list indexes.

**Request body** (all fields optional — omit a field to leave it unchanged):
```json
{
  "sponsorblock": false,
  "gemini_fallback": false,
  "title_include": "regex",
  "title_exclude": null,
  "description_include": "regex",
  "description_exclude": null,
  "min_minutes": 5,
  "max_minutes": null,
  "retention_days": 30
}
```

**Response:** `200 OK` — returns the updated source object.

**Errors:**
- `404` — source not found

Validation rules:
- `sponsorblock`: optional bool (per-source override)
- `gemini_fallback`: optional bool (per-source override)
- `min_minutes`/`max_minutes`: `1..1440` and `min_minutes <= max_minutes`
- `retention_days`: `1..3650`
- regex fields: same safety rules as `POST /api/sources`

### `DELETE /api/sources/{source_id}?delete_files=true`
Remove a source by stable ID. Optionally delete downloaded files and mark queue entries as deleted.

**Query params:**
- `delete_files` (bool, optional) — if `true`, removes the channel's download directory and marks its queue entries as deleted

**Response:** `200 OK`
```json
{ "removed": { "url": "...", "name": "..." }, "files_deleted": 0 }
```

**Errors:**
- `404` — source not found

## Queue

### `GET /api/queue`
Returns the parsed queue (from `data/queue.db`).

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

### `GET /api/queue/events?limit=100`
Returns recent queue transition events (newest first).

Validation rules:
- `limit`: `1..1000`

**Response:** `200 OK`
```json
[
  {
    "id": 123,
    "video_id": "dQw4w9WgXcQ",
    "at": "2026-02-16T21:00:00Z",
    "actor": "downloader",
    "from_status": "pending",
    "to_status": "downloading",
    "message": "Claimed for download"
  }
]
```

### `GET /api/queue/dead-letter?limit=200&status=download_failed&channel=Name&q=term`
Returns failed items for operator triage (`download_failed` and `process_failed`).

Validation rules:
- `limit`: `1..1000`
- `status`: optional, one of `download_failed|process_failed`

**Response:** `200 OK`
```json
[
  {
    "video_id": "dQw4w9WgXcQ",
    "status": "download_failed",
    "date": "2026-02-15",
    "channel": "Channel Name",
    "title": "Video Title",
    "updated_at": "2026-02-16T21:11:00Z",
    "attempt_download": 3,
    "attempt_process": 0,
    "last_error": "HTTP 429"
  }
]
```

### `POST /api/scan-now`
Trigger an immediate scan of all sources.

Auth:
- operator or admin token required (when keys are configured)

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

Auth:
- operator or admin token required (when keys are configured)

### `POST /api/queue/{video_id}/retry`
Retry a failed video. `download_failed` (`[!]`) goes back to `pending` (`[ ]`), `process_failed` (`[e]`) goes back to `processing` (`[p]`).

**Response:** `200 OK`
```json
{ "retried": "video_id", "new_status": "pending" }
```

**Errors:**
- `404` — no failed entry with that video ID

Auth:
- operator or admin token required (when keys are configured)

### `POST /api/queue/bulk/retry`
Retry many failed videos at once.

**Request body:**
```json
{ "video_ids": ["id1", "id2"] }
```

**Response:** `200 OK`
```json
{
  "requested": 2,
  "succeeded": 2,
  "results": [
    { "video_id": "id1", "ok": true, "new_status": "pending" },
    { "video_id": "id2", "ok": true, "new_status": "processing" }
  ]
}
```

Auth:
- operator or admin token required (when keys are configured)

### `DELETE /api/queue/{video_id}`
Delete a video's downloaded files and mark as deleted in the queue.

**Response:** `200 OK`
```json
{ "deleted": "video_id" }
```

**Errors:**
- `404` — video not found in queue

Auth:
- admin token required (when keys are configured)

### `POST /api/queue/bulk/delete`
Delete many queue items and their files at once.

**Request body:**
```json
{ "video_ids": ["id1", "id2"] }
```

**Response:** `200 OK`
```json
{
  "requested": 2,
  "succeeded": 2,
  "files_deleted": 1,
  "results": [
    { "video_id": "id1", "ok": true },
    { "video_id": "id2", "ok": true }
  ]
}
```

Auth:
- admin token required (when keys are configured)

## Settings

### `GET /api/settings`
Returns current effective values for all settings (settings.json > env vars > defaults).

**Response:** `200 OK`
```json
{
  "scan_interval": 30,
  "sleep_between_downloads": 5,
  "sponsorblock": true,
  "preferred_resolution": "1080p",
  "output_template": "%(channel)s/%(id)s/%(title)s.%(ext)s",
  "retention_days": 7,
  "gemini_api_key": ""
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
- `400` — invalid payload or no valid settings provided

Auth:
- admin token required (when keys are configured)

Validation rules:
- `scan_interval`: `1..1440`
- `sleep_between_downloads`: `0..3600`
- `preferred_resolution`: one of `360p|480p|720p|1080p`
- `output_template`: non-empty, max 500 chars
- `retention_days`: `1..3650`
- `gemini_api_key`: max 512 chars
