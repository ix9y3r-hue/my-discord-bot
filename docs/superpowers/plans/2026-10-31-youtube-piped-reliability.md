# YouTube Piped Fallback Reliability Implementation Plan

> **For agentic workers:** Use the Superpowers TDD workflow. Tasks use checkbox syntax.

**Goal:** Make the existing Piped fallback observable and resilient enough to diagnose and recover from YouTube extraction blocks without changing the bot's user-facing source rules.

**Architecture:** Keep yt-dlp as the primary extractor and Piped as the existing secondary path. Discover current Piped API instances from the official instance registry with a bounded cache and a safe built-in fallback. Validate audio URLs against the response's documented `proxyUrl`, and log sanitized, stage-specific failure reasons instead of silently swallowing them.

**Tech Stack:** Python 3.12, standard-library `unittest`, `urllib`, GitHub Actions, yt-dlp, Railway.

**Spec:** Existing bot behavior and user constraints in this conversation; official Piped API schema and public instance registry.

## Global Constraints

- Keep `main.py` as the bot's main file; do not create `main-2.py`.
- Preserve `ش ي` as YouTube title search and direct YouTube URLs.
- Preserve `ش س` as SoundCloud-only; it must not contact YouTube or Piped.
- Do not send `YOUTUBE_COOKIES`, `DISCORD_TOKEN`, or other secrets to Piped.
- Preserve the automatic join sound, playback panel, queue, temporary-room features, activity leaderboard, and existing bot commands.
- Keep Piped network timeouts bounded; do not add unbounded retries or background polling.
- Do not deploy to production as part of the test work. A PR and verified tests precede any production release.

## Review Focus

- Public instance registry unavailable, malformed, or changed: bounded fallback to the built-in known API list.
- Public instance URL is malformed or unsafe: reject non-HTTPS, credentials, query strings, fragments, and unexpected paths.
- Stream URL uses the API response's `proxyUrl` host, which may differ from the API host: accept only the documented proxy host.
- Stream URL points to an unrelated host or is not audio: reject it without logging signed query parameters.
- Every Piped instance fails or returns no usable audio stream: expose a concise, sanitized stage and reason in logs; continue existing SoundCloud fallback behavior.
- `ش س` remains isolated from YouTube and Piped code paths.

---

### Task 1: Add offline regression tests and a test workflow

**Files:**
- Create: `tests/test_youtube_piped.py`
- Create: `.github/workflows/python-tests.yml`

**Interfaces:**
- Tests will exercise public functions/helpers in `youtube_piped.py` using deterministic fixtures and patched network boundaries; no live endpoints or secrets are required.

- [ ] Write tests for parsing official instance-list Markdown, CDN filtering, safe URL validation, proxy-host acceptance/rejection, and Piped stream response handling.
- [ ] Add a Python 3.12 GitHub Actions workflow that runs `python -m unittest discover -s tests -v` on pushes and pull requests.
- [ ] Run the workflow and confirm expected RED failures against the current implementation.

### Task 2: Implement safe instance discovery and correct stream-host validation

**Files:**
- Modify: `youtube_piped.py`
- Test: `tests/test_youtube_piped.py`

**Interfaces:**
- Add a parser that returns validated `(api_url, has_cdn)` records from the official public-instance registry.
- Add a bounded in-memory cache for discovery; fall back to the current three known CDN instances when discovery fails.
- Preserve `PIPED_API_URLS` as an explicit operator override.
- Update stream validation to use the `proxyUrl` hostname returned by `/streams/:videoId`; retain a conservative legacy fallback only when `proxyUrl` is absent.
- For audio streaming, prefer instances marked CDN-capable; metadata search may use a small bounded set of other valid API instances.

- [ ] Implement only after Task 1's tests have been observed failing.
- [ ] Run the focused test module and confirm GREEN.

### Task 3: Add sanitized Piped failure diagnostics

**Files:**
- Modify: `youtube_piped.py`
- Test: `tests/test_youtube_piped.py`

**Interfaces:**
- Log the failed stage (discovery, search, stream metadata, or stream-host validation), API hostname, HTTP/error category, and counts.
- Never log full signed media URLs, cookies, or request query strings.
- Keep detailed per-instance failures at INFO/WARNING without changing the bot's normal response wording or creating retry loops.

- [ ] Add tests that assert the logged diagnostic includes stage and host but excludes signed query values.
- [ ] Run the focused test module and the entire unittest suite.

### Task 4: Review and release gate

**Files:**
- Review: `main.py`, `youtube_piped.py`, `patch_main.py`, `Dockerfile`, `requirements.txt`

- [ ] Confirm `patch_main.py` does not overwrite or conflict with the new implementation.
- [ ] Confirm `ش س` remains SoundCloud-only and the automatic join sound is unchanged.
- [ ] Run the test workflow and a clean Docker build/compile check.
- [ ] Request code review before merge.
- [ ] Do not merge or deploy to production until the Piped response and playback behavior have been verified from Railway. A successful build alone is not proof of audio playback.
