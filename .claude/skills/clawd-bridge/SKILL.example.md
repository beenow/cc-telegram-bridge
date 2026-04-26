---
name: clawd-bridge
description: Operate and evolve the clawd-bridge Telegram daemon. Covers two modes — (1) diagnostic triage when the bot is silent, crashing, flood-banned, or misbehaving; (2) UX design reference distilled from the clawdbot Telegram integration for when the user wants to improve or extend Telegram-facing behavior. Pick the relevant section based on the user's ask.
---

<!--
  Template note: this is the committed template. The real `SKILL.md` sits next to it and is gitignored,
  so each user can annotate freely without leaking personal info. Keep the two in sync by mirroring
  generic changes back here; never add paths, tokens, user IDs, or host-specific values to this file.
-->

# clawd-bridge skill

Two distinct modes below. Read the user's intent and use the right one — don't mix.

- **Operational triage** (§1): "bot isn't responding", "it's silent", "check health", "it crashed". Diagnostic only, no feature work.
- **UX design reference** (§2): "make the Telegram UX better", "add feature X from clawdbot", "how should streaming/commands/reactions/attachments work". Design choices, not step-by-step instructions.

---

# §1 — Operational triage

You are acting as the operator for the `clawd-bridge` launchd daemon. Your job is to diagnose why the bot is misbehaving and apply a fix. Work the diagnostic flow in order; stop as soon as you confirm a root cause.

## Prerequisites

- Working directory should be the clawd-bridge repo root.
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

### 3. Scan for the known failure modes

Grep the last ~200 lines for each signature:

| Signature in log | Root cause | Fix |
| --- | --- | --- |
| `claude exited 127: env: node: No such file or directory` | Daemon PATH is missing `/usr/local/bin` or `/opt/homebrew/bin`, so `claude`'s `#!/usr/bin/env node` shebang can't find node. | Ensure plist `EnvironmentVariables.PATH` includes `~/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin`. Reload. |
| `telegram.error.Conflict: terminated by other getUpdates` | Another process is polling the same bot token. A common source is a separately-installed Telegram plugin / bot reusing the token. Verify with `ps auxww \| grep -i telegram` and `pgrep -fa 'src/bridge.py'`. | If the collider is an external tool you intentionally run with the same token, that's a known collision — log noise is expected; in-flight tasks are NOT affected by Conflict (the bridge only misses *new* inbound updates during the colliding poll window). If it's a stray bridge.py, kill the non-daemon PID. |
| `telegram.error.RetryAfter: Flood control exceeded. Retry in <N> seconds` | Telegram rate-limited the bot token (often triggered by a crash loop). | Wait `<N>` seconds. Confirm `_error_handler` in `bridge.py` catches `RetryAfter` — if it's missing, the process will keep crashing and extending the ban. |
| `Claude did not respond within 30s — session may be stale` | The CLI emitted no output within 30s of starting. Usually a wedged `--resume` loading a large/corrupt history, or a logged-out Claude CLI that never prompted. This is now the **only** timeout path that auto-kills the subprocess; all post-first-byte timeouts are non-fatal by design. | The code auto-recovers on "no conversation found". If the CLI hangs silently, user should `/new`. Operator can clear: `sqlite3 data/bridge.db "UPDATE sessions SET claude_session_id=NULL WHERE chat_id=<id>;"`. |
| `Not logged in · Please run /login` in a CLI smoke test | The `claude` binary the daemon invokes is not the one your interactive shell logged into. Usually a dual-install issue: another `claude` exists (e.g. `/usr/local/bin/claude` from `sudo npm -g`) and your terminal resolves to that one. | Uninstall the stray binary (`sudo npm uninstall -g --prefix=/usr/local @anthropic-ai/claude-code`), or pin `CLAUDE_BIN` in the plist to the one you've authenticated. |
| User reports "long task never finishes / I never got a heartbeat" | The heartbeat coroutine in `_handle_message` was cancelled or the whole handler died silently. The bridge has no auto-kill after first byte by design, so a truly wedged task will sit forever unless explicitly cancelled. | Ask user to run `/ping` — that reports liveness from `_chat_liveness` without touching the subprocess. If `/ping` says "No active task" but the CLI subprocess is still alive (`pgrep -fa claude.*--print`), the bridge lost the handle; operator should kill the orphaned CLI PID manually and have the user `/new`. |
| User reports "I sent a message but bot is silent / I got a 'Queued' ack and forgot about it" | A prior task is still running for the chat. New messages are queued (one slot per chat, newer replaces) — they don't dispatch until the active task finishes. This is the intended UX as of 2026-04-25. | Ask user to `/ping` — it reports the active task's elapsed/silence and notes whether a queued message is waiting. To run the queued message immediately, `/stop` cancels the active task; to drop everything and reset, `/new`. |

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

