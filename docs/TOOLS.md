# Tool Use Reference

cc-telegram-bridge delegates all tool use to the Claude Code CLI. When Claude receives your message, it has access to its full built-in toolset — the same tools available in the Claude Code desktop app and terminal.

---

## How It Works

Tools are always available — there is no `/tools on` toggle. Claude decides when to use them based on your request, just as it does in the terminal.

The `--dangerously-skip-permissions` flag is passed to the Claude CLI so it can run non-interactively. This means Claude will execute tool calls without prompting for confirmation on each one. Use this bridge only with trusted users (enforced via `ALLOWED_USER_IDS`).

---

## Available Tools

These are Claude Code's built-in tools, all available through the bridge:

| Tool | What it does |
|---|---|
| `Bash` | Run shell commands on your machine |
| `Read` | Read a file's contents |
| `Write` | Write content to a file |
| `Edit` | Make targeted edits to a file |
| `Glob` | Find files by pattern |
| `Grep` | Search file contents by regex |
| `WebSearch` | Search the web |
| `WebFetch` | Fetch a URL's content |

Claude also has access to the full agent, task, and memory system it uses in normal Claude Code sessions — including persistent auto-memory across conversations.

---

## Example Prompts

- "Check how much disk space is free"
- "Show me the last 50 lines of `~/Documents/live-trading-engine/logs/scanner.log`"
- "What Python processes are running right now?"
- "Read my trading engine config at `~/Documents/live-trading-engine/config/system.yaml`"
- "Write a script to `~/scripts/cleanup.sh` that removes log files older than 7 days"
- "Search the web for the latest SPY options chain"

---

## Safety

- **Allowlist**: Only Telegram users listed in `ALLOWED_USER_IDS` can send messages to the bot
- **Local only**: Claude runs as your user account — it has the same permissions you do, nothing more
- **No escalation**: Commands cannot elevate privileges beyond your user
- **Audit log**: Every exchange is logged to `data/bridge.db` in the `exchanges` table

---

## Working Directory

The Claude CLI subprocess inherits the working directory of the bridge process, which is the `cc-telegram-bridge/` project root. When asking Claude to access files elsewhere, use absolute paths or home-relative paths (e.g. `~/Documents/...`).

---

## Memory

Claude Code's auto-memory system is active. Claude will remember facts about you, your preferences, and your projects across conversations. Memory is stored in `~/.claude/projects/...` alongside session history.

To start completely fresh (clear both session history and working memory for a chat):

```
/new
```
