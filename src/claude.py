"""
claude.py — Claude CLI wrapper with streaming output and session persistence

Uses `claude --print --output-format stream-json` for streaming.
Conversation history is managed entirely by the Claude CLI via --session-id / --resume.
No Anthropic API key needed — uses your existing Claude Code subscription.
"""

import asyncio
import json
import logging
import os
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import AsyncIterator

log = logging.getLogger(__name__)

# Pin to the user-local install. Different Claude installs have different auth
# state and ACLs on macOS keychain; picking one via PATH fallback silently
# routes to the wrong binary after a system-wide update.
# Override with CLAUDE_BIN=/path/to/claude if you need a different install.
CLAUDE_BIN = os.environ.get("CLAUDE_BIN") or str(Path.home() / ".local/bin/claude")
# Tight deadline to receive the first byte from the subprocess.
# Catches stale --resume sessions that hang while loading large history.
FIRST_BYTE_TIMEOUT = 30  # seconds

# Maximum time we're willing to block waiting for the NEXT stdout line once
# streaming has started. This is NOT a task-kill timer — it's just how long
# asyncio.wait_for will sleep between poll iterations so the event loop stays
# responsive (cancellation, daemon shutdown, etc.). If the CLI is genuinely
# silent for this long, we loop back and wait again — no kill.
READLINE_POLL_SECS = 300  # 5 minutes


@dataclass
class StreamChunk:
    text: str = ""
    done: bool = False
    error: str = ""


