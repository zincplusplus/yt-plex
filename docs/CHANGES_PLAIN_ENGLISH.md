# Changes (Plain English)

This file should be updated in every commit that changes behavior, operations, or user-visible output.

## 2026-02-18 — Backend: deletion reasons, richer queue data, NFO deep links

- Every deletion now records why it happened. Cleanup stores "Removed by global/source retention policy: older than N days" or "File missing — removed by Plex or an external process". Manual deletes store "Manually deleted". Source-removal deletes store "Source removed". The reason shows up in the queue's deleted-item tooltip.
- `parse_queue()` now returns `created_at`, `updated_at`, `last_error`, `attempt_download`, and `attempt_process` per item — previously these fields were dropped before reaching the API.
- Queue events now include the video title (joined from the queue table), so the event log can show titles instead of raw video IDs.
- Per-source retention now only overrides global retention when a source explicitly sets `retention_days`. Previously, a source with no retention setting would silently inherit global retention through the map — the logic was equivalent but fragile.
- NFO files now include a `<website>` field pointing to `{base_url}/#queue/{video_id}` when `base_url` is configured.
- `base_url` and `cleanup_timezone` added to settings (see earlier timezone entry for details).

## 2026-02-18 — Infra: rsync deploy, BuildKit cache mounts, dropped python-dotenv

- `deploy.sh` now uses `rsync` instead of `scp`, properly excluding `data/`, `downloads/`, `.venv`, `.env`, etc. Docker build no longer uses `--no-cache`.
- `Dockerfile` uses BuildKit cache mounts for apt and pip — rebuilds are much faster.
- `docker-compose.yml` pins `TZ=UTC` on both services so timestamps are consistent regardless of host timezone.
- `.env.example` simplified to just the overrides that matter; removed explicit defaults.
- `python-dotenv` removed — settings come from env vars and `data/settings.json` only.

## 2026-02-18 — chore: CLAUDE.md is now a symlink to AGENTS.md

No behavior change. Keeps a single source of truth for agent instructions.

## 2026-02-18 — Cleanup now runs at 4 AM in your timezone

Cleanup no longer runs at a random time tied to container restarts. It now runs every night at 4 AM in your timezone, which is auto-detected from your browser on first visit. The schedule handles DST transitions correctly.

## 2026-02-18

### NFO files now link back to yt-plex

- Each `.nfo` file written for Plex now includes a `<website>` field pointing to the exact queue entry: `http://your-server:8080/#queue/VIDEO_ID`.
- Opening that URL takes you straight to the Queue section in yt-plex with the row scrolled into view and briefly highlighted.
- The server URL is auto-detected on first browser visit (`window.location.origin`) and saved as the `base_url` setting. No manual configuration needed on standard setups. The detection logic lives in a single `initSettings()` function — adding more auto-filled settings in the future is a one-liner there.
- Users on keyed deployments can set or override `base_url` manually in the Settings section.
- If `base_url` is unset, the `<website>` element is omitted from the NFO (no empty or broken links).



### Status dot tooltips upgraded: larger target, copyable, no false blinking

- Hover target is now the full table cell with padding, not just the 7px dot — easier to land on.
- Tooltips are now custom (replaced native browser `title` attribute). They show a "click to copy" hint and copy the tooltip text to clipboard on click. The tooltip briefly flashes "Copied!" in green, then restores.
- Status dots in the Recent Events log no longer pulse. Events are historical — a `downloading` dot from an old transition shouldn't keep blinking.
- Deleted items now have a gray dot instead of no dot.

### Deleted tooltip now explains why the item was removed

- Hovering a deleted item's dot now says why it was deleted, not just when.
- Cleanup worker stores the reason at delete time:
  - **Retention (global):** "Removed by global retention policy: older than 7 days"
  - **Retention (source override):** "Removed by source retention policy: older than 30 days"
  - **Orphan (file gone):** "File missing — removed by Plex or an external process"
  - **Manual delete:** "Manually deleted"
  - **Source removed:** "Source removed"
