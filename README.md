# yt-plex

YouTube downloader for Plex designed to be smart.

## Architecture

Four independent services connected by a shared SQLite queue (`data/queue.db`). Each has a scheduler (dumb timer) and can also be triggered manually via the API.

```
Scanner → [ ] pending → Downloader → [p] processing → Post-processor → [x] done
                                                                         ↑
                                                          Cleaner: deletes old [x] → [d]
```

### Queue statuses

| Char | Status          | Meaning                                             |
| ---- | --------------- | --------------------------------------------------- |
| ` `  | pending         | Scanned, waiting to download                        |
| `↓`  | downloading     | Download in progress                                |
| `p`  | processing      | Post-processing (SponsorBlock + smart_cut)          |
| `x`  | done            | Complete                                            |
| `!`  | download_failed | Failed to download after 3 attempts                 |
| `e`  | process_failed  | Failed to post-process after 3 attempts             |
| `d`  | deleted         | Removed by retention, channel deletion, or manually |

### Queue storage

Queue state is stored in SQLite (`data/queue.db`):
- `queue_items`: current item state (`pending/downloading/processing/done/...`)
- `queue_events`: append-only transition log for observability/debugging

| Field       | Used by                             | Purpose                                                      |
| ----------- | ----------------------------------- | ------------------------------------------------------------ |
| status      | All services                        | Determines which service picks up the entry                  |
| upload_date | Cleaner                             | Retention check (`now - date > retention_days`)              |
| channel     | Cleaner, Downloader, UI             | Per-channel retention, download folder organization, display |
| title       | UI                                  | Display only                                                 |
| video_id    | Scanner, Downloader, Post-processor | Dedup, yt-dlp download, SponsorBlock API lookup              |

### Services

**Scanner** — Fetches recent videos from configured YouTube channels via yt-dlp (`extract_flat=True`, up to 200 per channel). Skips videos already in the queue. Adds new ones as `[ ]` pending, oldest first so the newest video appears as "recently added" in Plex.

- Scheduler: runs every `scan_interval` minutes (default 30)
- Manual: `POST /api/scan-now`
- When a source is added: queues the most recent N videos (default 1, configurable via `latest` field in the add form) and auto-sets `start_after` to the oldest queued video's Unix timestamp, so future scans only pick up newer videos
- Filtering pipeline (in order): already in queue → start_date → shorts → livestreams → title filters → (full metadata fetch if needed) → description filters
- Auto-filters: shorts (< 60s or `#shorts` in title) and livestreams are always skipped
- Per-source filters: `title_include`, `title_exclude` (regex), `description_include`, `description_exclude` (regex)
- Filtering is cheap: 1 call per channel for the flat extract. Full metadata fetch (1 extra call per video) only for videos that pass title filters and need description filtering
- Note: since `start_after` is auto-set on every source, every new video needs a full metadata fetch to check its timestamp. In practice this is 0-1 new videos per scan

**Downloader** — Downloads the first `[ ]` pending video. Marks `[↓]` while downloading, then `[p]` when done. In a single yt-dlp call: downloads video (H.264) + audio (AAC), merges to MP4, downloads + embeds English subtitles, downloads thumbnail. yt-dlp also writes `.info.json` with full metadata for the post-processor. Files go to `downloads/{channel}/{video_id}/`.

- Scheduler: runs continuously, picks up pending videos immediately
- Manual: `POST /api/queue/{video_id}/download-now`
- After 3 consecutive failures: marks `[!]`
- Partial downloads: yt-dlp writes `.part` files and resumes automatically
- On startup: any `[↓]` entries are reset to `[ ]` (interrupted downloads get retried)

**Post-processor** — Picks up `[p]` videos. Reads `.info.json` from the video folder. Fetches SponsorBlock segments (with Gemini fallback), runs smart_cut via ffmpeg to remove sponsor segments, renames thumbnail to `poster.jpg`, writes Plex-compatible `.nfo` metadata. Marks `[x]` done.

- Scheduler: runs continuously, picks up processing videos immediately
- After 3 consecutive failures: marks `[e]`
- Runs concurrently with downloads (video N+1 downloads while video N is processed)

**Cleaner** — Deletes video files older than `retention_days` (default 14, per-channel or global). Marks `[d]`.

- Scheduler: runs once per day
- Manual: `DELETE /api/queue/{video_id}` (immediate)

### Failure handling

Download and post-process failures are tracked persistently in SQLite counters (`attempt_download`, `attempt_process`). After 3 consecutive failures, the video is marked `[!]` or `[e]` in the queue and stays there until manually retried. Retry via the API sends `[!]` back to `[ ]` and `[e]` back to `[p]`.

### Concurrency

All queue writes are transactional SQLite updates (WAL mode), so readers can continue while writes happen and status changes are atomic.

### Observability

- `GET /healthz` — liveness probe
- `GET /readyz` — readiness probe (includes worker heartbeat health)
- `GET /api/system/status` — runtime snapshot (queue counts, worker activity, next scan/cleanup times)
- `GET /metrics` — Prometheus-style metrics for queue and worker activity

### Access Control

Auth is optional by default (for local setup). If either key is set, mutating endpoints require tokens:

- `YT_PLEX_OPERATOR_KEY`: scan/retry/prioritize/source edits
- `YT_PLEX_ADMIN_KEY`: operator actions + delete/settings updates

Send either:
- `X-API-Key: <token>`
- `Authorization: Bearer <token>`

Optional audit actor:
- `X-Actor: <name>`

## Local development

```bash
cp -n .env.example .env
mkdir -p data downloads
./.venv/bin/pip install -r requirements.txt
./.venv/bin/watchfiles --filter python --ignore-paths .git,.venv,data,downloads,__pycache__ "./.venv/bin/python main.py" .
```

Open http://localhost:8080

This runs the full app locally with auto-restart on file changes:
- web API/UI (Uvicorn)
- scanner loop
- downloader loop
- post-processor loop
- cleanup loop

To run API and workers as separate processes:

```bash
# Terminal 1 (API only)
RUN_API=true RUN_WORKERS=false ./.venv/bin/python main.py

# Terminal 2 (workers only)
RUN_API=false RUN_WORKERS=true ./.venv/bin/python main.py
```

## Testing

```bash
./.venv/bin/python -m unittest discover -s tests -v
```

## Operations Docs

- `docs/RUNBOOK.md`
- `docs/RELEASE.md`
- `docs/HOW_IT_WORKS.md`
- `docs/CHANGES_PLAIN_ENGLISH.md`

## Documentation Workflow

This repo keeps maker-facing docs current so you can run the product without reading source code.

Before committing code changes:

1. Update `docs/CHANGES_PLAIN_ENGLISH.md` with what changed and why.
2. If runtime behavior or architecture changed, update `docs/HOW_IT_WORKS.md`.

Enable the commit guard once per clone:

```bash
./scripts/install-hooks.sh
```

The pre-commit hook blocks code commits when `docs/CHANGES_PLAIN_ENGLISH.md` is not staged.

If you only want API/UI hot reload (without background worker loops), run:

```bash
./.venv/bin/uvicorn server:app --reload --host 0.0.0.0 --port 8080
```
