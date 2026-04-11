# Architecture — cc-telegram-bridge

> Last updated: 2026-04-11

---

## 1. Overview

`cc-telegram-bridge` is a single-process Python service that creates a bidirectional messaging bridge between Telegram and the local `claude` CLI (Claude Code). It is designed to run permanently on a local machine (Mac Mini or similar always-on device) as a user-space daemon.

**Key design principle:** cc-telegram-bridge is a thin relay, not an AI framework. Conversation history, memory, and tool use are all delegated entirely to the Claude Code CLI — this project only handles the Telegram transport layer, session ID bookkeeping, and UX concerns (streaming, steering, personality).

No Anthropic API key is required. The bridge uses your existing Claude Code subscription.

---

## 2. System Context

```
┌─────────────────────────────────────────────────────────────────┐
│                         External World                          │
│                                                                 │
│   ┌──────────────┐                                              │
│   │   Telegram   │                                              │
│   │  (Cloud)     │                                              │
│   └──────┬───────┘                                              │
│          │ HTTPS                                                │
└──────────┼──────────────────────────────────────────────────────┘
           │
           ▼
┌───────────────────────────────────────────────────────────────┐
│                    Mac Mini (local machine)                    │
│                                                               │
│  ┌─────────────────────────────────────────────────────────┐  │
│  │               cc-telegram-bridge service                │  │
│  │                                                         │  │
│  │   bridge.py ◄──── db.py (SQLite — session IDs only)    │  │
│  │       │                                                  │  │
│  │   claude.py ──► claude CLI subprocess                   │  │
│  │                  (--session-id / --resume)               │  │
│  │                                                         │  │
│  │   soul.md ──► system prompt (loaded at startup)         │  │
│  └─────────────────────────────────────────────────────────┘  │
│                                                               │
│  data/bridge.db    logs/bridge.log                            │
│                                                               │
│  ~/.claude/projects/...  (Claude Code's own session store)    │
└───────────────────────────────────────────────────────────────┘
```

The service has **one outbound connection** — to Telegram (polling). Claude is invoked as a local subprocess, not via HTTP.

---

## 3. Component Design

### 3.1 `bridge.py` — Main Process

The top-level event loop and Telegram bot handler. Responsibilities:

- Initialize all components on startup (DB, config, Claude client)
- Register Telegram command and message handlers
- Dispatch incoming messages to the Claude client
- Stream Claude's response back to the user via progressive message edits
- Show animated thinking indicator while waiting for the first chunk
- Implement message steering (cancel active task on new message)
- Split responses that exceed Telegram's 4096-char limit
- Handle errors gracefully

**Concurrency model:** Single-threaded async (`asyncio`). Per-chat `asyncio.Task` tracking enables steering — a new message cancels the previous task rather than queuing behind it.

**Lifecycle:**
```
startup
  └─ load config from .env + soul.md
  └─ initialize DB
  └─ initialize ClaudeClient
  └─ start Telegram polling
       └─ on message:
            → cancel active task for this chat (steering)
            → create new task → _handle_message()
                 → show animated thinking indicator
                 → call claude.py → stream reply
                 → stop indicator on first chunk
                 → split and send full response
  └─ run until SIGINT/SIGTERM
shutdown
  └─ close DB
  └─ stop polling
```

---

### 3.2 `db.py` — Session Store

Manages all persistent state in a single SQLite file at `data/bridge.db`.

**Schema:**

```sql
-- One row per Telegram chat
CREATE TABLE sessions (
    chat_id           INTEGER PRIMARY KEY,
    model             TEXT    NOT NULL DEFAULT 'sonnet',
    tools_enabled     INTEGER NOT NULL DEFAULT 0,
    claude_session_id TEXT,              -- UUID for claude --session-id / --resume
    message_count     INTEGER NOT NULL DEFAULT 0,
    created_at        TEXT    NOT NULL,
    updated_at        TEXT    NOT NULL
);

-- Immutable audit log
CREATE TABLE exchanges (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id     INTEGER NOT NULL,
    role        TEXT    NOT NULL,   -- 'user' | 'assistant'
    content     TEXT    NOT NULL,
    model       TEXT,
    created_at  TEXT    NOT NULL
);

CREATE INDEX idx_exchanges_chat ON exchanges(chat_id, created_at);
```

**Key operations:**
- `get_session(chat_id)` — load or create session row
- `set_claude_session_id(chat_id, uuid)` — persist session UUID after first message
- `increment_message_count(chat_id)` — track usage
- `set_model(chat_id, model)` — update preferred model
- `reset_session(chat_id)` — clear `claude_session_id` (next message starts fresh)
- `log_exchange(...)` — append to audit log

Note: conversation history itself is **not stored here**. It lives in Claude Code's native session files at `~/.claude/projects/...`.

---

### 3.3 `claude.py` — Claude CLI Wrapper

