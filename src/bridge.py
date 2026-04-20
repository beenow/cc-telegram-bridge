"""
bridge.py — Main Telegram bot process

Run with:
    python3 src/bridge.py
"""

import asyncio
import logging
import re
import sys
import time
from pathlib import Path

from telegram import (
    BotCommand,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReactionTypeEmoji,
    Update,
    constants,
)
from telegram.error import BadRequest, RetryAfter
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

sys.path.insert(0, str(Path(__file__).parent))

from config import load_config, Config
from db import Database
from claude import ClaudeClient, StreamChunk

log = logging.getLogger(__name__)

# Per-chat active tasks — new message cancels the previous one (steering)
_active_tasks: dict[int, asyncio.Task] = {}

# Per-chat liveness snapshot for /ping and the heartbeat editor.
# started_at: time.monotonic() when the Claude subprocess launched
# last_chunk_at: time.monotonic() of the most recent stdout chunk
# bytes_streamed: total length of text streamed so far (rough progress signal)
_chat_liveness: dict[int, dict[str, float]] = {}

# How long to wait between heartbeat edits when the CLI is silent.
HEARTBEAT_INTERVAL_SECS = 300  # 5 minutes

# ACK reactions: 👀 on receipt, ✅ on success. Silently no-op on failure
# (Bot API 7.0+ required; older accounts or group-permission issues shouldn't
# break the reply path).
ACK_RECEIVED = "👀"
ACK_DONE = "✅"


async def _set_reaction(ctx: ContextTypes.DEFAULT_TYPE, chat_id: int, message_id: int, emoji: str | None):
    try:
        reaction = [ReactionTypeEmoji(emoji=emoji)] if emoji else []
        await ctx.bot.set_message_reaction(
            chat_id=chat_id, message_id=message_id, reaction=reaction
        )
    except Exception as e:
        log.debug(f"set_message_reaction failed: {e}")


_FILENAME_SAFE = re.compile(r"[^A-Za-z0-9._-]+")


def _sanitize_filename(name: str) -> str:
    """Strip any path components and unsafe chars from a user-supplied filename."""
    name = Path(name).name  # drop directories
    name = _FILENAME_SAFE.sub("_", name).strip("._")
    return name or "file"


async def _download_attachments(
    update: Update,
    ctx: ContextTypes.DEFAULT_TYPE,
    downloads_dir: Path,
) -> list[tuple[Path, str]]:
    """
    Download any media on the incoming message to `downloads_dir` and return
    [(absolute_path, kind), ...]. Claude CLI doesn't accept file paths in
    --print mode, so we inject the paths into the prompt text instead.
    """
    msg = update.message
    if msg is None:
        return []

    downloads_dir.mkdir(parents=True, exist_ok=True)
    chat_id = update.effective_chat.id
    ts = int(time.time())
    prefix = f"{chat_id}_{ts}_{msg.message_id}"

    results: list[tuple[Path, str]] = []

    async def _save(file_obj, filename: str, kind: str):
        dest = downloads_dir / f"{prefix}_{_sanitize_filename(filename)}"
        try:
            tg_file = await file_obj.get_file()
            await tg_file.download_to_drive(custom_path=str(dest))
            results.append((dest.resolve(), kind))
        except Exception as e:
            log.warning(f"Failed to download {kind} {filename}: {e}")

    if msg.photo:
        # Highest-resolution PhotoSize is last in the list.
        photo = msg.photo[-1]
        await _save(photo, f"photo_{photo.file_unique_id}.jpg", "photo")
    if msg.document:
        await _save(msg.document, msg.document.file_name or "document.bin", "document")
    if msg.video:
        await _save(msg.video, msg.video.file_name or "video.mp4", "video")
    if msg.audio:
        await _save(msg.audio, msg.audio.file_name or "audio.mp3", "audio")
    if msg.voice:
        await _save(msg.voice, f"voice_{msg.voice.file_unique_id}.ogg", "voice")
    if msg.video_note:
        await _save(msg.video_note, f"video_note_{msg.video_note.file_unique_id}.mp4", "video_note")

    return results