Caveat: `env -i` strips macOS Keychain session hints, so a "Not logged in" from this test is not conclusive. Use `launchctl asuser $(id -u) claude --print --model sonnet "say ok"` for an auth test that matches the daemon's actual session context.

### 6. Inspect session state (optional)

```
sqlite3 data/bridge.db 'SELECT chat_id, model, claude_session_id, message_count FROM sessions;'
```

A single chat with a wedged `claude_session_id` can be cleared without affecting others.

## Restart procedure

```
launchctl bootout gui/$(id -u)/com.clawd.bridge
sleep 2
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.clawd.bridge.plist
sleep 3
launchctl list | grep com.clawd.bridge
tail -n 20 logs/bridge.log
```

(`bootout`/`bootstrap` is the modern replacement for `unload`/`load` — either works.) After restart, expect a single `clawd-bridge starting` line (not two — if you see doubling, `setup_logging()` has been changed to re-add `StreamHandler(sys.stdout)`, which double-writes through launchd's stdout redirect).

## Things you must not do

- Do **not** `launchctl remove` the daemon to "clean restart" — that drops the plist reference and the next user reboot won't relaunch it.
- Do **not** delete `data/bridge.db` — it holds all per-chat session mappings. To reset one chat, `UPDATE sessions SET claude_session_id=NULL WHERE chat_id=?`.
- Do **not** `rm logs/bridge.log` while the daemon is running — Python holds the file descriptor; the file will be unlinked but disk will keep filling. Truncate instead: `: > logs/bridge.log`.
- Do **not** force-load a modified plist without reading the current one first; the user hand-edits the plist on occasion.

## Reporting back

After triaging, report in this shape:

- **Status:** one line — "healthy" / "silent but alive" / "crash-looping" / "flood-banned N seconds left".
- **Root cause:** the log signature that confirmed it.
- **Action taken:** the exact commands you ran.
- **Verification:** what you observed post-fix (new log line, smoke test output).

Keep the report under 10 lines unless the user asks for detail.

---

# §2 — Telegram UX design reference

Distilled from reviewing a clawdbot checkout. Each pattern is tagged **adopt / adapt / skip** for clawd-bridge's narrower scope.

## Ground rules before adopting anything

- **clawd-bridge uses python-telegram-bot against the standard Bot API.** Several clawdbot patterns rely on grammy + a custom userbot/MTProto API (notably `sendMessageDraft`). Those won't work here. Always verify an API call exists in python-telegram-bot before porting.
- **Single-user, single file (`bridge.py`).** Multi-tenant patterns (pairing, allowlists, per-group policy) are over-engineered for our scope.
- **The existing design is good.** Edit-in-place streaming, queue-on-busy with `/stop` as explicit cancel, per-chat Claude session, `/new` to reset — clawdbot validates most of these; don't churn them. (Note: 2026-04-25 we replaced the old "new message cancels current task" steering with a queue, since the user is often away from desk and an accidental follow-up would kill a multi-hour task.)

## 1. Streaming cadence

**Clawdbot:** Uses a custom "draft" API that overwrites a streaming preview without sending edit events. Throttles at 300ms default, 50ms floor. Stops cleanly when text exceeds 4096 chars. Three modes: `block` (semantic paragraphs via chunker), `partial` (every text chunk), `off`.

**For clawd-bridge:** Standard Bot API has no draft messages — we must edit. Current gate is `EDIT_INTERVAL_CHARS=120` and `EDIT_MIN_SECS=0.8`. Stay conservative: Telegram's informal limit is ~1 edit/sec per chat.

- **Adapt:** If response reaches 4096 chars, stop editing and start sending new messages — don't let edits fail silently. clawdbot ref: `src/telegram/draft-stream.ts:43-48`.
- **Adapt:** Consider a "semantic block" chunking mode (flush on `\n\n`) for very long responses so the UI stabilizes at paragraph boundaries instead of mid-word. Their chunker: `src/telegram/draft-chunking.ts:9-41`.
- **Skip:** `block`/`partial`/`off` mode switching — premature config surface for a single-user bot.

## 2. Progress signals

**Clawdbot:**
- `sendChatAction("typing")` at stream start and periodically during.
- Separate action `"record_voice"` when a voice message is being prepared.
- ACK reactions: on receiving a message the bot optionally adds a ✅ via `setMessageReaction`, removes it after reply. Policy scope: `all | direct | group-all | group-mentions | off` (`src/channels/ack-reactions.ts:16-29`).
- No per-tool spinners — streaming preview is the progress indicator.

**For clawd-bridge:**
- **Done:** ACK reaction via `bot.set_message_reaction(chat_id, message_id, [ReactionTypeEmoji("👀")])` on receipt, swap to ✅ on success, clear on error. Bot API 7.0+.
- **Done:** Long-task heartbeat. `_heartbeat()` in `bridge.py` sends a passive "⏳ Still working… (running Nm, last output Nm ago)" message every 5 min of CLI silence. Crucially this **sends a new message** instead of editing the streaming placeholder, so any accumulated output is preserved. Paired with a `/ping` command that reads `_chat_liveness[chat_id]` (started_at / last_chunk_at / bytes_streamed) and reports without disturbing the subprocess. Rationale: user is often away from desk; losing a task to a silent timeout = whole day lost. Reliability > auto-completion.
- **Done (2026-04-25):** Queue-by-default for incoming messages while a task is running. `_pending[chat_id]` holds at most one queued message (newer replaces) and is drained by `_run_and_drain` after the active task finishes. `/stop` is the explicit cancel; `/new` cancels + drops queue + resets the session. The Application is built with `concurrent_updates=True` so the queued message's `on_message` actually runs while the prior task is still streaming — without it, PTB serializes per-chat updates and the queue ack never gets sent. Replaces the prior "new message cancels current task" steering, which lost in-progress work whenever the user typed a follow-up while away.
- **Consider:** Replace the animated "N is thinking ." placeholder with a periodic `send_chat_action("typing")` refresh every ~4s (the action auto-expires at ~5s). Cheaper on Telegram's edit budget.
- **Skip:** Scope policy — single-user bot, just ACK every time or not at all.

## 3. Error UX

**Clawdbot:**
- HTML parse-mode fallback: if `editMessageText` fails with parse error from malformed model output, retry plain (`src/telegram/bot/delivery.ts:247-250`). Warn-log, don't surface.
- 429 rate-limit handling: extract `retry_after` from Telegram's `RetryAfter`, sleep + retry (`src/infra/retry-policy.ts:22-58`).
- Silent skip on media download failures, logged but not user-facing.
- Cap exceeded: stop streaming cleanly rather than error-looping.

**For clawd-bridge:**
- **Done:** Parse-mode fallback in `_edit()` — catches `BadRequest("can't parse entities")`, retries `parse_mode=None`.
- **Done:** `RetryAfter` in `_error_handler` (see CLAUDE.md).
- **Done:** Removed the mid-stream kill timer and the total-deadline cap entirely. A previous bug had a 30-min `INTER_LINE_TIMEOUT` reported in error text but `COMMAND_TIMEOUT_SECS=600` as the actual killer — every long task died at exactly 600s with a misleading "1800s" message. Current design: the **only** auto-kill path is `FIRST_BYTE_TIMEOUT=30s` (catches wedged `--resume`). After first byte, silence is never fatal; `READLINE_POLL_SECS=300` just keeps the event loop responsive between readline polls. Never re-introduce a mid-stream timeout without the same heartbeat design — the user relies on tasks *always* finishing or being explicitly cancelled, not on heuristic timeouts.
- **Adapt:** Remaining error messages (`"Claude did not respond within 30s..."`, `"No conversation found..."`) are specific and actionable with `/new`. Don't regress to generic "Error occurred" when catching exceptions.

## 4. Attachments & media

**Clawdbot:**
- Inbound: photos/videos/docs/audio/voice → `getFile()` → temp dir → pass path to the model.
- Outbound: smart type detection — image → `sendPhoto`, audio → `sendAudio` or `sendVoice` based on `[[audio_as_voice]]` metadata flag.
- Caption split at 1024 chars, overflow as follow-up (`src/telegram/bot/delivery.ts:91-93`).
- Media groups buffered and reassembled across an album.

**For clawd-bridge:**
- **Done:** `_download_attachments()` saves photo / document / video / audio / voice / video_note into `downloads/{chat_id}_{ts}_{msg_id}_{safe_name}`. `_build_prompt_with_attachments()` prepends a path-listing block so the Claude CLI can open the file via its own Read/Bash tools. `on_message` extracts text from `.text` **or** `.caption`, so a media message with a caption flows through the same streaming path. `downloads/` is gitignored.
- **Skip:** Media groups — no use case for a single-user Claude frontend.
- **Skip:** Voice-note output — no audio output path today. (Inbound voice notes are saved as `.ogg`; transcription is a future enhancement.)

## 5. Commands & inline UI

**Clawdbot:**
- Native commands registered via `setMyCommands`, with a skills-vs-commands distinction.
- Inline keyboards (`InlineKeyboardMarkup`) with callback handlers, scoped per policy.
- Callback data strictly ≤64 chars (Telegram hard limit).

**For clawd-bridge:**
- **Done:** `bot.set_my_commands()` in `post_init` registers `/new`, `/model`, `/status`, `/help` in Telegram's `/` picker.
- **Done:** Inline keyboard for `/model` — `/model` with no arg shows `sonnet` / `opus` / `haiku` buttons with a ● on the current. `cb_model` handles the `model:<name>` callback (registered with `pattern=r"^model:"`). Callback data stays well under Telegram's 64-char limit.
- **Skip:** Per-scope command visibility — single-user.

## 6. Session persistence & forum topics

**Clawdbot:**
- Chat ID + forum topic ID as composite key.
- History capped per-group (default 100 messages).
- Inbound message debouncing: combines rapid-fire messages into one turn (`src/telegram/bot-handlers.ts:61-105`).

**For clawd-bridge:**
- **Adapt (optional):** Debouncing rapid messages — if the user sends "one thing" + "also this" within ~2s, merge into a single Claude turn rather than queueing as two. The current queue keeps only the *latest* message anyway (newer replaces queued), so two rapid follow-ups already collapse cleanly without a real debounce — pure debounce would only help when the chat is *idle* and the user is mid-thought.
- **Skip:** Forum topics, per-topic history — single-user DM doesn't use topics.
- **Already done:** Per-chat Claude session UUID in `data/bridge.db`.

## 7. Clever details worth remembering

- **Sent-message cache** (`src/telegram/sent-message-cache.ts`): remember message_ids the bot sent so echoes/retries don't reprocess. Only relevant if we ever enable group mode.
- **Update offset persistence** across restarts: clawd-bridge does the opposite (`drop_pending_updates=True`) — safer for a bot that's been down during a crash loop. Don't change this; see CLAUDE.md.
- **Thread params helper** (`src/telegram/bot/helpers.ts:15-24`): centralize `message_thread_id` handling rather than sprinkling it through every send call. Worth doing if we ever add group support.

## How to apply this skill

When the user asks to "improve the Telegram UX", "make it feel more polished", "add X feature from clawdbot":

1. Identify which section of this doc covers it.
2. Re-read the referenced clawdbot files via Read tool if available (at `../clawdbot/`) — they're source of truth.
3. Check Bot API / python-telegram-bot support before porting anything that uses grammy-specific APIs.
4. Prefer small, non-invasive changes. The current `bridge.py` is short and readable; don't introduce a framework to add one feature.

## What NOT to port

- `sendMessageDraft` — not in Bot API.
- Multi-tenant pairing, allowlist editors, per-group policies — out of scope.
- Media group reassembly — no use case.
- Voice-note output — no audio path.
- ACP (Agent Communication Protocol) layer — clawdbot's agent orchestration; we have the Claude CLI directly.
