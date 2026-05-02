# Changelog

All notable changes to cc-telegram-bridge are listed here. Patch-level improvements (bug fixes, small reliability tweaks) are grouped under the release that introduced the feature they affect.

---

## [Unreleased]

---

## 2026-05-02

### Added
- **Multi-photo support** — Telegram albums (multiple photos sent together) are now debounced into a single prompt instead of generating a separate "📥 Queued" reply for each photo in the group.

---

## 2026-04-25

### Changed
- **Message queue replaces steering** — a new message sent while Claude is working is now queued (one slot per chat; newer messages replace older ones) rather than cancelling the active task. The bridge acks with "📥 Queued" and runs the queued message as soon as the task finishes.
- **`/stop` command** — cancels the current task and drops any queued message without resetting the session.

### Fixed
- Improvement to heartbeat: "⏳ Still working..." no longer appears after a task has already completed.

---

## 2026-04-19

### Changed
- **No more mid-stream timeout** — the bridge no longer auto-kills Claude after a silence window. Long agentic tasks (tool calls, large file reads) legitimately go quiet between steps; killing them was causing lost work. The 30s first-byte timeout is still enforced.
- **`/ping` liveness check** — reports whether a task is running, how long it's been silent, and whether a message is queued, without disturbing the subprocess.
- **Passive heartbeat** — sends "⏳ Still working..." every 5 minutes of CLI silence so you know it's alive.

---

## 2026-04-18

### Added
- **Attachment support** — send a photo, document, PDF, audio, voice note, or video. The bridge downloads it to `downloads/` and tells Claude the path so its file tools can read or analyze it.
- **Inline `/model` picker** — `/model` with no argument shows one-tap buttons for `sonnet`, `opus`, and `haiku`.
- **ACK reactions** — bot reacts 👀 on receipt and ✅ on completion.
- **Command registry** — all commands registered with Telegram via `setMyCommands` so they show in the `/` picker.

### Fixed
- Improvement to CLI resilience: bridge now parses the `errors[]` array introduced in CLI v2.1.x alongside the legacy `result` field, so expired-session recovery continues to work after CLI upgrades.
- Improvement to streaming: `asyncio.StreamReader` buffer raised to 10 MB, preventing `LimitOverrunError` crashes on large tool outputs.
- Improvement to subprocess path: `CLAUDE_BIN` resolved dynamically via `shutil.which()` instead of a hardcoded path.

---

## 2026-04-15

### Fixed
- Improvement to flood control: `RetryAfter` errors from Telegram are now caught and slept through instead of crashing the process (which under `KeepAlive` would trigger a crash loop and a Telegram ban).
- Improvement to session recovery: expired or missing Claude sessions are detected, cleared from the database, and retried with a fresh session ID transparently.
- Improvement to startup: `basicConfig(force=True)` prevents duplicate log lines across launchd restart cycles.

---

## 2026-04-14 — Initial public release

### Added
- Telegram → Claude CLI → Telegram relay via long polling (no public webhook required)
- Persistent per-chat sessions stored in local SQLite (`data/bridge.db`)
- Streaming replies edited in-place as Claude generates
- Multi-model support — switch between `sonnet`, `opus`, `haiku` per session with `/model <name>`
- Soul / personality layer — define assistant name, tone, and context in `soul.md`
- macOS launchd daemon via `scripts/install-daemon.sh` — auto-starts on login, auto-restarts on crash
- Allowlist — restrict access to specific Telegram user IDs via `ALLOWED_USER_IDS`
- Long response splitting — replies over Telegram's 4096-char limit split automatically
- `soul.example.md` template — personal config stays gitignored