def _build_prompt_with_attachments(
    caption: str,
    attachments: list[tuple[Path, str]],
) -> str:
    """
    Prepend a block that tells Claude where to find the attached files.
    The CLI can Read/open these paths via its normal tools.
    """
    if not attachments:
        return caption
    lines = ["[Attachments available on disk — use your Read/file tools to inspect them:]"]
    for path, kind in attachments:
        lines.append(f"  - {kind}: {path}")
    lines.append("")
    lines.append(caption if caption else "(no accompanying text — please examine the attachment above)")
    return "\n".join(lines)


async def _cancel_active(chat_id: int):
    """Cancel the active task for this chat and wait for it to finish."""
    task = _active_tasks.pop(chat_id, None)
    if task and not task.done():
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass


def _is_allowed(update: Update, cfg: Config) -> bool:
    user = update.effective_user
    return user is not None and user.id in cfg.allowed_user_ids


# ── Command handlers ─────────────────────────────────────────────────────────

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    cfg: Config = ctx.bot_data["config"]
    if not _is_allowed(update, cfg):
        return
    text = (
        "Hello! I'm your local Claude assistant.\n\n"
        "Commands:\n"
        "  /new — start a fresh conversation\n"
        "  /model [name] — switch Claude model\n"
        "  /ping — check if a long task is still running\n"
        "  /status — show current session info\n"
        "  /help — show this message"
    )
    await update.message.reply_text(text)


async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await cmd_start(update, ctx)