class ClaudeClient:
    def __init__(self, system_prompt: str = "", model: str = "sonnet"):
        self._system_prompt = system_prompt
        self._model = model
        self._proc: asyncio.subprocess.Process | None = None

    def new_session_id(self) -> str:
        """Generate a new UUID to use as a Claude CLI session ID."""
        return str(uuid.uuid4())

    def cancel(self):
        """Kill the currently running Claude subprocess, if any."""
        if self._proc and self._proc.returncode is None:
            try:
                self._proc.kill()
                log.info("Claude subprocess killed (steering cancel)")
            except ProcessLookupError:
                pass  # already gone
        self._proc = None

    async def stream(
        self,
        prompt: str,
        session_id: str,
        is_new_session: bool,
    ) -> AsyncIterator[StreamChunk]:
        """
        Stream a Claude CLI response for a given prompt.

        - First message in a chat: uses --session-id <uuid> to start a new named session
        - Subsequent messages: uses --resume <uuid> to continue the same session

        Yields StreamChunk objects with incremental text.

        Timeouts:
        - FIRST_BYTE_TIMEOUT (30s): if no output at all within 30s, the
          subprocess is killed. Catches stale --resume hangs where the CLI is
          loading a large/corrupt history and never starts responding. This is
          the ONLY path that kills the subprocess automatically.
        - After the first byte, there is NO kill timer. A long agentic run can
          stay silent for hours between stdout lines (long tool calls, sleeps,
          waiting on external services) and the bridge will keep waiting. The
          caller should use steering (send a new message) or /new to stop a
          wedged task — we never time-bomb it here.
        """
        cmd = self._build_command(prompt, session_id, is_new_session)
        log.info(f"Running: {' '.join(cmd[:6])}...")

        # Declared outside try so except blocks can always reference them safely.
        got_first_byte = False
        proc = None

        try:
            # Claude CLI can emit very large JSON lines (tool outputs, long context).
            # Default asyncio StreamReader limit is 64KB — increase to 10MB to avoid
            # LimitOverrunError / ValueError crashes on large responses.
            _STREAM_LIMIT = 10 * 1024 * 1024  # 10 MB
            self._proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                limit=_STREAM_LIMIT,
            )
            proc = self._proc

            full_text = ""

            while True:
                if not got_first_byte:
                    # Kill-on-timeout only while waiting for the very first byte.
                    try:
                        raw = await asyncio.wait_for(proc.stdout.readline(), timeout=FIRST_BYTE_TIMEOUT)
                    except asyncio.TimeoutError:
                        raise
                else:
                    # Poll in bounded chunks so the event loop stays responsive
                    # to cancellation, but don't kill on silence — just keep
                    # waiting for the next line however long that takes.
                    while True:
                        try:
                            raw = await asyncio.wait_for(
                                proc.stdout.readline(), timeout=READLINE_POLL_SECS
                            )
                            break
                        except asyncio.TimeoutError:
                            if proc.returncode is not None:
                                raw = b""  # subprocess actually exited
                                break
                            # Still alive, just quiet. Loop and wait again.
                            continue

                if not raw:
                    break  # EOF

                got_first_byte = True
                line = raw.decode("utf-8", errors="replace").strip()
                if not line:
                    continue

                chunk = self._parse_stream_line(line)
                if chunk is None:
                    continue

                if chunk.error:
                    yield chunk
                    return

                if chunk.text:
                    full_text += chunk.text
                    yield chunk

                if chunk.done:
                    break

            # Drain stderr for error surfacing and logging.
            stderr_bytes = await proc.stderr.read()
            stderr_text = stderr_bytes.decode("utf-8", errors="replace").strip()
            if stderr_text:
                log.debug(f"claude stderr: {stderr_text[:500]}")

            await proc.wait()

            if proc.returncode not in (0, None):
                log.warning(f"claude exited {proc.returncode}: {stderr_text[:200]}")
                yield StreamChunk(error=f"Claude error (rc={proc.returncode}): {stderr_text[:300] or 'unknown'}")
                return

            yield StreamChunk(done=True)

        except asyncio.CancelledError:
            self.cancel()
            raise
        except asyncio.TimeoutError:
            # Only path that reaches here is the first-byte timeout; any
            # post-first-byte timeout is absorbed into the inner poll loop.
            self.cancel()
            yield StreamChunk(error=f"Claude did not respond within {FIRST_BYTE_TIMEOUT}s — session may be stale. Send /new to reset.")
        except FileNotFoundError:
            yield StreamChunk(error=f"Claude CLI not found ({CLAUDE_BIN}). Is Claude Code installed and on PATH?")
        except Exception as e:
            log.exception("Unexpected error running Claude CLI")
            yield StreamChunk(error=f"Error: {type(e).__name__}: {e}")
        finally:
            self._proc = None

    def _build_command(self, prompt: str, session_id: str, is_new_session: bool) -> list[str]:
        cmd = [
            CLAUDE_BIN,
            "--print",
            "--output-format", "stream-json",
            "--verbose",
            "--model", self._model,
            "--dangerously-skip-permissions",
        ]

        if is_new_session:
            cmd += ["--session-id", session_id]
        else:
            cmd += ["--resume", session_id]

        if self._system_prompt:
            cmd += ["--append-system-prompt", self._system_prompt]

        cmd.append(prompt)
        return cmd

    def _parse_stream_line(self, line: str) -> StreamChunk | None:
        """
        Parse a single stream-json line from the Claude CLI.

        Relevant event types (from --output-format stream-json --verbose):
          {"type":"assistant","message":{"content":[{"type":"text","text":"..."}],...}}
          {"type":"result","subtype":"success","result":"full text","is_error":false,...}
          {"type":"result","subtype":"error_max_turns","is_error":true,...}
          {"type":"system",...}  — ignored
        """
        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            return None

        event_type = data.get("type", "")

        if event_type == "assistant":
            # Extract text from content blocks
            message = data.get("message", {})
            content = message.get("content", [])
            text = ""
            for block in content:
                if block.get("type") == "text":
                    text += block.get("text", "")
            if text:
                return StreamChunk(text=text)

        elif event_type == "result":
            if data.get("is_error"):
                # Newer CLI puts error text in errors[] array; older versions used `result`.
                # Check both so session-recovery heuristics can match the message.
                errors = data.get("errors") or []
                msg = "; ".join(str(e) for e in errors) if errors else data.get("result") or ""
                return StreamChunk(error=msg or "Unknown error from Claude CLI")
            return StreamChunk(done=True)

        elif event_type == "system":
            # Log the session_id from init event so we can verify
            if data.get("subtype") == "init":
                log.debug(f"Claude CLI session: {data.get('session_id')}")

        return None
