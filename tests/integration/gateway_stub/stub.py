# SPDX-License-Identifier: Apache-2.0
"""A stand-in model API for the gateway compatibility test (ADR-0037).

Runs inside the sandbox, next to the real Claude Code, on ``127.0.0.1``. It
answers every request with a short valid reply and records each request's
shape (never its prompt text) as one JSON line, so the test can check what
pinned Claude Code sends through a plain-HTTP ``ANTHROPIC_BASE_URL``.

Usage: ``python3 stub.py <port> <log file> <expected token>``.
Standard library only.
"""

from __future__ import annotations

import json
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

PORT, LOG, TOKEN = int(sys.argv[1]), sys.argv[2], sys.argv[3]
SERVER_TOOL_PREFIXES = ("web_search", "web_fetch", "code_execution", "bash_2", "text_editor_2")


def _shape(path: str, headers: Any, body: bytes) -> dict[str, Any]:
    """What the request is, without its content."""
    try:
        data = json.loads(body) if body else {}
    except ValueError:
        data = {"unparsable": True}
    if not isinstance(data, dict):
        data = {}
    tools = data.get("tools") or []
    blocks: set[str] = set()
    for message in data.get("messages") or []:
        content = message.get("content") if isinstance(message, dict) else None
        if isinstance(content, list):
            blocks.update(b.get("type", "?") for b in content if isinstance(b, dict))
    thinking = data.get("thinking")
    return {
        "path": path,
        "auth_ok": headers.get("x-api-key") == TOKEN
        or headers.get("authorization") == f"Bearer {TOKEN}",
        "auth_present": bool(headers.get("x-api-key") or headers.get("authorization")),
        "beta": headers.get("anthropic-beta"),
        "version": headers.get("anthropic-version"),
        "bytes": len(body),
        "model": data.get("model"),
        "max_tokens": data.get("max_tokens"),
        "stream": bool(data.get("stream")),
        "thinking_budget": thinking.get("budget_tokens") if isinstance(thinking, dict) else None,
        "tool_types": sorted({t.get("type", "custom") for t in tools if isinstance(t, dict)}),
        "server_tools": sorted(
            {
                str(t.get("type"))
                for t in tools
                if isinstance(t, dict) and str(t.get("type", "")).startswith(SERVER_TOOL_PREFIXES)
            }
        ),
        "content_blocks": sorted(blocks),
        "messages": len(data.get("messages") or []),
    }


def _sse(model: str) -> bytes:
    usage = {"input_tokens": 10, "output_tokens": 1}
    events = [
        (
            "message_start",
            {
                "type": "message_start",
                "message": {
                    "id": "msg_stub",
                    "type": "message",
                    "role": "assistant",
                    "model": model,
                    "content": [],
                    "stop_reason": None,
                    "stop_sequence": None,
                    "usage": usage,
                },
            },
        ),
        (
            "content_block_start",
            {
                "type": "content_block_start",
                "index": 0,
                "content_block": {"type": "text", "text": ""},
            },
        ),
        (
            "content_block_delta",
            {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "text_delta", "text": "OK"},
            },
        ),
        ("content_block_stop", {"type": "content_block_stop", "index": 0}),
        (
            "message_delta",
            {
                "type": "message_delta",
                "delta": {"stop_reason": "end_turn", "stop_sequence": None},
                "usage": {"output_tokens": 1},
            },
        ),
        ("message_stop", {"type": "message_stop"}),
    ]
    return b"".join(
        f"event: {name}\ndata: {json.dumps(data)}\n\n".encode() for name, data in events
    )


def _message(model: str) -> bytes:
    return json.dumps(
        {
            "id": "msg_stub",
            "type": "message",
            "role": "assistant",
            "model": model,
            "content": [{"type": "text", "text": "OK"}],
            "stop_reason": "end_turn",
            "stop_sequence": None,
            "usage": {"input_tokens": 10, "output_tokens": 1},
        }
    ).encode()


class Handler(BaseHTTPRequestHandler):
    def _record(self, body: bytes) -> dict[str, Any]:
        shape = _shape(self.path, self.headers, body)
        shape["method"] = self.command
        with open(LOG, "a", encoding="utf-8") as log:
            log.write(json.dumps(shape) + "\n")
        return shape

    def _send(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("content-type", content_type)
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:
        body = self.rfile.read(int(self.headers.get("content-length") or 0))
        shape = self._record(body)
        if not shape["auth_ok"]:
            self._send(
                401,
                b'{"type":"error","error":{"type":"authentication_error",'
                b'"message":"stub: unexpected credential"}}',
                "application/json",
            )
            return
        model = str(shape["model"] or "stub")
        path = self.path.split("?")[0]
        if path.endswith("/count_tokens"):
            self._send(200, b'{"input_tokens": 10}', "application/json")
        elif path.endswith("/v1/messages"):
            if shape["stream"]:
                self._send(200, _sse(model), "text/event-stream")
            else:
                self._send(200, _message(model), "application/json")
        else:
            self._send(
                404,
                b'{"type":"error","error":{"type":"not_found_error","message":"stub"}}',
                "application/json",
            )

    def do_GET(self) -> None:
        self._record(b"")
        self._send(
            404,
            b'{"type":"error","error":{"type":"not_found_error","message":"stub"}}',
            "application/json",
        )

    def log_message(self, *args: object) -> None:
        pass


if __name__ == "__main__":
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
