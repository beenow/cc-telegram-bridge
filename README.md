# cc-telegram-bridge

A lightweight, self-hosted bridge that connects **Telegram** to **Claude Code** (the CLI), running entirely on your local machine. Send messages from your phone and get Claude's full intelligence — with persistent conversation memory — without any cloud intermediary or API costs beyond your existing Claude subscription.

> Built for personal use. Designed to be open-source.

---

## How It Works

```
Telegram (your phone)  →  cc-telegram-bridge  →  claude CLI  →  response back
```

cc-telegram-bridge is a thin relay. It receives your Telegram message, passes it to the local `claude` CLI with a session ID, and streams the response back. Conversation history is managed natively by Claude Code — no message arrays to maintain, no API key required.

---

## Features

- **Telegram → Claude CLI → Telegram** — full bidirectional conversation via polling (no public webhook required)
- **Persistent memory** — Claude Code remembers your conversation across restarts using its native session system
- **Streaming replies** — Claude's response appears progressively, edited in-place as it generates
- **Animated thinking indicator** — "3 is thinking ." cycles up to 50 dots while waiting for the first response chunk
- **ACK reactions** — bot reacts 👀 on receipt and ✅ when the reply completes, so you can see at a glance which messages were handled
- **Attachments** — send a photo, document, audio file, voice note, or video; the bridge saves it to `downloads/` and tells Claude the path, so Claude's native file tools can read/transcribe/analyze it
- **Inline model picker** — `/model` with no argument shows one-tap buttons for `sonnet` / `opus` / `haiku`
- **Message steering** — send a new message while Claude is responding to instantly cancel and redirect (no queue buildup)
- **Long response splitting** — responses over Telegram's 4096-char limit are automatically split across multiple messages
- **Soul / personality layer** — define your assistant's name, tone, and context in `soul.md` — loaded at startup, no code changes needed
- **Per-chat sessions** — each Telegram chat gets its own Claude session ID stored in local SQLite
- **Multi-model** — switch between `sonnet`, `opus`, and `haiku` per session with `/model`
- **Allowlist** — restrict access to specific Telegram user IDs
- **Mac-native daemon** — runs as a launchd service, auto-starts on login, auto-restarts on crash
- **No API key needed** — uses your existing Claude Code subscription

---

## Requirements