- Items deleted before this change show no reason (the data wasn't captured).

### .env file eliminated entirely
- CasaOS never reads `.env` — it manages volumes and env vars through its own UI. The `.env` we were rsync'ing to the server was being silently ignored the whole time.
- All docker-compose variables already have sensible defaults, so `.env` isn't needed for plain docker-compose users either (unless they want non-default paths/ports).
- `deploy.sh` now excludes `.env` from rsync so it can never accidentally overwrite server-side config.
- `TZ=UTC` hardcoded in `docker-compose.yml` for both services. The container was already running in UTC (the `TZ` in `.env` was never applied); now this is explicit and deterministic.
- `.env.example` rewritten to a minimal optional-overrides reference for non-CasaOS users only. Gemini API key note added pointing to the Settings panel.

### App no longer auto-loads .env file
- `main.py` previously called `load_dotenv()` which silently loaded `.env` from disk on startup. This meant local dev runs inherited deployment config like `DOWNLOADS_DIR=/mnt/plex1/YouTube`, which could cause unexpected behaviour.
- Now the app reads environment variables directly from the process environment — the same way it works inside Docker. No `.env` file is needed to run the app.
- `python-dotenv` removed from `requirements.txt`.
- `deploy.sh` had its two deploy-specific variables (`DEPLOY_TARGET`, `DEPLOY_REMOTE_DIR`) hardcoded directly — it no longer sources `.env`.
- `.env.example` updated to clarify it's a docker-compose override file, not app config.

### Deploys are now faster (BuildKit cache mounts)
- `deploy.sh` previously ran `docker compose build --no-cache` on every deploy. This forced apt and pip to download everything from scratch each time.
- Now uses BuildKit cache mounts: apt packages and pip wheels are cached on the remote host's filesystem between builds, so they don't re-download unless a package actually changes.
- The first deploy after this change will be as slow as before (cold cache). Every deploy after that: only changed packages fetch from the network.
- No stale-layer risk: cache mounts are host-filesystem caches, not Docker layer cache, so they can't bake stale code into an image.



### Time display now uses familiar messaging-style tiers
- Consolidated two overlapping time functions (`timeAgo` and `fmtSince`) into one `timeAgo` function.
- New tiers match how iOS Messages / WhatsApp show timestamps:
  - Under 1 minute → "just now"
  - Under 1 hour → "5m ago"
  - Under 24 hours → "3h ago"
  - Yesterday → "yesterday"
  - This week → day name (e.g. "Monday")
  - Older, same year → "Feb 15"
  - Different year → "Feb 15, 2025"
- Applies to queue item tooltips and worker heartbeat pills in the system status panel.

### Deploy script no longer overwrites production data
- Previously, `deploy.sh` used `scp -r *` which copied the local `data/` directory (including your local queue database) onto the production server.
- Now uses `rsync` with explicit excludes for `data/`, `downloads/`, `.venv/`, `__pycache__/`, and `.git/`.
- The `.env` file is still deployed (it's included by rsync since it's not excluded).
- Production data backups still happen before the container rebuild.

### Status dots now have tooltips explaining the current situation
- Hover over the colored dot next to any queue item to see what's happening.
- **Pending**: shows when it was queued (e.g. "queued 5m ago").
- **Downloading**: shows when the download started and which attempt it's on.
- **Processing**: shows when post-processing started and which attempt.
- **Done**: shows when processing completed.
- **Failed** (download or processing): shows the error reason and how many attempts were made.
- **Deleted**: shows when it was removed.

### Recent events now show video title instead of video ID
- The recent events panel in the queue section now displays the video title, matching how the queue table shows items.
- Falls back to video ID if the title is unavailable (e.g. deleted items with no queue record).

## 2026-02-17

### Documentation workflow added
- Added a plain-English documentation standard with three core files:
  - `docs/INTENT.md`
  - `docs/HOW_IT_WORKS.md`
  - `docs/CHANGES_PLAIN_ENGLISH.md`
- Added a pre-commit guard that blocks code commits unless this changelog file is updated.
- Added `AGENTS.md` guidance so future agent edits keep these docs current.

### Maker-mode docs refined
- Added `docs/AI_BUILD_GUIDE.md` so AI has explicit implementation and quality rules.
- Added `docs/AI_MEMORY.md` to persist user preferences, dislikes, and workflow expectations.
- Expanded `docs/HOW_IT_WORKS.md` with visual flow and status lifecycle diagrams.
- Updated `AGENTS.md` to enforce a maker-first workflow and explicit doc ownership.

### HOW_IT_WORKS restructured for maker readability
- Rebuilt `docs/HOW_IT_WORKS.md` into 2 clear sections:
  - Main components + how each works (scanner, downloader, post-processor, cleanup).
  - UI breakdown with what to expect in each page section.
- Added plain-English expectations per section so behavior can be validated without reading code.

### HOW_IT_WORKS expanded with concrete runtime detail
- Added exact queue-state semantics and transition behavior.
- Documented scanner/downloader/processor/cleanup cadence, retries, and failure thresholds.
- Added concrete API and UI action mapping (what each button calls and what state changes).
- Documented health/readiness/metrics behavior and UI refresh intervals.

### Applied maker inline comments in HOW_IT_WORKS
- Clarified unclear terms (`WAL`, worker heartbeat, `latest`, `start_after`, scanner model).
- Added rationale for source-level filters/settings and current "not yet implemented" AI filter note.
- Rephrased downloader rate-limit behavior in plain English.
- Expanded smart-cut explanation (how ffprobe/ffmpeg are used and why full re-encode is avoided).
- Standardized naming to `gemini_fallback` / `gemini_api_key`.

### Removed queue title click-to-copy behavior
- Queue `done` titles are no longer clickable for clipboard path copy.
- Updated docs to remove this expectation.

### Removed unused `downloads_host_path` setting
- Removed `downloads_host_path` from settings API schema and defaults.
- Removed the field from the Settings UI.
- Removed related docs and compose env wiring.
- Result: fewer confusing settings and cleaner configuration surface.

### Removed unused AI docs
- Removed `docs/AI_BUILD_GUIDE.md` and `docs/AI_MEMORY.md`.
- Updated `AGENTS.md` and `README.md` to stop referencing them.
- Documentation workflow now centers on:
  - `docs/HOW_IT_WORKS.md`
  - `docs/CHANGES_PLAIN_ENGLISH.md`