Wraps the `claude` CLI and handles streaming output. Responsibilities:

- Build the subprocess command with the correct session flags
- Parse `stream-json` output line by line
- Yield text chunks to the caller for progressive Telegram edits
- Expose `cancel()` to kill the subprocess from outside (used by steering)
- Handle errors (timeout, CLI not found, error result)

**Session management:**

```
First message in a chat:
  → generate new UUID
  → store in DB
  → claude --session-id <uuid> --print --output-format stream-json "prompt"

Subsequent messages:
  → load UUID from DB
  → claude --resume <uuid> --print --output-format stream-json "prompt"

/new command:
  → clear UUID from DB
  → next message starts a fresh session
```

**Streaming flow:**

```
claude.stream(prompt, session_id, is_new)
  └─ spawn subprocess: claude --resume <id> --print --output-format stream-json --verbose "prompt"
  └─ store proc handle in self._proc (for cancel())
  └─ for each stdout line:
       parse JSON event
       if type == "assistant" → yield StreamChunk(text=...)
       if type == "result" and is_error → yield StreamChunk(error=...)
       if type == "result" and success → yield StreamChunk(done=True)
  └─ on CancelledError → call self.cancel() → kill subprocess → re-raise
  └─ finally → clear self._proc
```

**Stream-JSON event types used:**

| Event type | Action |
|---|---|
| `system` (subtype: `init`) | Log session_id for debugging |
| `assistant` | Extract text from `message.content[].text`, yield to caller |
| `result` (subtype: `success`) | Signal done |
| `result` (subtype: `error_*`) | Yield error chunk |
| All others | Ignored |

---

### 3.4 `config.py` — Configuration

Loads and validates all configuration from `.env` at startup. Also loads `soul.md` from the project root and prepends it to the system prompt. Fails fast with a clear error message if required keys are missing.

**Required:**
- `TELEGRAM_BOT_TOKEN`
- `ALLOWED_USER_IDS`

**Optional with defaults:**

| Key | Default | Description |
|---|---|---|
| `DEFAULT_MODEL` | `sonnet` | Default Claude model alias |
| `COMMAND_TIMEOUT_SECS` | `120` | Max seconds for a Claude CLI call |
| `DATA_DIR` | `./data` | SQLite database directory |
| `LOG_DIR` | `./logs` | Log file directory |
| `LOG_LEVEL` | `INFO` | Logging verbosity |

**System prompt construction:**
```
soul.md content
  +
[optional runtime context]
  =
--append-system-prompt passed to claude CLI
```

---

## 4. Message Flow — End to End

```
[1] User sends message on Telegram mobile app
        │
[2] bridge.py polling loop receives update (getUpdates)
        │
        │  Check: is sender in ALLOWED_USER_IDS?
        │  No  → silently ignore
        │  Yes → continue
        │
[3] Cancel any active task for this chat (steering)
    → active subprocess killed via claude.cancel()
    → interrupted message placeholder updated to "[interrupted]"
        │
[4] Create new asyncio.Task for this message, register in _active_tasks
        │
[5] Send "typing..." action to Telegram
        │
[6] db.get_session(chat_id)
    → load existing session row (or create new one)
        │
[7] Determine Claude session:
    → claude_session_id is NULL? generate new UUID, store it (is_new=True)
    → claude_session_id exists?  use it (is_new=False)
        │
[8] db.log_exchange(chat_id, "user", text)
    db.increment_message_count(chat_id)
        │
[9] Send initial Telegram placeholder message ("3 is thinking .")
    Start animated thinking indicator (cycles . through 50 dots, then loops)
        │
[10] claude.stream(prompt, session_id, is_new)
        │
        │  Subprocess: claude --session-id|--resume <uuid>
        │              --print --output-format stream-json --verbose
        │              --model <model> --dangerously-skip-permissions
        │              --append-system-prompt <soul.md content>
        │              "<prompt>"
        │
        │  Claude CLI handles:
        │    - Full conversation history (via session file)
        │    - Memory (auto-memory system)
        │    - Tool use (Bash, Read, Write, etc.)
        │    - Multi-turn tool loops
        │
[11] First chunk received → stop thinking animation → begin streaming into placeholder
     Subsequent chunks → edit Telegram message with accumulated text
     (throttled: max every 0.8s or 120 chars)
        │
[12] "result" event received → final edit with complete text
     If response > 4096 chars → split on newline boundaries → send as sequential messages
        │
[13] db.log_exchange(chat_id, "assistant", full_response)
        │
[14] Task completes → removed from _active_tasks
```

---

## 5. Concurrency Model — Steering

```
Main asyncio event loop
│
├── Telegram polling task
│     └── on update → on_message()
│                        └─ _cancel_active(chat_id)   ← kills previous task
│                             └─ create_task(_handle_message)
│                             └─ _active_tasks[chat_id] = task
│
└── _active_tasks: dict[int, asyncio.Task]
      └── chat_id → currently running Task
      └── cancelled when a new message arrives for the same chat
```