| Dependency | Notes |
|---|---|
| Python 3.11+ | Tested on 3.11, 3.12, 3.14 |
| [Claude Code CLI](https://claude.ai/code) | Must be installed and authenticated (`claude` in PATH) |
| Telegram Bot Token | From [@BotFather](https://t.me/BotFather) |
| macOS 13+ | For launchd daemon support |

---

## Quick Start

```bash
# 1. Clone the repo
git clone https://github.com/beenow/cc-telegram-bridge.git
cd cc-telegram-bridge

# 2. Install dependencies
pip3 install -r requirements.txt

# 3. Configure
cp .env.example .env
# Edit .env — fill in TELEGRAM_BOT_TOKEN and ALLOWED_USER_IDS

# 4. (Optional) Customise your assistant's personality
cp soul.example.md soul.md
# Edit soul.md — set a name, tone, and context for your assistant
# soul.md is gitignored — your personal config stays private

# 5. Run
python3 src/bridge.py

# 6. Install as daemon (macOS, auto-starts on login)
bash scripts/install-daemon.sh
```

---

## Configuration

Copy `.env.example` to `.env` and fill in the required fields. The `.env` file is gitignored — never commit it.

```env
# Required — fill these in
TELEGRAM_BOT_TOKEN=your_bot_token_here
ALLOWED_USER_IDS=123456789        # Comma-separated Telegram user IDs

# Optional
DEFAULT_MODEL=sonnet              # sonnet | opus | haiku
COMMAND_TIMEOUT_SECS=600          # Max seconds for Claude CLI calls
DATA_DIR=./data                   # SQLite database location
LOG_DIR=./logs
DOWNLOADS_DIR=./downloads         # Where inbound attachments are saved (gitignored)
LOG_LEVEL=INFO
```

Find your Telegram user ID by messaging [@userinfobot](https://t.me/userinfobot).

> **Note:** The `.env.example` file in this repo contains placeholder values only. Before running the bridge, copy it to `.env` and replace all values with your own.

---

## Personality — soul.md

The `soul.md` file defines your assistant's identity, tone, and context. It is loaded at startup and injected as a system prompt into every Claude session. It is **gitignored** — your personal config never gets committed.

Copy the template to get started:

```bash
cp soul.example.md soul.md
```

Edit `soul.md` to:
- Give your assistant a name
- Set its personality and communication style
- Add context about yourself (your role, goals, preferences)
- Define things it should never do

Changes take effect on the next daemon restart. No code changes needed.

```bash
# After editing soul.md
launchctl unload ~/Library/LaunchAgents/com.clawd.bridge.plist
launchctl load ~/Library/LaunchAgents/com.clawd.bridge.plist
```

---

## Long Tasks & Message Queue

If Claude is mid-response and you send another message, the current task **keeps running** and the new message is **queued** (one slot per chat — newer messages replace any earlier queued one). The bridge acks the queued message and runs it as soon as the active task finishes. This is intentional: long agentic tasks can run for hours, and a stray follow-up shouldn't kill them.

- `/ping` reports whether a task is still running, how long it's been silent, and whether a message is queued — without disturbing the subprocess.
- `/stop` cancels the active task (and drops anything queued).
- `/new` resets the conversation entirely (cancels + drops queue + clears the Claude session).
- The bot also sends a passive `⏳ Still working...` message every 5 minutes of CLI silence so you know it's alive.

---

## Bot Commands

| Command | Description |
|---|---|
| `/start` | Introduction and help |
| `/new` | Start a fresh conversation (cancels any active task, drops queue, clears session) |
| `/model` | Show one-tap inline keyboard to switch model |
| `/model <name>` | Switch model directly — `sonnet`, `opus`, or `haiku` |
| `/ping` | Check if a long task is still running and whether a message is queued |
| `/stop` | Cancel the current task (and drop any queued message) |
| `/status` | Show current session info (model, message count, session ID) |
| `/help` | Show all commands |

Commands are registered with Telegram via `setMyCommands` on startup, so they show up in the `/` picker in the chat.

---

## Attachments

Send a photo, document, PDF, audio file, voice note, or video alongside (or without) a caption and the bridge will:

1. Download the file to `downloads/` with the pattern `{chat_id}_{timestamp}_{message_id}_{sanitized_filename}`.
2. Prepend a short block to your prompt telling Claude where the file lives on disk:

   ```
   [Attachments available on disk — use your Read/file tools to inspect them:]
     - photo: /abs/path/downloads/12345_1712345678_42_photo_abc123.jpg

   <your caption here>
   ```

3. Claude's normal file tools (Read, Bash, etc.) can then open, transcribe, or analyze the file — no Vision-API plumbing needed; the CLI already knows how to handle files on disk.

`downloads/` is gitignored. No cloud upload is involved — everything stays on your machine.

---

## Project Structure

```
cc-telegram-bridge/
├── src/
│   ├── bridge.py          # Main entry point — Telegram bot + event loop
│   ├── claude.py          # Claude CLI wrapper — streaming, session management, cancel
│   ├── db.py              # SQLite store — session IDs, audit log
│   └── config.py          # Configuration loading from .env + soul.md
├── scripts/
│   ├── install-daemon.sh  # Install launchd plist (macOS)
│   └── uninstall-daemon.sh
├── docs/
│   ├── ARCHITECTURE.md    # System design and message flow
│   └── TOOLS.md           # Claude's built-in tool use
├── soul.example.md        # Personality template — copy to soul.md and fill in
├── soul.md                # Your personal assistant config (gitignored)
├── data/                  # SQLite database (gitignored)
├── logs/                  # Log files (gitignored)
├── downloads/             # Inbound attachments from Telegram (gitignored)
├── .env.example           # Configuration template (fill in and copy to .env)
├── .env                   # Your local config (gitignored — never commit this)
├── requirements.txt       # Python dependencies
└── README.md
```

---

## Running as a Daemon (macOS)

```bash
# Install (auto-starts on login, auto-restarts on crash)
bash scripts/install-daemon.sh

# Check status
launchctl list | grep clawd

# View logs
tail -f logs/bridge.log
tail -f logs/bridge.err

# Restart (e.g. after editing soul.md or .env)
launchctl unload ~/Library/LaunchAgents/com.clawd.bridge.plist
launchctl load ~/Library/LaunchAgents/com.clawd.bridge.plist

# Stop
launchctl unload ~/Library/LaunchAgents/com.clawd.bridge.plist

# Uninstall
bash scripts/uninstall-daemon.sh
```

### What the installer does

`scripts/install-daemon.sh` is a one-shot bootstrap. It:

1. **Validates `.env`** — aborts if missing, since the bridge needs `TELEGRAM_BOT_TOKEN` to start.
2. **Generates `~/Library/LaunchAgents/com.clawd.bridge.plist`** pointing launchd at `src/bridge.py` with the project root as `WorkingDirectory`. The plist is regenerated every run, so re-running the script picks up any change in Python path or project location.
3. **Sets a usable `PATH`** in the plist: `~/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin`. launchd's default `PATH` is minimal and excludes Homebrew, so without this the `claude` CLI (a `#!/usr/bin/env node` shebang) fails silently with `exit 127 — env: node: No such file`.
4. **Pipes stdout/stderr** to `logs/bridge.log` and `logs/bridge.err`.
5. **Sets `RunAtLoad=true`, `KeepAlive=true`, `ThrottleInterval=10`** — the daemon starts at login, restarts on crash, but no faster than once every 10 seconds (so a tight crash loop can't melt the CPU).
6. **Reloads launchd** — unloads any existing instance, then `launchctl load`s the new plist so the daemon is active immediately.

The daemon runs as **your user**, not root. All paths and environment are inherited from the user that ran the installer.

If you need to customise (e.g. point at a non-system Python), set `PYTHON=/path/to/python3 bash scripts/install-daemon.sh`.

---

## Data & Privacy

- Session IDs and an audit log are stored **locally** in `data/bridge.db` (SQLite)
- Conversation history lives in Claude Code's native session storage (not in this project)
- No data is sent anywhere except to Telegram (for messaging) and Anthropic (via Claude CLI for inference)
- Use `/new` to start a fresh conversation and clear the session

---

## Architecture Overview

```
Telegram (your phone)
        │
        │  HTTPS polling
        ▼
┌─────────────────────────────┐
│         bridge.py           │
│   Telegram bot handler      │
│   - Receives messages       │
│   - Steering (cancel+replace│
│     active task on new msg) │
│   - Streams replies back    │
└──────────────┬──────────────┘
               │
       ┌───────┴────────┐
       │                │
       ▼                ▼
┌────────────┐   ┌────────────────────────────┐
│   db.py    │   │        claude.py            │
│  SQLite    │   │  Spawns: claude --resume    │
│  session   │   │  <session_id> "your prompt" │
│  ID store  │   │  Streams JSON, cancel()     │
└────────────┘   └────────────────────────────┘
                          │
                          ▼
               claude CLI (local process)
               - Maintains full conversation history
               - Has access to Claude's built-in tools
               - Uses your Claude Code subscription
```

See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for the full design document.

---

## Roadmap

- [x] Image understanding (send photo → Claude reads it from disk)
- [x] Document attachments (PDF / text / audio / video via Claude's file tools)
- [x] One-tap `/model` picker
- [x] `/stop` to cancel the current task without sending a new prompt
- [x] Queue-by-default for messages sent during a long task (one-slot, newer replaces)
- [ ] Voice message transcription (right now voice notes are passed through as `.ogg` files — Claude can inspect but not natively transcribe without a tool)
- [ ] Scheduled messages / reminders via Telegram
- [ ] Docker support

---

## Contributing

Contributions are welcome. Please open an issue first to discuss what you'd like to change.

```bash
# Development setup
pip3 install -r requirements.txt
cp .env.example .env
# Fill in .env
python3 src/bridge.py
```

---

## License

MIT License — see [LICENSE](LICENSE).
