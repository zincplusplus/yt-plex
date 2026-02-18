# How It Works

This is a concrete, operator-level explanation of runtime behavior and UI behavior.

## 1) Main Components And How Each Works

### System pipeline (visual)

```text
YouTube sources (saved in queue.db:sources)
            |
            v
Scanner (interval + manual trigger)
            |
            v
queue_items.status = pending
            |
            v
Downloader (continuous worker, one item at a time)
            |
            v
queue_items.status = processing
            |
            v
Post-Processor (continuous worker, one item at a time)
            |
            v
queue_items.status = done
            |
            v
Cleanup (daily pass)
            |
            v
queue_items.status = deleted
```

### Shared data model

- Primary store: `data/queue.db` (SQLite, WAL mode: reads and writes can happen concurrently, so the app stays responsive while workers write updates).
- Queue table: `queue_items`.
- Event log table: `queue_events` (append-only transitions).
- Runtime table: `runtime_state` (worker heartbeats, scanner/cleanup timing). A "heartbeat" is a timestamp each worker updates regularly to prove it is still alive.
- Settings file: `data/settings.json` (overrides env defaults).

### Queue statuses and exact meanings

- `pending`: discovered and waiting for downloader claim.
- `downloading`: currently claimed by downloader.
- `processing`: download succeeded; waiting for/under post-processing.
- `done`: fully processed and kept until cleanup/manual delete.
- `download_failed`: download failed 3 consecutive attempts.
- `process_failed`: post-processing failed 3 consecutive attempts.
- `deleted`: logically removed from active pipeline.
- Skipped videos are currently only visible in scanner logs (not persisted in `queue_events`).

### Scanner

Purpose:

- Find new videos from each configured source and queue them.
- A "source" is one YouTube channel URL (or equivalent channel feed URL) plus optional per-source filters and overrides.

Cadence and triggers:

- Runs every `scan_interval` minutes (default `30`). This interval is global (project-level), not per source.
- Uses one scanner loop that iterates through all configured sources in sequence.
- Wakes early if source definitions change.
- Manual trigger via `POST /api/scan-now`.

Source behavior:

- Sources are stored in SQLite (`sources` table).
- Each source can define:
  - `title_include`, `title_exclude` regex filters.
  - `description_include`, `description_exclude` regex filters.
  - `min_minutes`, `max_minutes`.
  - `retention_days` override.
  - `sponsorblock` and `gemini_fallback` overrides.
  - `start_after` timestamp cursor: only videos newer than this are eligible.

Why these source settings exist (UI intent):
- Regex filters: include/exclude topics based on title/description.
- Min/max minutes: keep only the runtime range you want.
- Per-source retention: keep some channels longer/shorter than global defaults.
- SponsorBlock/Gemini toggles: allow stricter or lighter post-processing by source.
- `start_after`: prevent backfilling older content when you only want new uploads.

Filtering pipeline (in order):

1. Deduplicate by `video_id` already present in queue.
2. Apply title regex filters.
3. Fetch full video metadata only if required (`start_after` or description filters).
4. Enforce `start_after` cutoff.
5. Skip shorts/livestreams and enforce min/max duration.
6. Apply description regex filters.
7. AI-based content filters are not implemented yet (planned item).

Queue writes:

- New matches are inserted as `pending`.
- Each insertion records a `queue_events` row.

What to expect:

- New items appear as `pending`.
- No duplicate queue entries for same `video_id`.
- Per-source filter changes only affect future scans.

### Downloader

Purpose:

- Consume `pending` items and download media/metadata to disk.

Claim model:

- Atomically claims one highest-priority pending item.
- Claim transition: `pending -> downloading`.
- Priority is normally `0`; `download-now` sets one item to top priority (`1`).

Output behavior:

- Uses yt-dlp with format constraints (H.264 video + AAC audio, merged to MP4).
- Writes subtitles, thumbnail, and `.info.json`.
- Default output template: `%(channel)s/%(video_id)s/%(title)s.%(ext)s`.
- Files land under `DOWNLOADS_DIR` (default `./downloads`).

Retry and failure behavior:

- Success transition: `downloading -> processing`.
- Failure transitions:
  - Attempts 1-2: `downloading -> pending`.
  - Attempt 3: `downloading -> download_failed`.
- If YouTube rate-limits the request, the worker waits before continuing so it does not hammer requests.
- On startup, interrupted `downloading` items are reset to `pending`.

What to expect:

- One item downloads at a time.
- `download_failed` means three consecutive failed download attempts.
- `download-now` only works on `pending` items.

