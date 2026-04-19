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
| `telegram.error.Conflict: terminated by other getUpdates` | A second bot instance is polling the same token. | Find and kill it: `pgrep -fa 'src/bridge.py'`. Only one PID should match (the daemon). |
| `telegram.error.RetryAfter: Flood control exceeded. Retry in <N> seconds` | Telegram rate-limited the bot token (often triggered by a crash loop). | Wait `<N>` seconds. Confirm `_error_handler` in `bridge.py` catches `RetryAfter` — if it's missing, the process will keep crashing and extending the ban. |
| `Claude did not respond within 30s — session may be stale` | A per-chat Claude session is wedged on `--resume`. | The code auto-recovers on "no conversation found", but if the CLI hangs without erroring, user should `/new` in that chat. As operator, you can also clear the stuck row: `sqlite3 data/bridge.db "UPDATE sessions SET claude_session_id=NULL WHERE chat_id=<id>;"`. |
| `Not logged in · Please run /login` in a CLI smoke test | The `claude` binary the daemon invokes is not the one your interactive shell logged into. Usually a dual-install issue: another `claude` exists (e.g. `/usr/local/bin/claude` from `sudo npm -g`) and your terminal resolves to that one. | Uninstall the stray binary (`sudo npm uninstall -g --prefix=/usr/local @anthropic-ai/claude-code`), or pin `CLAUDE_BIN` in the plist to the one you've authenticated. |

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
- **The existing design is good.** Edit-in-place streaming, steering cancel, per-chat Claude session, `/new` to reset — clawdbot validates these choices; don't churn them.

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
- **Adapt:** Current error messages (`"Claude stalled..."`, `"Claude timed out..."`, `"No conversation found..."`) are specific and actionable with `/new`. Don't regress to generic "Error occurred" when catching exceptions.

## 4. Attachments & media

**Clawdbot:**
- Inbound: photos/videos/docs/audio/voice → `getFile()` → temp dir → pass path to the model.
- Outbound: smart type detection — image → `sendPhoto`, audio → `sendAudio` or `sendVoice` based on `[[audio_as_voice]]` metadata flag.
- Caption split at 1024 chars, overflow as follow-up (`src/telegram/bot/delivery.ts:91-93`).
- Media groups buffered and reassembled across an album.

**For clawd-bridge:**
- **Adapt (future):** Claude CLI doesn't natively accept attachment paths in `--print` mode. Inbound images would require copying the file into the session cwd and injecting a reference into the prompt ("image at /tmp/xyz.png — look at it"). Hacky but works.
- **Skip:** Media groups — no use case for a single-user Claude frontend.
- **Skip:** Voice-note opt-in — no audio output path today.

## 5. Commands & inline UI

**Clawdbot:**
- Native commands registered via `setMyCommands`, with a skills-vs-commands distinction.
- Inline keyboards (`InlineKeyboardMarkup`) with callback handlers, scoped per policy.
- Callback data strictly ≤64 chars (Telegram hard limit).

**For clawd-bridge:**
- **Done:** `bot.set_my_commands()` in `post_init` registers `/new`, `/model`, `/status`, `/help` in Telegram's `/` picker.
- **Consider:** Inline keyboard for `/model` — tap `sonnet` / `opus` / `haiku` instead of typing. CallbackQueryHandler dispatches on `callback_data` strings; keep them ≤64 chars.
- **Skip:** Per-scope command visibility — single-user.

## 6. Session persistence & forum topics

**Clawdbot:**
- Chat ID + forum topic ID as composite key.
- History capped per-group (default 100 messages).
- Inbound message debouncing: combines rapid-fire messages into one turn (`src/telegram/bot-handlers.ts:61-105`).

**For clawd-bridge:**
- **Adapt (optional):** Debouncing rapid messages is interesting — if user sends "one thing" + "also this" within ~2s, merge into one Claude turn rather than cancelling the first via steering. But steering is already a good default; debouncing is mutually exclusive. Not recommended unless user asks.
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