async def cmd_new(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    cfg: Config = ctx.bot_data["config"]
    db: Database = ctx.bot_data["db"]
    if not _is_allowed(update, cfg):
        return
    chat_id = update.effective_chat.id
    db.reset_session(chat_id)
    await update.message.reply_text("Conversation reset. Starting fresh.")


def _fmt_duration(secs: float) -> str:
    secs = int(secs)
    if secs < 60:
        return f"{secs}s"
    if secs < 3600:
        return f"{secs // 60}m {secs % 60}s"
    hours = secs // 3600
    mins = (secs % 3600) // 60
    return f"{hours}h {mins}m"


async def cmd_ping(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Report liveness of the current Claude task without disturbing it."""
    cfg: Config = ctx.bot_data["config"]
    if not _is_allowed(update, cfg):
        return
    chat_id = update.effective_chat.id
    live = _chat_liveness.get(chat_id)
    task = _active_tasks.get(chat_id)
    if not live or not task or task.done():
        await update.message.reply_text("No active task. Send a message to start one.")
        return
    now = time.monotonic()
    elapsed = _fmt_duration(now - live["started_at"])
    silence = _fmt_duration(now - live["last_chunk_at"])
    bytes_streamed = int(live.get("bytes_streamed", 0))
    await update.message.reply_text(
        f"⏳ Task still running\n"
        f"Elapsed: {elapsed}\n"
        f"Last output: {silence} ago\n"
        f"Streamed: {bytes_streamed} chars\n\n"
        f"Send any message to cancel + redirect, or /new to reset."
    )


async def cmd_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    cfg: Config = ctx.bot_data["config"]
    db: Database = ctx.bot_data["db"]
    if not _is_allowed(update, cfg):
        return
    chat_id = update.effective_chat.id
    session = db.get_session(chat_id, cfg.default_model, False)
    has_session = session["claude_session_id"] is not None
    session_str = f"`{session['claude_session_id'][:8]}...`" if has_session else "none (new)"
    text = (
        f"Model: `{session['model']}`\n"
        f"Messages sent: `{session['message_count']}`\n"
        f"Claude session: {session_str}"
    )
    await update.message.reply_text(text, parse_mode=constants.ParseMode.MARKDOWN)


VALID_MODELS = ["sonnet", "opus", "haiku"]


def _model_keyboard(current: str) -> InlineKeyboardMarkup:
    """One-tap model picker. Current model gets a ● marker."""
    row = [
        InlineKeyboardButton(
            ("● " if m == current else "") + m,
            callback_data=f"model:{m}",
        )
        for m in VALID_MODELS
    ]
    return InlineKeyboardMarkup([row])


async def cmd_model(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    cfg: Config = ctx.bot_data["config"]
    db: Database = ctx.bot_data["db"]
    if not _is_allowed(update, cfg):
        return
    chat_id = update.effective_chat.id
    args = ctx.args

    session = db.get_session(chat_id, cfg.default_model, False)

    if not args:
        await update.message.reply_text(
            f"Current model: `{session['model']}`\n\nTap to switch:",
            parse_mode=constants.ParseMode.MARKDOWN,
            reply_markup=_model_keyboard(session["model"]),
        )
        return

    model = args[0].strip().lower()
    if model not in VALID_MODELS:
        await update.message.reply_text(
            f"Unknown model: `{model}`\n\nAvailable: " + ", ".join(f"`{m}`" for m in VALID_MODELS),
            parse_mode=constants.ParseMode.MARKDOWN,
        )
        return

    db.set_model(chat_id, model)
    await update.message.reply_text(f"Model switched to `{model}`", parse_mode=constants.ParseMode.MARKDOWN)


async def cb_model(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    cfg: Config = ctx.bot_data["config"]
    db: Database = ctx.bot_data["db"]
    query = update.callback_query
    if query is None:
        return
    if not _is_allowed(update, cfg):
        await query.answer("Not authorized.", show_alert=False)
        return

    data = query.data or ""
    if not data.startswith("model:"):
        await query.answer()
        return
    model = data.split(":", 1)[1].strip().lower()
    if model not in VALID_MODELS:
        await query.answer(f"Unknown model: {model}", show_alert=False)
        return

    chat_id = update.effective_chat.id
    db.set_model(chat_id, model)
    await query.answer(f"Model: {model}")
    try:
        await query.edit_message_text(
            f"Model switched to `{model}`",
            parse_mode=constants.ParseMode.MARKDOWN,
            reply_markup=_model_keyboard(model),
        )
    except BadRequest:
        pass  # message_not_modified or too old — answer() already confirmed


# ── Message handler ──────────────────────────────────────────────────────────

EDIT_INTERVAL_CHARS = 120
EDIT_MIN_SECS = 0.8
TG_MAX_LEN = 4096  # Telegram hard limit per message


def _split_text(text: str, limit: int = TG_MAX_LEN) -> list[str]:
    """Split text into chunks ≤ limit, preferring newline boundaries."""
    if len(text) <= limit:
        return [text]
    chunks = []
    while text:
        if len(text) <= limit:
            chunks.append(text)
            break
        # Find the last newline within the limit
        cut = text.rfind("\n", 0, limit)
        if cut <= 0:
            cut = limit  # no newline — hard cut
        chunks.append(text[:cut])
        text = text[cut:].lstrip("\n")
    return chunks


async def on_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    cfg: Config = ctx.bot_data["config"]
    if not _is_allowed(update, cfg):
        log.warning(f"Rejected message from user {update.effective_user.id}")
        return

    msg = update.message
    if msg is None:
        return

    # Text body is either .text (pure text) or .caption (media with caption).
    text = (msg.text or msg.caption or "").strip()

    chat_id = update.effective_chat.id
    user_msg_id = msg.message_id

    # Cancel any in-progress response for this chat (steering)
    await _cancel_active(chat_id)

    # ACK: eyeballs on the user's message while we work on it.
    await _set_reaction(ctx, chat_id, user_msg_id, ACK_RECEIVED)

    # Download any attachments; inject their paths into the prompt so the
    # Claude CLI (which can't take attachments in --print mode) can Read them.
    attachments = await _download_attachments(update, ctx, Path(cfg.downloads_dir))
    if not text and not attachments:
        return  # nothing to send

    prompt = _build_prompt_with_attachments(text, attachments)

    task = asyncio.create_task(_handle_message(update, ctx, chat_id, prompt))
    _active_tasks[chat_id] = task
    try:
        await task
    except asyncio.CancelledError:
        pass  # steering — new message took over
    finally:
        _active_tasks.pop(chat_id, None)
        _chat_liveness.pop(chat_id, None)


async def _handle_message(
    update: Update,
    ctx: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    user_text: str,
):
    cfg: Config = ctx.bot_data["config"]
    db: Database = ctx.bot_data["db"]
    claude: ClaudeClient = ctx.bot_data["claude"]
    user_msg_id = update.message.message_id

    await ctx.bot.send_chat_action(chat_id=chat_id, action=constants.ChatAction.TYPING)

    # Load or create session
    session = db.get_session(chat_id, cfg.default_model, False)

    # Determine if this is a new Claude session
    is_new = session["claude_session_id"] is None
    if is_new:
        session_id = claude.new_session_id()
        db.set_claude_session_id(chat_id, session_id)
    else:
        session_id = session["claude_session_id"]

    # Log user message
    db.log_exchange(chat_id, "user", user_text, session["model"])
    db.increment_message_count(chat_id)

    # Send placeholder
    reply_msg = await update.message.reply_text("3 is thinking .")
    reply_id = reply_msg.message_id

    accumulated = ""
    last_edit_len = 0
    last_edit_time = time.monotonic()
    first_chunk_received = False
    task_started = time.monotonic()
    last_chunk_time = task_started
    _chat_liveness[chat_id] = {
        "started_at": task_started,
        "last_chunk_at": task_started,
        "bytes_streamed": 0,
    }

    # Animated thinking indicator — cycles until first real text arrives
    _THINKING_FRAMES = [f"3 is thinking {'.' * i}" for i in range(1, 51)]
    _thinking_frame = 0

    async def _animate_thinking():
        nonlocal _thinking_frame
        while not first_chunk_received:
            await asyncio.sleep(0.6)
            if first_chunk_received:
                break
            frame = _THINKING_FRAMES[_thinking_frame % len(_THINKING_FRAMES)]
            _thinking_frame += 1
            try:
                await ctx.bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=reply_id,
                    text=frame,
                )
            except Exception:
                pass

    async def _heartbeat():
        """
        After first byte, send a passive status message every
        HEARTBEAT_INTERVAL_SECS while the CLI is silent. Unlike the thinking
        animation, this does not overwrite real output — it sends a NEW
        message so the streaming placeholder keeps its accumulated text.
        """
        while True:
            try:
                await asyncio.sleep(HEARTBEAT_INTERVAL_SECS)
            except asyncio.CancelledError:
                return
            now = time.monotonic()
            silence = now - last_chunk_time
            if silence < HEARTBEAT_INTERVAL_SECS * 0.9:
                continue  # recent output — no need to heartbeat
            elapsed = _fmt_duration(now - task_started)
            silence_s = _fmt_duration(silence)
            try:
                await ctx.bot.send_message(
                    chat_id=chat_id,
                    text=(
                        f"⏳ Still working... (running {elapsed}, last output {silence_s} ago)\n"
                        f"Send /ping anytime to check, or any message to redirect."
                    ),
                )
            except Exception as e:
                log.debug(f"heartbeat send failed: {e}")

    async def _edit(force: bool = False):
        nonlocal last_edit_len, last_edit_time
        now = time.monotonic()
        if not force:
            if len(accumulated) - last_edit_len < EDIT_INTERVAL_CHARS:
                return
            if now - last_edit_time < EDIT_MIN_SECS:
                return
        # Show the last TG_MAX_LEN chars during streaming (tail the live message)
        display = accumulated[-TG_MAX_LEN:] if len(accumulated) > TG_MAX_LEN else accumulated
        text = display or "…"
        try:
            await ctx.bot.edit_message_text(
                chat_id=chat_id,
                message_id=reply_id,
                text=text,
                parse_mode=constants.ParseMode.MARKDOWN,
            )
            last_edit_len = len(accumulated)
            last_edit_time = now
        except BadRequest as e:
            msg = str(e).lower()
            if "message is not modified" in msg:
                return
            # Malformed markdown from the model (unclosed backticks, stray *,
            # etc.) — retry as plain text rather than freezing the stream.
            if "can't parse entities" in msg or "parse" in msg:
                try:
                    await ctx.bot.edit_message_text(
                        chat_id=chat_id,
                        message_id=reply_id,
                        text=text,
                    )
                    last_edit_len = len(accumulated)
                    last_edit_time = now
                except Exception as e2:
                    log.debug(f"Plain-text retry also failed: {e2}")
            else:
                log.debug(f"Edit failed (will retry): {e}")
        except Exception as e:
            log.debug(f"Edit failed (will retry): {e}")

    thinking_task = asyncio.create_task(_animate_thinking())
    heartbeat_task = asyncio.create_task(_heartbeat())

    async def _stream_with_session_recovery():
        """
        Stream from Claude, auto-recovering if the session no longer exists.
        On 'No conversation found' error, resets the DB session and retries once
        with a fresh session ID — transparent to the user.
        """
        nonlocal session_id, is_new

        async def _do_stream():
            async for chunk in claude.stream(user_text, session_id, is_new):
                yield chunk

        first_error = None
        async for chunk in _do_stream():
            if chunk.error and "no conversation found" in chunk.error.lower() and not is_new:
                # Session missing from Claude's local store — reset and retry once.
                log.warning(f"Session {session_id} not found, resetting and retrying...")
                db.reset_session(chat_id)
                session_id = claude.new_session_id()
                db.set_claude_session_id(chat_id, session_id)
                is_new = True
                async for retry_chunk in claude.stream(user_text, session_id, is_new):
                    yield retry_chunk
                return
            yield chunk

    try:
        async for chunk in _stream_with_session_recovery():
            if chunk.error:
                # Stop the animator before displaying the error — otherwise its
                # next tick overwrites our error message with a thinking frame.
                first_chunk_received = True
                thinking_task.cancel()
                heartbeat_task.cancel()
                await ctx.bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=reply_id,
                    text=f"Error: {chunk.error}",
                )
                await _set_reaction(ctx, chat_id, user_msg_id, None)
                return

            if chunk.text:
                if not first_chunk_received:
                    first_chunk_received = True
                    thinking_task.cancel()
                accumulated += chunk.text
                last_chunk_time = time.monotonic()
                live = _chat_liveness.get(chat_id)
                if live is not None:
                    live["last_chunk_at"] = last_chunk_time
                    live["bytes_streamed"] = len(accumulated)
                await _edit()

            if chunk.done:
                break

    except asyncio.CancelledError:
        first_chunk_received = True
        thinking_task.cancel()
        heartbeat_task.cancel()
        # Steered away — mark the placeholder as interrupted and propagate.
        # Don't touch the reaction here: the next message's on_message handler
        # will set a fresh 👀 on its own user_msg, and the old one staying as
        # 👀 is a fair signal that *that* message was interrupted.
        stub = (accumulated[:200] + "…\n\n_[interrupted]_") if accumulated else "_[interrupted]_"
        try:
            await ctx.bot.edit_message_text(
                chat_id=chat_id,
                message_id=reply_id,
                text=stub,
                parse_mode=constants.ParseMode.MARKDOWN,
            )
        except Exception:
            pass
        raise

    except Exception as e:
        first_chunk_received = True
        thinking_task.cancel()
        heartbeat_task.cancel()
        log.exception("Streaming error")
        await ctx.bot.edit_message_text(
            chat_id=chat_id,
            message_id=reply_id,
            text=f"Unexpected error: {e}",
        )
        await _set_reaction(ctx, chat_id, user_msg_id, None)
        return

    thinking_task.cancel()
    heartbeat_task.cancel()

    # Final send — split into chunks if response exceeds Telegram's 4096-char limit
    if accumulated:
        parts = _split_text(accumulated)
        for i, part in enumerate(parts):
            try:
                if i == 0:
                    await ctx.bot.edit_message_text(
                        chat_id=chat_id,
                        message_id=reply_id,
                        text=part,
                        parse_mode=constants.ParseMode.MARKDOWN,
                    )
                else:
                    await ctx.bot.send_message(
                        chat_id=chat_id,
                        text=part,
                        parse_mode=constants.ParseMode.MARKDOWN,
                    )
            except Exception:
                # Retry without markdown if parse error
                try:
                    if i == 0:
                        await ctx.bot.edit_message_text(
                            chat_id=chat_id,
                            message_id=reply_id,
                            text=part,
                        )
                    else:
                        await ctx.bot.send_message(chat_id=chat_id, text=part)
                except Exception:
                    pass

    # Log assistant response
    if accumulated:
        db.log_exchange(chat_id, "assistant", accumulated, session["model"])

    # Swap eyeballs → check mark so the user's message shows "done" at a glance.
    await _set_reaction(ctx, chat_id, user_msg_id, ACK_DONE)


# ── Startup ──────────────────────────────────────────────────────────────────

def setup_logging(log_dir: str, log_level: str):
    Path(log_dir).mkdir(parents=True, exist_ok=True)
    level = getattr(logging, log_level.upper(), logging.INFO)
    # Only a FileHandler: launchd already redirects stdout to bridge.log via
    # StandardOutPath, so adding a StreamHandler(stdout) would write every
    # line twice.
    handlers = [logging.FileHandler(str(Path(log_dir) / "bridge.log"))]
    # force=True removes any handlers already on the root logger (prevents
    # duplicate log lines when the process is restarted by launchd).
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=handlers,
        force=True,
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("telegram").setLevel(logging.DEBUG)


def main():
    cfg = load_config()
    setup_logging(cfg.log_dir, cfg.log_level)

    log.info("clawd-bridge starting")
    log.info(f"Allowed users: {cfg.allowed_user_ids}")
    log.info(f"Default model: {cfg.default_model}")

    db = Database(cfg.data_dir)
    claude = ClaudeClient(
        system_prompt=cfg.system_prompt,
        model=cfg.default_model,
        timeout_secs=cfg.command_timeout_secs,
    )

    app = Application.builder().token(cfg.telegram_bot_token).build()
    app.bot_data["config"] = cfg
    app.bot_data["db"] = db
    app.bot_data["claude"] = claude

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("new", cmd_new))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("ping", cmd_ping))
    app.add_handler(CommandHandler("model", cmd_model))
    app.add_handler(CallbackQueryHandler(cb_model, pattern=r"^model:"))
    # Accept plain text and any captioned/non-captioned media. Commands are
    # excluded so `/new` etc. still dispatch to their CommandHandlers.
    media_filter = (
        filters.TEXT
        | filters.PHOTO
        | filters.Document.ALL
        | filters.VIDEO
        | filters.AUDIO
        | filters.VOICE
        | filters.VIDEO_NOTE
    )
    app.add_handler(MessageHandler(media_filter & ~filters.COMMAND, on_message))

    async def _post_init(app: Application) -> None:
        # Register commands so they appear in Telegram's `/` picker.
        # Runs once after the bot is authenticated; non-fatal on failure.
        try:
            await app.bot.set_my_commands([
                BotCommand("new", "Start a fresh conversation"),
                BotCommand("model", "View or switch Claude model"),
                BotCommand("ping", "Check if a long task is still running"),
                BotCommand("status", "Show current session info"),
                BotCommand("help", "Show available commands"),
            ])
            log.info("Registered bot commands with Telegram")
        except Exception as e:
            log.warning(f"set_my_commands failed (non-fatal): {e}")

    app.post_init = _post_init

    async def _error_handler(update: object, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        """Log errors; on RetryAfter sleep the required time instead of crashing."""
        err = ctx.error
        if isinstance(err, RetryAfter):
            log.warning(f"Telegram flood control: sleeping {err.retry_after}s")
            await asyncio.sleep(err.retry_after)
            return
        log.exception("Unhandled telegram error", exc_info=err)

    app.add_error_handler(_error_handler)

    log.info("Starting Telegram polling...")
    app.run_polling(drop_pending_updates=True)

    db.close()
    log.info("clawd-bridge stopped.")


if __name__ == "__main__":
    main()