### Post-Processor

Purpose:

- Convert downloaded content into Plex-ready final output.

Input expectations:

- Finds folder by `downloads/<channel>/<video_id>/`.
- Reads `.info.json` when available.
- Finds `.mp4` file; fails if none exists.

Processing steps:

1. Sponsor detection:
   - Try SponsorBlock (`sponsor`, `selfpromo` categories).
   - If no segments and Gemini key enabled, try AI subtitle-based fallback.
2. Smart cut:
   - Computes keep intervals (the parts of the video to keep after removing sponsor segments).
   - Uses `ffprobe` to inspect duration/keyframes, then `ffmpeg` to cut segments and concatenate them.
   - The approach avoids full-file re-encode; only tiny boundary slivers may be re-encoded when needed for cleaner keyframe joins.
   - Replaces original file when output is valid.
3. Metadata artifacts:
   - Renames first thumbnail to `poster.jpg`.
   - Writes `.nfo` with title, description, air date, studio, unique YouTube ID, and (if `base_url` is configured) a `<website>` link back to the exact queue entry in yt-plex (`{base_url}/#queue/{video_id}`).

Retry and failure behavior:

- Success transition: `processing -> done`.
- Failure transitions:
  - Attempts 1-2: remain/requeue as `processing`.
  - Attempt 3: `processing -> process_failed`.

What to expect:

- `done` means post-processing and metadata steps completed.
- Sponsor removal depends on SponsorBlock data or AI fallback availability.

### Cleanup

Purpose:

- Keep storage bounded and queue state aligned with filesystem reality.

Cadence:

- Runs immediately when the worker starts, then every night at 4 AM in the configured cleanup timezone.
- The timezone is detected automatically from your browser on first visit and saved to settings. You can override it by updating the `cleanup_timezone` setting directly. Default: Europe/Amsterdam.
- The schedule is DST-aware — spring-forward and fall-back days are handled correctly.
- A container restart runs cleanup immediately, then re-anchors to the next 4 AM in your timezone.

Two-pass behavior over `done` items:

1. Orphan cleanup:
   - If folder missing or no video file in folder, mark `deleted`.
2. Retention cleanup:
   - Compare queue item date to retention cutoff.
   - Delete folder and mark `deleted` if older than retention.

Retention source:

- Global: `retention_days` setting.
- Optional source-level override: `source.retention_days`.

What to expect:

- Old `done` items eventually become `deleted`.
- Missing files are reconciled automatically on cleanup pass.

### Observability and health

- `GET /healthz`: process is up. (`healthz`/`readyz` is a common ops naming convention from "health" + "z endpoint".)
- `GET /readyz`: checks queue/sources access and worker heartbeat freshness.
  <!-- vlad: look into this -->
  - Worker heartbeat older than 180s is treated as not alive.
- `GET /api/system/status`: consolidated runtime snapshot used by UI.
- `GET /metrics`: Prometheus-style queue/worker/uptime metrics.

### Auth model

<!-- vlad: look into this -->

- If no keys are configured, mutating APIs allow local anonymous mode.
- If keys exist:
  - Operator key can run operational mutations.
  - Admin key is required for destructive/admin updates.
- Token accepted via `X-API-Key` or `Authorization: Bearer`.

## 2) UI And What To Expect On Each Section

UI is a single-page app at `/` (`templates/index.html`) with collapsible sections.

### Add Source section

Purpose:

- Create a source and optionally define per-source behavior.

Fields:

- Required: `url`.
- Optional: `latest` (1-200): how many most-recent videos to queue immediately when adding the source.
- Optional: title/description regex, min/max minutes, per-source retention, source sponsor/AI toggles.

What happens on submit:

- Calls `POST /api/sources`.
- Server validates URL and regex safety.
- Server resolves source metadata and queues `latest` recent videos.
- Source appears in Sources list.

What to expect:

- Success toast reports source name and number of videos queued.
- Duplicate source returns a conflict and does not create a second entry.

### Sources section

Purpose:

- Manage current sources and trigger scans.

Available actions:

- `scan now` button -> `POST /api/scan-now`.
- `edit` -> `PUT /api/sources/{source_id}`.
- `remove` -> `DELETE /api/sources/{source_id}` with optional `delete_files=true`.

Behavior details:

- Source cards show tags for active filters and overrides.
- Removing source can also delete channel files from disk when chosen.
- Source edits affect future scan behavior, not historical queue history.

What to expect:

- `scan now` returns number of newly added queue items.
- Remove updates source list immediately; queue changes depend on delete mode.

