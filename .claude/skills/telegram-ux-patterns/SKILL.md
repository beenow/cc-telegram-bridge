---
name: telegram-ux-patterns
description: Transferable interaction-design patterns from the clawdbot Telegram integration, scoped to what's actionable in clawd-bridge (python-telegram-bot, Bot API, single-user). Use when adding or refactoring Telegram-facing UX in bridge.py — streaming cadence, progress signals, error surfacing, attachments, access control. Not a feature list; a design reference.
---

# Telegram UX patterns — lessons from clawdbot

This is a distilled reference of interaction-design decisions from a clawdbot checkout (sibling repo at `../clawdbot/`) that are worth considering when evolving `clawd-bridge`. Each pattern flags whether it's **adopt**, **adapt**, or **skip** for this project, with rationale.

## Ground rules before adopting anything

- **clawd-bridge uses python-telegram-bot against the standard Bot API.** Several clawdbot patterns rely on grammy + a custom userbot/MTProto API (notably `sendMessageDraft`). Those won't work here. Always verify an API call exists in python-telegram-bot before porting.
- **Single-user, single file (`bridge.py`).** Multi-tenant patterns (pairing, allowlists, per-group policy) are over-engineered for our scope.
- **The existing design is good.** Edit-in-place streaming, steering cancel, per-chat Claude session, `/new` to reset — clawdbot validates these choices; don't churn them.

## 1. Streaming cadence

**What clawdbot does:** Uses a custom "draft" API that overwrites a streaming preview without sending edit events. Throttles at **300ms default, 50ms floor**. Stops cleanly when text exceeds 4096 chars. Supports three modes: `block` (semantic paragraphs via chunker), `partial` (every text chunk), `off`.

**For clawd-bridge:** Standard Bot API has no draft messages — we must edit. Current gate is `EDIT_INTERVAL_CHARS=120` and `EDIT_MIN_SECS=0.8`. clawdbot's 300ms throttle is aggressive but they're not hitting `editMessageText` (which is rate-limited per chat). **Stay conservative on edit frequency** — 800ms floor is safe against Telegram's ~1 edit/sec per-chat informal limit.

- **Adapt:** If response reaches 4096 chars, stop editing and start sending new messages — don't let edits fail silently. clawdbot file:line `src/telegram/draft-stream.ts:43-48` shows the stop-on-cap pattern.
- **Adapt:** Consider a "semantic block" chunking mode (flush on `\n\n`) for very long responses so the UI stabilizes at paragraph boundaries instead of mid-word. Their chunker: `src/telegram/draft-chunking.ts:9-41`.
- **Skip:** `block`/`partial`/`off` mode switching — premature config surface for a single-user bot.

## 2. Progress signals (what to show while Claude is working)

**What clawdbot does:**
- `sendChatAction("typing")` at stream start and periodically during.
- Separate action `"record_voice"` when a voice message is being prepared (`src/telegram/bot/delivery.ts:132-136`).
- **ACK reactions:** On receiving a message the bot optionally adds a ✅ (configurable) via `setMessageReaction`; removes it after reply. Policy scope: `all | direct | group-all | group-mentions | off` (`src/channels/ack-reactions.ts:16-29`).
- **No per-tool spinners.** Streaming preview is itself the progress indicator.

**For clawd-bridge:**
- **Adopt:** `bot.send_chat_action(chat_id, "typing")` at the start of each message handler and re-upped every ~4s during long tool runs. python-telegram-bot supports this; the action auto-expires at ~5s so must be repeated. The current "N is thinking ." animated placeholder is a workable substitute but chews into Telegram's edit budget — a single `typing` action would be cheaper.
- **Adopt:** ACK reaction via `bot.set_message_reaction(chat_id, message_id, [ReactionTypeEmoji("👀")])` immediately on receipt. Signals "got it, working on it" without waiting for the placeholder message to send. Remove or swap to ✅ on completion. This works over standard Bot API (added in v7.0).
- **Skip:** Scope policy. Single-user bot, just add the reaction every time or not at all.

## 3. Error UX

**What clawdbot does:**
- **HTML parse-mode fallback:** If `editMessageText` fails with parse error (malformed entities from model output), retry as plain text (`src/telegram/bot/delivery.ts:247-250`). Log as warning, don't surface to user.
- **429 rate-limit handling:** Extract `retry_after` from Telegram's `RetryAfter` error; sleep + retry (`src/infra/retry-policy.ts:22-58`).
- **Silent skip** on media download failures, logged but not user-facing.
- **Cap exceeded:** Stop streaming cleanly rather than error-looping.

