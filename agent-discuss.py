#!/usr/bin/env python3
"""Filesystem-backed MCP server for turn-based agent-to-agent discussion."""

from __future__ import annotations

import fcntl
import json
import os
import random
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

BASE_DIR = Path(
    os.environ.get(
        "AGENT_DISCUSS_HOME", Path.home() / ".local" / "share" / "agent-discuss"
    )
)
SESSIONS_DIR = BASE_DIR / "sessions"
LOG_PATH = Path(os.environ.get("AGENT_DISCUSS_LOG", "/tmp/agent-discuss-mcp.log"))

SERVER_NAME = "agent-discuss"
SERVER_VERSION = "1.0.0"

MAX_MESSAGE_BYTES = 64 * 1024
MAX_SESSION_ID_LEN = 80
SESSION_ID_COLLISION_ATTEMPTS = 20
POLL_INTERVAL_SECS = 0.5

ADJECTIVES = [
    "amber", "ancient", "brisk", "calm", "clever", "cobalt", "crimson",
    "curious", "ember", "gentle", "golden", "hidden", "icy", "jolly",
    "lunar", "mellow", "nimble", "quiet", "rapid", "silver", "solar",
    "steady", "stormy", "velvet",
]
NOUNS = [
    "anchor", "badger", "bridge", "comet", "elephant", "falcon", "forest",
    "harbor", "horizon", "hurricane", "lantern", "meadow", "otter",
    "pioneer", "river", "signal", "sparrow", "summit", "thunder",
    "voyager", "washer", "willow", "window", "zephyr",
]

# ---------------------------------------------------------------------------
# I/O helpers
# ---------------------------------------------------------------------------

_stdin = sys.stdin.buffer
_stdout = sys.stdout.buffer


def _now_iso() -> str:
    """Return the current UTC time as a compact ISO-8601 string."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _log(message: str) -> None:
    """Best-effort append to the log file; never raises."""
    try:
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with LOG_PATH.open("a", encoding="utf-8") as fh:
            fh.write(f"{_now_iso()} {message}\n")
    except Exception:
        pass


def _send(message: dict) -> None:
    """Write a single JSON-RPC message to stdout (newline-delimited)."""
    payload = json.dumps(message, separators=(",", ":")).encode("utf-8") + b"\n"
    _stdout.write(payload)
    _stdout.flush()


def _recv() -> dict | None:
    """Read the next non-blank line from stdin and parse it as JSON."""
    while True:
        raw = _stdin.readline()
        if not raw:
            return None
        raw = raw.strip()
        if raw:
            return json.loads(raw.decode("utf-8"))

# ---------------------------------------------------------------------------
# Session-ID helpers
# ---------------------------------------------------------------------------

_SESSION_ID_RE = re.compile(r"[a-z0-9\-]+")


def _sanitize_session_id(raw: str) -> str:
    """Normalise and validate a session ID. Raises on empty/invalid input."""
    value = raw.strip().lower().replace("_", "-").replace(" ", "-")
    value = "-".join(_SESSION_ID_RE.findall(value))
    if not value:
        raise ValueError("session_id must contain letters or numbers")
    return value[:MAX_SESSION_ID_LEN]


def _random_session_id() -> str:
    """Generate a human-friendly session ID that doesn't collide on disk."""
    for _ in range(SESSION_ID_COLLISION_ATTEMPTS):
        candidate = f"{random.choice(ADJECTIVES)}-{random.choice(NOUNS)}-{random.choice(NOUNS)}"
        if not (SESSIONS_DIR / candidate).exists():
            return candidate
    return f"session-{int(time.time())}-{random.randint(1000, 9999)}"

# ---------------------------------------------------------------------------
# Storage primitives
#
# Path helpers accept *already-sanitised* session IDs so that sanitisation
# happens exactly once, at the tool-function boundary.
# ---------------------------------------------------------------------------


def _session_dir(sid: str) -> Path:
    return SESSIONS_DIR / sid


def _session_file(sid: str) -> Path:
    return _session_dir(sid) / "session.json"


def _message_file(sid: str) -> Path:
    return _session_dir(sid) / "messages.ndjson"


def _lock_path(sid: str) -> Path:
    return SESSIONS_DIR / f".{sid}.lock"


