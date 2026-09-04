# Roadmap — Moataz Media Bot

## Completed

### Phases 1 + 2 + 3 — v0.2.0

- Runtime stability and Railway-safe defaults.
- Unified Job lifecycle and append-only `job_events`.
- Selective retry, cancellation and manual retry.
- Arabic/English Telegram main menu and recent jobs.
- Inline queue by default, Redis optional.

### Phase 4 — Media Source Engine — v0.3.0

Acceptance:

- Stable source detection for YouTube/Facebook.
- Stable platform names independent from yt-dlp extractor variants.
- Quality extraction and Telegram-friendly format selection.
- Tunable yt-dlp timeout/retry/fragment concurrency.
- No additional required production secrets.

Status: **COMPLETE**.

### Phase 5 — Adaptive Media Delivery — v0.3.0

Acceptance:

- Internal file budget separated from Telegram delivery budget.
- Automatic FFprobe + FFmpeg fitting for oversized video/audio.
- Bounded compression attempts and clear failure after exhaustion.
- Existing clip/MP3/download flows remain compatible.
- Compression can be disabled by configuration.

Status: **COMPLETE**.

### Phase 6 — Production Operations & Recovery — v0.3.0

Acceptance:

- `/readyz` checks DB, FFmpeg and FFprobe.
- Interrupted inline jobs do not remain permanently stuck after restart.
- Stale worker heartbeats become `OFFLINE`.
- Dashboard exposes richer runtime stats and job-event history.
- No destructive migration is required for existing `download_jobs` rows.
- CI must pass Ruff, Pytest and compile checks before the release is considered complete.

Status: **IMPLEMENTED — CI gate required on every commit**.

## Next candidates

Future work should be scoped separately after v0.3.0 is stable in production. No later phase is implied by this document until explicitly approved.
