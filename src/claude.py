"""
claude.py — Claude CLI wrapper with streaming output and session persistence

Uses `claude --print --output-format stream-json` for streaming.
Conversation history is managed entirely by the Claude CLI via --session-id / --resume.
No Anthropic API key needed — uses your existing Claude Code subscription.
"""

import asyncio
import json
import logging
import uuid
from dataclasses import dataclass
from typing import AsyncIterator

log = logging.getLogger(__name__)

CLAUDE_BIN = "/Users/bullabear/.local/bin/claude"
MAX_TOOL_ROUNDS = 10  # safety cap (claude CLI handles tools internally, but just in case)


@dataclass
class StreamChunk:
    text: str = ""
    done: bool = False
    error: str = ""


class ClaudeClient:
    def __init__(self, system_prompt: str = "", model: str = "sonnet", timeout_secs: int = 120):
        self._system_prompt = system_prompt
        self._model = model
        self._timeout = timeout_secs
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
        """
        cmd = self._build_command(prompt, session_id, is_new_session)
        log.info(f"Running: {' '.join(cmd[:6])}...")

        try:
            self._proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            proc = self._proc

            full_text = ""
            async for line in proc.stdout:
                line = line.decode("utf-8", errors="replace").strip()
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

            # Drain stderr for logging
            stderr = await proc.stderr.read()
            if stderr:
                log.debug(f"claude stderr: {stderr.decode('utf-8', errors='replace')[:500]}")

            await proc.wait()

            if proc.returncode not in (0, None):
                err = stderr.decode("utf-8", errors="replace").strip()
                yield StreamChunk(error=f"Claude exited with code {proc.returncode}: {err[:300]}")
                return

            yield StreamChunk(done=True)

        except asyncio.CancelledError:
            self.cancel()
            raise
        except asyncio.TimeoutError:
            yield StreamChunk(error=f"Claude timed out after {self._timeout}s")
        except FileNotFoundError:
            yield StreamChunk(error=f"Claude CLI not found at {CLAUDE_BIN}. Is Claude Code installed?")
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
                return StreamChunk(error=data.get("result", "Unknown error from Claude CLI"))
            return StreamChunk(done=True)

        elif event_type == "system":
            # Log the session_id from init event so we can verify
            if data.get("subtype") == "init":
                log.debug(f"Claude CLI session: {data.get('session_id')}")

        return None