def _read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json(path: Path, data: dict) -> None:
    """Atomically overwrite *path* with pretty-printed JSON."""
    tmp = path.with_suffix(f".{os.getpid()}.tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(path)


def _read_messages(sid: str) -> list[dict]:
    """Return all messages for *sid*, or [] if the file doesn't exist yet."""
    path = _message_file(sid)
    if not path.exists():
        return []
    messages: list[dict] = []
    with path.open("r", encoding="utf-8") as fh:
        for raw_line in fh:
            stripped = raw_line.strip()
            if stripped:
                messages.append(json.loads(stripped))
    return messages


def _read_messages_after(sid: str, after_seq: int) -> list[dict]:
    """Return messages with seq > *after_seq*, reading only what's needed."""
    path = _message_file(sid)
    if not path.exists():
        return []
    result: list[dict] = []
    with path.open("r", encoding="utf-8") as fh:
        for raw_line in fh:
            stripped = raw_line.strip()
            if not stripped:
                continue
            msg = json.loads(stripped)
            if int(msg.get("seq", 0)) > after_seq:
                result.append(msg)
    return result


def _append_message(sid: str, message: dict) -> None:
    with _message_file(sid).open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(message, separators=(",", ":")) + "\n")


def _load_session(sid: str) -> dict:
    """Load a session's metadata. Raises FileNotFoundError if missing."""
    path = _session_file(sid)
    if not path.exists():
        raise FileNotFoundError(f"Unknown session_id: {sid}")
    return _read_json(path)


def _read_session_snapshot(sid: str, after_seq: int = 0) -> tuple[dict, list[dict]]:
    """Read session metadata and messages under a shared lock."""
    with _SessionLock(sid, exclusive=False):
        session = _load_session(sid)
        messages = _read_messages_after(sid, after_seq)
    return session, messages

# ---------------------------------------------------------------------------
# Locking helper
# ---------------------------------------------------------------------------


class _SessionLock:
    """Context manager that holds a flock on a session-specific lock file."""

    def __init__(self, sid: str, *, exclusive: bool = True) -> None:
        self._path = _lock_path(sid)
        self._mode = fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH

    def __enter__(self) -> _SessionLock:
        SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
        self._fh = self._path.open("a")
        fcntl.flock(self._fh, self._mode)
        return self

    def __exit__(self, *exc) -> None:
        fcntl.flock(self._fh, fcntl.LOCK_UN)
        self._fh.close()

# ---------------------------------------------------------------------------
# Agent-hint constants
# ---------------------------------------------------------------------------

_HINT_AFTER_SEND = (
    "You MUST now call wait_for_messages to receive the reply. "
    "Stay in the conversation loop: send → wait → read → reply. "
    "Do NOT spawn or create another agent — the other participant is "
    "expected to already be running independently. "
    "Do not disengage, summarize to the user, or end your turn until "
    "the conversation reaches a natural conclusion."
)

_HINT_AFTER_WAIT = (
    "You MUST read the new message(s) and call send_message with your reply. "
    "Stay in the conversation loop: read → reply → wait. "
    "Do not disengage or summarize to the user mid-conversation."
)

_HINT_AFTER_GET = (
    "Review the session state above. If there are messages to respond to, "
    "call send_message with your reply, then call wait_for_messages. "
    "Stay in the send → wait → read → reply loop until the conversation "
    "reaches a natural conclusion."
)

# ---------------------------------------------------------------------------
# Tool implementations
# ---------------------------------------------------------------------------