**Steering flow:**
1. New message arrives for chat 123
2. `_cancel_active(123)` — cancels the running Task, awaits it to finish cleanup
3. The cancelled Task catches `CancelledError`, kills the subprocess, updates the Telegram message to `[interrupted]`, re-raises
4. New Task starts immediately — no wait, no queue

---

## 6. Animated Thinking Indicator

While waiting for the first response chunk from Claude CLI, a background coroutine cycles through frames:

```
3 is thinking .
3 is thinking ..
3 is thinking ...
  ...
3 is thinking ..................................................  (50 dots)
3 is thinking .  (loops)
```

Frame interval: 0.6 seconds. The same Telegram message is edited in-place — no new messages are sent. When the first real text chunk arrives, the thinking task is cancelled and the message switches to streaming content.

---

## 7. Long Response Handling

Telegram enforces a hard 4096-character limit per message. The `_split_text()` function splits on newline boundaries (falling back to a hard cut if no newline is found within the limit). The first chunk edits the original placeholder; subsequent chunks are sent as new messages.

During streaming, if `accumulated` exceeds 4096 chars, the live edit shows the last 4096 characters (tail) so the message stays within limit while Claude is still writing.

---

## 8. Soul / Personality Layer

`soul.md` at the project root defines the assistant's identity. It is loaded once at startup by `config.py` and passed to every Claude CLI invocation via `--append-system-prompt`. Editing `soul.md` and restarting the daemon changes the assistant's behaviour without touching any code.

---

## 9. Daemon Management (macOS)

The service runs as a **launchd user agent** — starts when the user logs in, restarts automatically on crash.

**Plist location:** `~/Library/LaunchAgents/com.clawd.bridge.plist`

**Key launchd settings:**
- `KeepAlive: true` — auto-restart on exit
- `RunAtLoad: true` — start on login
- `ThrottleInterval: 10` — minimum 10s between restarts
- `StandardOutPath` / `StandardErrorPath` → `logs/bridge.log` / `logs/bridge.err`

---

## 10. Security Considerations

| Concern | Mitigation |
|---|---|
| Unauthorized Telegram access | `ALLOWED_USER_IDS` allowlist checked on every message |
| Secrets exposure | `.env` is gitignored; no API key used or stored |
| Claude tool use scope | `--dangerously-skip-permissions` required for non-interactive use; Claude still respects its own safety guidelines |
| Data leakage | All data local; no telemetry; conversation history stays in Claude Code's session store |
| SQLite file access | Readable only by the running user (file permissions) |

---

## 11. File Layout

```
cc-telegram-bridge/
├── src/
│   ├── bridge.py       # Telegram bot + main loop + steering + streaming
│   ├── claude.py       # Claude CLI subprocess wrapper + streaming + cancel()
│   ├── db.py           # SQLite session ID store + audit log
│   └── config.py       # .env loading + soul.md loading + validation
├── scripts/
│   ├── install-daemon.sh    # Write plist + launchctl load
│   └── uninstall-daemon.sh  # launchctl unload + remove plist
├── docs/
│   ├── ARCHITECTURE.md      # This document
│   └── TOOLS.md             # Claude's built-in tool capabilities
├── soul.md             # Assistant personality + system prompt (edit freely)
├── data/               # SQLite database (gitignored)
├── logs/               # Log output (gitignored)
├── .env.example        # Config template
├── .env                # Local config (gitignored — never commit)
├── requirements.txt    # Python dependencies
├── .gitignore
└── README.md
```

---

## 12. Dependencies

```
python-telegram-bot>=21.0    # Async Telegram bot framework
python-dotenv>=1.0.0         # .env file loading
```

SQLite is part of Python's standard library (`sqlite3`). `asyncio` and `subprocess` are standard library. The only runtime dependency beyond Python itself is `python-telegram-bot`.

Claude Code CLI (`claude`) must be installed separately and authenticated. It is not a Python package — it is a local binary at `~/.local/bin/claude`.

---

## 13. Future Extensions

| Extension | Where it plugs in |
|---|---|
| Voice messages | `bridge.py` — download OGG, transcribe with Whisper, pass text to Claude |
| Image understanding | `bridge.py` — download photo, write to temp file, pass path in prompt |
| `/stop` command | `bridge.py` — call `_cancel_active(chat_id)` without a follow-up message |
| Scheduled tasks | New `scheduler.py` asyncio task alongside the polling loop |
| Multiple users | Already supported — each `chat_id` gets its own Claude session UUID |
| Docker | Single-container; mount `data/`, `logs/`, `.env`, `soul.md`, and Claude credentials as volumes |
| Ollama / local LLM | Replace `claude.py` subprocess call with Ollama API; inject domain context via RAG |
