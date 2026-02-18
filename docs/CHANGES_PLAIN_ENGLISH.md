# Changes (Plain English)

This file should be updated in every commit that changes behavior, operations, or user-visible output.

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