**For clawd-bridge:**
- **Adopt:** Parse-mode fallback is high-value. Claude's output often has backticks/asterisks that Telegram rejects when `parse_mode=MarkdownV2`. Catch `BadRequest` on edit/send, retry with `parse_mode=None`. We already saw this in the log (`parse_mode=<ParseMode.MARKDOWN>` on line 17:32:14 preceding retries). Wrap the edit call.
- **Already done:** `RetryAfter` handling is in `_error_handler` per CLAUDE.md — keep it.
- **Adapt:** Current error surface is `"Error: Claude stalled..."` or `"Error: Claude timed out..."`. These are good (specific, actionable with `/new`). Don't regress to generic "Error occurred" when catching exceptions.

## 4. Attachments / media

**What clawdbot does:**
- Inbound: photos/videos/docs/audio/voice → download via `getFile()` to temp dir → pass path to the model.
- Outbound: smart type detection — image → `sendPhoto`, audio → `sendAudio` or `sendVoice` based on a `[[audio_as_voice]]` metadata flag.
- Caption split at 1024 chars, overflow as follow-up message (`src/telegram/bot/delivery.ts:91-93`).
- **Media groups**: buffered and reassembled if user sends multiple photos in one album.

**For clawd-bridge:**
- **Adapt (future):** Claude CLI doesn't natively accept attachment paths on the command line in `--print` mode. Inbound images would require either (a) waiting for CLI support, or (b) copying the file into the session cwd and injecting a reference into the prompt ("I've placed the image at /tmp/xyz.png — look at it"). The latter is hacky but works.
- **Skip:** Media groups — we don't need album-reassembly for a single-user Claude frontend.
- **Skip:** Voice-note opt-in — no audio output path today.

## 5. Commands & inline UI

**What clawdbot does:**
- Native commands registered via `setMyCommands`, with a skills-vs-commands distinction.
- Inline keyboards (`InlineKeyboardMarkup`) with callback handlers, scoped per policy.
- Callback data strictly ≤64 chars (Telegram hard limit).

**For clawd-bridge:**
- **Adapt:** We already have `/start`, `/new`, `/model`, `/status`. Register them via `bot.set_my_commands([...])` on startup so they show in Telegram's `/` picker. Current code may not do this — verify and add if missing.
- **Consider:** Inline keyboard for `/model` — tap `sonnet` / `opus` / `haiku` instead of typing. Small, high-affordance win. CallbackQueryHandler dispatches on `callback_data` strings; keep them ≤64 chars.
- **Skip:** Per-scope command visibility — single-user.

## 6. Session persistence & forum topics

**What clawdbot does:**
- Chat ID + forum topic ID as composite key.
- History capped per-group (default 100 messages).
- Inbound message debouncing: combines rapid-fire messages into one turn (`src/telegram/bot-handlers.ts:61-105`).

**For clawd-bridge:**
- **Adapt (optional):** Debouncing rapid messages is interesting — if user sends "one thing" + "also this" within ~2s, merge them into one Claude turn rather than cancelling the first via steering. But steering is already a good default; debouncing would be mutually exclusive. Not recommended unless user asks.
- **Skip:** Forum topics, per-topic history — single-user DM doesn't use topics.
- **Already done:** Per-chat Claude session UUID in `data/bridge.db`.

## 7. Clever details worth remembering

- **Sent-message cache** (`src/telegram/sent-message-cache.ts`): remember message_ids the bot sent so echoes/retries don't reprocess. Only relevant if we ever enable group mode.
- **Update offset persistence** across restarts: `drop_pending_updates=True` in current code does the opposite (safer for a bot that's been down during a crash loop — correct choice per CLAUDE.md).
- **Thread params helper** (`src/telegram/bot/helpers.ts:15-24`): centralize `message_thread_id` handling rather than sprinkling it through every send call. Worth doing if we ever add group support.

## How to apply this skill

When the user asks to "improve the Telegram UX", "make it feel more polished", "add X feature from clawdbot":

1. Identify which section of this doc covers it.
2. Re-read the referenced clawdbot files via Read tool — they're source of truth.
3. Check Bot API / python-telegram-bot support before porting anything that uses grammy-specific APIs.
4. Prefer small, non-invasive changes. The current `bridge.py` is short and readable; don't introduce a framework to add one feature.

## What NOT to port

- `sendMessageDraft` — not in Bot API.
- Multi-tenant pairing, allowlist editors, per-group policies — out of scope.
- Media group reassembly — no use case.
- Voice-note output — no audio path.
- ACP (Agent Communication Protocol) layer — clawdbot's agent orchestration; we have the Claude CLI directly.
