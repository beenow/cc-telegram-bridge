# clawd-bridge

> **Template note:** This is the committed template. The working `CLAUDE.md` is gitignored so each user can annotate freely without leaking personal info. Keep the two in sync by mirroring generic changes back here; never add paths, tokens, user IDs, or host-specific values to this file.

Telegram bot that fronts the local Claude Code CLI. Each Telegram chat maps to a persistent Claude session; messages are streamed back as edits to a single placeholder reply. No Anthropic API key — uses the Claude CLI subscription.

## Architecture

- `src/bridge.py` — Telegram polling entrypoint. Handlers for `/start`, `/new`, `/model`, `/status`, plus a text handler that streams a reply. Per-chat active task is stored in `_active_tasks`; a new message cancels the previous one (steering).
- `src/claude.py` — Wraps the `claude` CLI as an async subprocess. `stream()` yields `StreamChunk(text|done|error)` parsed from `--output-format stream-json --verbose`. Enforces a 30s first-byte timeout and a total `COMMAND_TIMEOUT_SECS` deadline.
- `src/db.py` — SQLite at `data/bridge.db`. One row per chat with the Claude session UUID and model. `exchanges` table is an append-only audit log.
- `src/config.py` — Reads `.env`. `soul.md` (if present) is prepended to the system prompt.

## Running

The bot runs as a launchd user agent: `~/Library/LaunchAgents/com.clawd.bridge.plist` → `python3 src/bridge.py`. Logs go to `logs/bridge.log` (stdout) and `logs/bridge.err` (stderr); launchd's `StandardOutPath` does the redirect, so `setup_logging()` uses **only** a `FileHandler` — never add a `StreamHandler(sys.stdout)` or every line double-writes.

Operational commands:

```
launchctl list | grep com.clawd.bridge       # PID and last exit code
launchctl unload ~/Library/LaunchAgents/com.clawd.bridge.plist
launchctl load   ~/Library/LaunchAgents/com.clawd.bridge.plist
tail -f logs/bridge.log
```

## The `claude` CLI / `node` PATH gotcha

`/usr/local/bin/claude` is a `#!/usr/bin/env node` shebang, so whoever spawns it needs `node` reachable on PATH. launchd gives a user agent the bare `/usr/bin:/bin:/usr/sbin:/sbin` by default, which does **not** include `/usr/local/bin` or `/opt/homebrew/bin`. Without an explicit `PATH` in the plist, every claude invocation fails with `env: node: No such file or directory` (exit 127) and the bot appears alive but silent. The plist sets `EnvironmentVariables.PATH` to include `~/.local/bin`, `/opt/homebrew/bin`, `/usr/local/bin` — do not remove it. `claude.py` also honours `CLAUDE_BIN` if you want to pin a specific install.

## Telegram quirks

- **Conflict / flood-control**: two running instances cause `telegram.error.Conflict`, and a crash-loop under launchd's `KeepAlive` gets the bot `RetryAfter`-banned. The registered error handler catches `RetryAfter` and sleeps instead of dying — don't remove it.
- **Streaming edits**: reply is a single message edited as chunks arrive. `EDIT_INTERVAL_CHARS=120` and `EDIT_MIN_SECS=0.8` gate edit frequency to avoid hitting Telegram rate limits. Final send splits at `\n` under the 4096-char cap.
- **Steering**: sending a new message during streaming cancels the prior subprocess and the placeholder is marked `[interrupted]`.
- **Attachments**: Claude CLI can't take file paths in `--print` mode, so inbound media (photo/document/video/audio/voice/video_note) is downloaded to `downloads/` with filename pattern `{chat_id}_{ts}_{msg_id}_{sanitized_name}`, and the prompt is prefixed with an `[Attachments available on disk — use your Read/file tools...]` block listing the paths. Claude can then open them via its own file tools.
- **Inline `/model`**: `/model` with no argument shows a one-tap inline keyboard. Callback data is `model:<name>` (≤64 chars). Handled by `cb_model` dispatched on the `^model:` regex.

## Session lifecycle

- First message in a chat → generate UUID, store as `claude_session_id`, invoke `claude --session-id <uuid>`.
- Subsequent messages → `claude --resume <uuid>`.
- If the CLI replies "no conversation found" (e.g. user logged out of Claude Code, local session store wiped), `_stream_with_session_recovery()` resets the row and retries once with a fresh UUID — transparent to the user.
- `/new` zeroes `claude_session_id` and `message_count`; model/tools are preserved.

## Config

`.env` required: `TELEGRAM_BOT_TOKEN`, `ALLOWED_USER_IDS` (comma-separated ints — any junk triggers a hard exit). Optional: `DEFAULT_MODEL` (default `sonnet`), `COMMAND_TIMEOUT_SECS` (default 600), `DATA_DIR`, `LOG_DIR`, `DOWNLOADS_DIR` (default `./downloads`, gitignored), `LOG_LEVEL`, `TRADING_SYSTEM_ENABLED` + `TRADING_SYSTEM_LOG_DIR` + `TRADING_SYSTEM_CONFIG_PATH`.

`soul.md` (gitignored) is prepended to the system prompt. `soul.example.md` is the published template.

## Known edge cases baked into code

- `asyncio` `StreamReader` limit is bumped to 10 MB (`_STREAM_LIMIT` in `claude.py`) — default 64 KB crashed on large tool outputs with `LimitOverrunError`.
- `FIRST_BYTE_TIMEOUT = 30s` catches stale `--resume` sessions that hang loading large/corrupt history; the user sees an actionable "send /new" hint.
- `basicConfig(force=True)` clears root handlers on startup so launchd restarts don't accumulate duplicates inside the Python process.

## Diagnostic checklist when "bot isn't responding"

1. `launchctl list | grep com.clawd.bridge` — PID alive? Last exit code `0`?
2. `tail -n 50 logs/bridge.log` — look for `claude exited 127` (PATH/node issue), `RetryAfter` (flood-banned — count down and wait), `Conflict` (a second instance somewhere).
3. `ps eww -p <pid> | tr ' ' '\n' | grep ^PATH=` — confirm PATH has `.local/bin`, `/opt/homebrew/bin`, `/usr/local/bin`.
4. `env -i PATH=<daemon-path> claude --version` — smoke-test the CLI in the daemon's environment.
5. `sqlite3 data/bridge.db 'select * from sessions;'` — check per-chat session state; a stuck `claude_session_id` can be cleared manually or via `/new`.

## What NOT to do

- Don't re-add `StreamHandler(sys.stdout)` to `setup_logging()` — launchd already mirrors stdout to the log file (double lines).
- Don't drop the plist `PATH` env var — bot goes silent within one message.
- Don't remove `drop_pending_updates=True` from `run_polling` — after a flood ban or long restart, queued updates can flood the handler.
- Don't skip the `RetryAfter` branch in `_error_handler` — re-raising crashes the process under `KeepAlive` and re-triggers the ban.