def _tool_send_message(arguments: dict) -> dict:
    """Send a message into an existing session, or create a new one when omitted."""
    author = str(arguments.get("author", "")).strip()
    body = str(arguments.get("message", "")).strip()
    requested_id = arguments.get("session_id")
    title = arguments.get("title")

    if not author:
        raise ValueError("author is required")
    if not body:
        raise ValueError("message is required")
    if len(body.encode("utf-8")) > MAX_MESSAGE_BYTES:
        raise ValueError(f"message exceeds {MAX_MESSAGE_BYTES} bytes")

    if requested_id is not None:
        sid = _sanitize_session_id(str(requested_id))
        with _SessionLock(sid):
            if not _session_file(sid).exists():
                raise FileNotFoundError(f"Unknown session_id: {sid}")
            session = _load_session(sid)
            created = False

            participants = list(session.get("participants", []))
            if author not in participants:
                participants.append(author)

            seq = int(session.get("last_seq", 0)) + 1
            ts = _now_iso()
            message = {"seq": seq, "timestamp": ts, "author": author, "message": body}
            _append_message(sid, message)

            session.update({
                "updated_at": ts,
                "participants": participants,
                "last_seq": seq,
            })
            _write_json(_session_file(sid), session)
    else:
        while True:
            sid = _random_session_id()
            with _SessionLock(sid):
                if _session_file(sid).exists():
                    continue

                _session_dir(sid).mkdir(parents=True, exist_ok=True)
                now = _now_iso()
                session = {
                    "session_id": sid,
                    "title": (title or "").strip() or None,
                    "created_at": now,
                    "updated_at": now,
                    "participants": [],
                    "last_seq": 0,
                }
                _write_json(_session_file(sid), session)
                _message_file(sid).touch()
                created = True

                participants = [author]
                seq = 1
                ts = _now_iso()
                message = {"seq": seq, "timestamp": ts, "author": author, "message": body}
                _append_message(sid, message)

                session.update({
                    "updated_at": ts,
                    "participants": participants,
                    "last_seq": seq,
                })
                _write_json(_session_file(sid), session)
                break

    return {
        "ok": True,
        "session_id": sid,
        "created": created,
        "message": message,
        "last_seq": seq,
        "participants": participants,
        "agent_hint": _HINT_AFTER_SEND,
        "recommended_next_call": {
            "tool": "wait_for_messages",
            "arguments": {"session_id": sid, "agent_name": author, "after_seq": seq},
        },
    }


def _tool_wait_for_messages(arguments: dict) -> dict:
    """Poll until at least one new message from another author appears."""
    sid = _sanitize_session_id(str(arguments.get("session_id", "")))
    agent_name = str(arguments.get("agent_name", "")).strip()
    after_seq = int(arguments.get("after_seq", 0) or 0)

    if not agent_name:
        raise ValueError("agent_name is required")

    while True:
        session, snapshot_messages = _read_session_snapshot(sid, after_seq)
        messages = [m for m in snapshot_messages if m.get("author") != agent_name]
        if messages:
            last_seq = int(session.get("last_seq", after_seq))
            return {
                "ok": True,
                "session_id": sid,
                "messages": messages,
                "last_seq": last_seq,
                "message_count": len(messages),
                "participants": session.get("participants", []),
                "agent_hint": _HINT_AFTER_WAIT,
                "recommended_next_call": {
                    "tool": "send_message",
                    "arguments": {
                        "session_id": sid,
                        "author": agent_name,
                        "message": "<your reply>",
                    },
                },
            }
        time.sleep(POLL_INTERVAL_SECS)


def _tool_get_session(arguments: dict) -> dict:
    """Return session metadata and (optionally filtered) messages."""
    sid = _sanitize_session_id(str(arguments.get("session_id", "")))
    after_seq = int(arguments.get("after_seq", 0) or 0)

    session, messages = _read_session_snapshot(sid, after_seq)
    last_seq = int(session.get("last_seq", 0))

    return {
        "ok": True,
        "session_id": sid,
        "session": session,
        "messages": messages,
        "message_count": len(messages),
        "last_seq": last_seq,
        "agent_hint": _HINT_AFTER_GET,
        "recommended_next_call": {
            "tool": "send_message",
            "arguments": {
                "session_id": sid,
                "author": "<your agent name>",
                "message": "<your reply>",
            },
        },
    }

# ---------------------------------------------------------------------------
# Tool dispatch & schema (module-level constants)
# ---------------------------------------------------------------------------

_TOOL_DISPATCH: dict[str, callable] = {
    "send_message": _tool_send_message,
    "wait_for_messages": _tool_wait_for_messages,
    "get_session": _tool_get_session,
}

