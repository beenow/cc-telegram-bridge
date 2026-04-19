---
name: clawd-bridge-ops
description: Diagnose and repair the clawd-bridge Telegram daemon. Use when the user reports the bot is silent, crashing, flood-banned, failing to respond, or when they want a health check. Do NOT use for feature work on bridge.py/claude.py — this is operational triage only.
---

# clawd-bridge operator skill

You are acting as the operator for the `clawd-bridge` launchd daemon. Your job is to diagnose why the bot is misbehaving and apply a fix. Work the diagnostic flow in order; stop as soon as you confirm a root cause.

## Prerequisites

- Working directory should be `the clawd-bridge repo root`.
- The daemon label is `com.clawd.bridge`; plist at `~/Library/LaunchAgents/com.clawd.bridge.plist`.
- Logs: `logs/bridge.log` (stdout), `logs/bridge.err` (stderr).

## Diagnostic flow

### 1. Is the daemon running?

```
launchctl list | grep com.clawd.bridge
```

Columns are `PID  LastExit  Label`.

- **No row** → daemon isn't loaded. `launchctl load ~/Library/LaunchAgents/com.clawd.bridge.plist`.
- **PID `-`, non-zero LastExit** → crash loop. Read `logs/bridge.err` and the tail of `logs/bridge.log` for the stack trace.
- **PID present** → process alive. Continue — "alive" does not mean "functional".

### 2. Is the log still advancing?

```
stat -f "%Sm" logs/bridge.log
tail -n 40 logs/bridge.log
```

If the last timestamp is older than the user's last message, the bot received nothing (Telegram-side issue) **or** silently failed (see below).

### 3. Scan for the four known failure modes

Grep the last ~200 lines for each signature:

| Signature in log | Root cause | Fix |
| --- | --- | --- |
| `claude exited 127: env: node: No such file or directory` | Daemon PATH is missing `/usr/local/bin` or `/opt/homebrew/bin`, so `claude`'s `#!/usr/bin/env node` shebang can't find node. | Ensure plist `EnvironmentVariables.PATH` includes `~/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin`. Reload. |
| `telegram.error.Conflict: terminated by other getUpdates` | A second bot instance is polling the same token. | Find and kill it: `pgrep -fa 'src/bridge.py'`. Only one PID should match (the daemon). |
| `telegram.error.RetryAfter: Flood control exceeded. Retry in <N> seconds` | Telegram rate-limited the bot token (often triggered by a crash loop). | Nothing to do but wait `<N>` seconds. Confirm the `_error_handler` in `bridge.py` catches `RetryAfter` — if it's missing, the process will keep crashing and extending the ban. |
| `Claude did not respond within 30s — session may be stale` | A per-chat Claude session is wedged on `--resume`. | The code auto-recovers on "no conversation found", but if the CLI hangs without erroring, user should `/new` in that chat. As operator, you can also clear the stuck row: `sqlite3 data/bridge.db "UPDATE sessions SET claude_session_id=NULL WHERE chat_id=<id>;"`. |

### 4. Verify PATH inside the running process

```
ps eww -p <pid> | tr ' ' '\n' | grep ^PATH=
```

Must include `~/.local/bin`, `/opt/homebrew/bin`, `/usr/local/bin`. If not, the plist change didn't take effect — `launchctl unload` then `launchctl load` (a plain `kickstart` does **not** re-read EnvironmentVariables).

### 5. Smoke-test the claude CLI in daemon-equivalent env

```
env -i PATH=<the PATH from step 4> claude --version
```

Should print a version. If it prints `env: node: No such file or directory`, PATH is still wrong.

### 6. Inspect session state (optional)

```
sqlite3 data/bridge.db 'SELECT chat_id, model, claude_session_id, message_count FROM sessions;'
```

A single chat with a wedged `claude_session_id` can be cleared without affecting others.

## Restart procedure

```
launchctl unload ~/Library/LaunchAgents/com.clawd.bridge.plist
sleep 2
launchctl load   ~/Library/LaunchAgents/com.clawd.bridge.plist
sleep 3
launchctl list | grep com.clawd.bridge
tail -n 20 logs/bridge.log
```

After restart, expect a single `clawd-bridge starting` line (not two — if you see doubling, `setup_logging()` has been changed to re-add `StreamHandler(sys.stdout)`, which double-writes through launchd's stdout redirect).

## Things you must not do

- Do **not** `launchctl remove` the daemon to "clean restart" — that drops the plist reference and the next user reboot won't relaunch it.
- Do **not** delete `data/bridge.db` — it holds all per-chat session mappings. To reset one chat, `UPDATE sessions SET claude_session_id=NULL WHERE chat_id=?`.
- Do **not** `rm logs/bridge.log` while the daemon is running — Python holds the file descriptor; the file will be unlinked but disk will keep filling. Truncate instead: `: > logs/bridge.log`.
- Do **not** push `--no-verify` or force-load a modified plist without reading the current one first; the user hand-edits the plist on occasion.

## Reporting back

After triaging, report in this shape:

- **Status:** one line — "healthy" / "silent but alive" / "crash-looping" / "flood-banned N seconds left".
- **Root cause:** the log signature that confirmed it.
- **Action taken:** the exact commands you ran.
- **Verification:** what you observed post-fix (new log line, smoke test output).

Keep the report under 10 lines unless the user asks for detail.