### Settings section

Purpose:

- Control global defaults for runtime behavior.

Backed by:

- `GET /api/settings`
- `PUT /api/settings`
- Persistent storage in `data/settings.json`.

Main settings:

- `scan_interval` (1-1440 min)
- `sleep_between_downloads` (0-3600 sec)
- `preferred_resolution` (`360p|480p|720p|1080p`) to stay in H.264-compatible outputs and avoid 4K codec complexity.
- `retention_days` (1-3650)
- `sponsorblock` toggle
- `gemini_api_key`
- `output_template`

What to expect:

- Save shows temporary "saved" indicator.
- Changes apply to subsequent worker cycles.
- Invalid values return validation errors.

### Queue section

Purpose:

- Main operational view of pipeline progress.

Data sources:

- `GET /api/queue` for rows.
- `GET /api/system/status` for summary counts.

Displayed summary:

- `pending`, `active` (`downloading+processing`), `done`, `failed`, `total`.

Status dot tooltips (hover over the colored dot):

- Pending: when the video was queued.
- Downloading/Processing: when it started and which attempt.
- Done: when it completed.
- Failed: the error message and attempt count.
- Deleted: when it was removed.

Row actions by status:

- `pending`: `now`, `del`.
- `download_failed` / `process_failed`: `retry`, `del`.
- `done`: `del`.
- Deleted rows are visually dimmed.

Action endpoints:

- `now` -> `POST /api/queue/{video_id}/download-now`.
- `retry` -> `POST /api/queue/{video_id}/retry`.
- `del` -> `DELETE /api/queue/{video_id}`.

What to expect:

- Actions update queue and recent events immediately after success.

### System Status panel (inside Queue section)

Purpose:

- Real-time operational heartbeat and schedule visibility.

Backed by:

- `GET /api/system/status` every 10s.
- Scan countdown ticks every 1s on client side.

Shown values:

- Next scan countdown.
- Last scan timestamp and last scan added count.
- Worker pills (`scanner`, `downloader`, `processor`, `cleanup`) with heartbeat age and active item.
- Queue active and failed totals.

Worker health interpretation in UI:

- Non-scanner workers:
  - `live` <= 20s heartbeat age.
  - `stale` <= 180s.
  - `dead` > 180s or missing.
- Scanner:
  - Considered healthy when scheduled or currently scanning.
  - More tolerant stale/dead thresholds due to timer-driven behavior.

What to expect:

- Countdown and worker pills should change without full page refresh.
- If panel fails to load, queue can still render from its own API call.

### Recent Events panel

Purpose:

- Show latest status transitions for debugging and trust.

Backed by:

- `GET /api/queue/events?limit=25` every 15s.

What appears:

- Video title, transition (`from -> to`), timestamp, actor, optional message.

What to expect:

- Manual actions (`retry`, `now`, `delete`) appear quickly as new events.
- Repeated failures will show repeated transitions/messages.

### UI refresh behavior

- Initial page load calls:
  - `loadSources()`
  - `loadSettings()`
  - `loadSystemStatus()`
  - `loadQueue()`
  - `loadQueueEvents()`
- Background refresh:
  - System status: every 10s.
  - Queue and events: every 15s.
  - Scan countdown: every 1s.

## 3) Deployment (`deploy.sh`)

Single script, run locally. Reads `DEPLOY_TARGET` (SSH host alias) and `DEPLOY_REMOTE_DIR` (absolute path on the server) from a local `.env` file.

Steps in order:

1. **Clear the remote app directory** — deletes everything under `DEPLOY_REMOTE_DIR` except `data/` and `downloads/`, so stale code files don't accumulate. Database, settings, and downloaded files are untouched.
2. **rsync the project** — copies the local working directory to the remote, skipping `data/`, `downloads/`, `.venv/`, `__pycache__/`, `.git/`, and `.DS_Store`.
3. **Back up remote data files** — before rebuilding, creates timestamped copies of `queue.db`, `settings.json`, and `sources.json` into `data/backups/` on the remote. These are a safety net only; they are not automatically rotated.
4. **Rebuild and restart the container** — runs `docker compose down`, `docker compose build` (with BuildKit), then `docker compose up -d`.

What to expect:
- The `data/` and `downloads/` directories on the server survive every deploy unchanged.
- Each deploy creates one new backup snapshot of the three data files.
- If `docker compose build` or `up` fails, the container stays down until the problem is fixed and deploy is re-run.
- No rollback logic — restoring from a backup snapshot is manual.