_TOOL_DEFINITIONS: list[dict] = [
    {
        "name": "send_message",
        "description": (
            "Send a message into an agent-discuss session. If session_id is "
            "omitted, a new session is created and a human-readable session_id "
            "is returned. After sending, call wait_for_messages to block until "
            "the other (independently running) participant replies."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "session_id": {
                    "type": "string",
                    "description": "Optional existing session id. Omit to create a new session.",
                },
                "author": {
                    "type": "string",
                    "description": "Stable name for the current agent, e.g. claude-agent-1.",
                },
                "message": {
                    "type": "string",
                    "description": "Message body to deliver to the other agent(s).",
                },
                "title": {
                    "type": "string",
                    "description": "Optional title for a brand new session.",
                },
            },
            "required": ["author", "message"],
            "additionalProperties": False,
        },
    },
    {
        "name": "wait_for_messages",
        "description": (
            "Block until new messages from other agents appear after a known "
            "sequence number. The other participant must already be running "
            "independently — this tool does NOT create or spawn another agent."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "session_id": {
                    "type": "string",
                    "description": "Existing session id to listen on.",
                },
                "agent_name": {
                    "type": "string",
                    "description": (
                        "Name of the current agent. Messages from this author "
                        "are filtered out."
                    ),
                },
                "after_seq": {
                    "type": "integer",
                    "description": "Only return messages with seq greater than this value.",
                    "default": 0,
                },
            },
            "required": ["session_id", "agent_name"],
            "additionalProperties": False,
        },
    },
    {
        "name": "get_session",
        "description": (
            "Inspect a session and optionally return messages after a given "
            "sequence number. Returns immediately without waiting."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "session_id": {
                    "type": "string",
                    "description": "Session id to inspect.",
                },
                "after_seq": {
                    "type": "integer",
                    "description": "Only return messages with seq greater than this value.",
                    "default": 0,
                },
            },
            "required": ["session_id"],
            "additionalProperties": False,
        },
    },
]

# ---------------------------------------------------------------------------
# JSON-RPC plumbing
# ---------------------------------------------------------------------------


def _ok_content(result: dict) -> dict:
    """Wrap a tool result as MCP tool-call response content."""
    return {
        "content": [{"type": "text", "text": json.dumps(result, sort_keys=True)}],
        "isError": False,
    }


def _err_content(message: str) -> dict:
    return {"content": [{"type": "text", "text": message}], "isError": True}


def _handle_request(request: dict) -> dict | None:
    """Route a single JSON-RPC request and return the response (or None)."""
    if not isinstance(request, dict):
        return {
            "jsonrpc": "2.0",
            "id": None,
            "error": {"code": -32600, "message": "Request must be a JSON object"},
        }

    method = request.get("method")
    req_id = request.get("id")

    if method == "initialize":
        proto = request.get("params", {}).get("protocolVersion", "2024-11-05")
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {
                "protocolVersion": proto,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
            },
        }

    if method == "notifications/initialized":
        return None

    if method == "tools/list":
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {"tools": _TOOL_DEFINITIONS},
        }

    if method == "tools/call":
        params = request.get("params", {})
        name = str(params.get("name", ""))
        args = params.get("arguments", {})
        handler = _TOOL_DISPATCH.get(name)
        if handler is None:
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": _err_content(f"Unknown tool: {name}"),
            }
        try:
            result = handler(args)
            return {"jsonrpc": "2.0", "id": req_id, "result": _ok_content(result)}
        except Exception as exc:
            _log(f"tool_error name={name} error={exc}")
            return {"jsonrpc": "2.0", "id": req_id, "result": _err_content(str(exc))}

    # Unknown method: error if it's a request (has id), ignore if notification.
    if req_id is not None:
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "error": {"code": -32601, "message": f"Unknown method: {method}"},
        }
    return None


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> None:
    """Run the MCP stdio server loop."""
    SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
    _log(f"starting pid={os.getpid()}")

    while True:
        request = _recv()
        if request is None:
            break
        try:
            response = _handle_request(request)
        except Exception as exc:
            _log(f"fatal_request_error error={exc}")
            req_id = request.get("id") if isinstance(request, dict) else None
            response = (
                {"jsonrpc": "2.0", "id": req_id, "error": {"code": -32000, "message": str(exc)}}
                if req_id is not None
                else None
            )
        if response is not None:
            _send(response)


if __name__ == "__main__":
    main()
