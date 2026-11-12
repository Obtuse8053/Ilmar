# SPDX-License-Identifier: Apache-2.0
"""Ilmar's model gateway: the sandbox's only route to the model (ADR-0037 §1).

This file runs on its own inside the gateway sidecar container
(``python3 -I model_gateway.py …``), so it uses only the standard library and
never imports ``ilmar``. The Podman adapter mounts it read-only.

What it does, for each request from the sandbox:

* serves only clients on the sandbox's private network;
* accepts only ``POST /v1/messages`` and ``POST /v1/messages/count_tokens``
  (optionally ``?beta=true``), with a ``Content-Length`` of at most
  ``--max-request-bytes`` and no other transfer encoding;
* accepts only the per-attempt throwaway token (``x-api-key`` or
  ``Authorization: Bearer``), compared by SHA-256 digest in constant time. The
  gateway is given the digest, never the token;
* forwards an allow-listed set of headers, drops every credential the sandbox
  sent, and adds the real key, which it read once from ``--key-file`` and
  holds only in memory;
* allows at most ``--max-in-flight`` requests at once;
* streams the response back unchanged, with an allow-listed set of headers,
  then closes the connection.

Everything refused is answered here in the provider's error shape, and
nothing reaches the provider. The log has one line per request (method, path,
status, sizes), never a header value, a body or a credential.

Not here yet: the worst-case cost reservation (ADR-0037 §2, P5). Until it
lands, no configuration may route a real agent through this gateway.
"""

from __future__ import annotations

import argparse
import email.message
import hashlib
import hmac
import http.client
import ipaddress
import json
import re
import socket
import ssl
import sys
import threading
import urllib.parse
from collections.abc import Sequence
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import IO

PATHS = frozenset({"/v1/messages", "/v1/messages/count_tokens"})
QUERIES = frozenset({"", "beta=true"})
REQUEST_HEADERS = frozenset(
    {"content-type", "accept", "anthropic-version", "anthropic-beta", "user-agent"}
)
"""Forwarded as sent, along with ``x-stainless-*`` (SDK metadata). Nothing else."""
RESPONSE_HEADERS = frozenset(
    {"content-type", "request-id", "retry-after", "x-should-retry", "cache-control"}
)
"""Returned as received, along with ``anthropic-*``. Nothing else."""
CREDENTIAL_HEADERS = ("x-api-key", "authorization")
UPSTREAM_TIMEOUT = 600.0
CHUNK = 65536
_TOKEN_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_PRINTABLE = re.compile(r"[^\x21-\x7e]")


@dataclass(frozen=True, slots=True)
class Upstream:
    scheme: str
    host: str
    port: int

    @classmethod
    def parse(cls, value: str, *, allow_plain_http: bool) -> Upstream:
        parts = urllib.parse.urlsplit(value)
        if parts.scheme not in ("https", "http") or not parts.hostname:
            raise ValueError("the upstream must be an http(s) URL with a host")
        if parts.scheme == "http" and not allow_plain_http:
            raise ValueError("the upstream must use https")
        if parts.path not in ("", "/") or parts.query or parts.fragment or parts.username:
            raise ValueError("the upstream is a scheme, host and port only")
        port = parts.port or (443 if parts.scheme == "https" else 80)
        return cls(parts.scheme, parts.hostname, port)


@dataclass(frozen=True, slots=True)
class GatewayConfig:
    token_sha256: str
    key: str
    upstream: Upstream
    clients: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...]
    max_request_bytes: int = 4 * 1024 * 1024
    max_in_flight: int = 4

    def __post_init__(self) -> None:
        if not _TOKEN_DIGEST.match(self.token_sha256):
            raise ValueError("token_sha256 must be 64 lower-case hex digits")
        if not self.key or any(c in self.key for c in "\r\n\0"):
            raise ValueError("the key must be one non-empty line")
        if not self.clients:
            raise ValueError("at least one client network is needed")
        if self.max_request_bytes < 1 or self.max_in_flight < 1:
            raise ValueError("limits must be positive")

    def __repr__(self) -> str:  # never the key
        return f"GatewayConfig(upstream={self.upstream!r}, clients={self.clients!r})"


class Gateway:
    """The server. ``serve_forever`` until ``shutdown``."""

    def __init__(
        self, listen: tuple[str, int], config: GatewayConfig, *, log: IO[str] | None = None
    ) -> None:
        self.config = config
        self._log = log if log is not None else sys.stdout
        self._log_lock = threading.Lock()
        self._slots = threading.BoundedSemaphore(config.max_in_flight)
        gateway = self

        class Handler(_Handler):
            owner = gateway

        self._server = ThreadingHTTPServer(listen, Handler)
        self._server.daemon_threads = True

    @property
    def address(self) -> tuple[str, int]:
        host, port = self._server.server_address[:2]
        return str(host), int(port)

    def serve_forever(self) -> None:
        self.log("ilmar-gateway READY")
        self._server.serve_forever(poll_interval=0.2)

    def shutdown(self) -> None:
        self._server.shutdown()
        self._server.server_close()

    def log(self, line: str) -> None:
        with self._log_lock:
            self._log.write(line + "\n")
            self._log.flush()

    def client_allowed(self, address: str) -> bool:
        try:
            ip = ipaddress.ip_address(address)
        except ValueError:
            return False
        return any(ip in network for network in self.config.clients)

    def authenticated(self, headers: email.message.Message) -> bool:
        offered: list[str] = []
        if (key := headers.get("x-api-key")) is not None:
            offered.append(key)
        if (authorization := headers.get("authorization")) is not None:
            scheme, _, token = authorization.partition(" ")
            offered.append(token if scheme.lower() == "bearer" else "")
        if not offered or len(headers.get_all("x-api-key") or []) > 1:
            return False
        expected = self.config.token_sha256.encode()
        return all(
            hmac.compare_digest(hashlib.sha256(o.encode()).hexdigest().encode(), expected)
            for o in offered
        )

    def try_acquire(self) -> bool:
        return self._slots.acquire(blocking=False)

    def release(self) -> None:
        self._slots.release()


class _Handler(BaseHTTPRequestHandler):
    owner: Gateway
    protocol_version = "HTTP/1.1"
    server_version = "ilmar-gateway"
    sys_version = ""

    def log_message(self, format: str, *args: object) -> None:
        """Replaced by one line per request in ``_finish``."""

    def do_GET(self) -> None:
        self._refuse(405, "invalid_request_error", "method not allowed")

    do_PUT = do_DELETE = do_PATCH = do_HEAD = do_OPTIONS = do_GET  # noqa: N815

    def do_POST(self) -> None:
        gateway = self.owner
        config = gateway.config
        if not gateway.client_allowed(self.client_address[0]):
            return self._refuse(403, "permission_error", "client not allowed")
        path, _, query = self.path.partition("?")
        if path not in PATHS or query not in QUERIES:
            return self._refuse(404, "not_found_error", "not served by this gateway")
        if not gateway.authenticated(self.headers):
            return self._refuse(401, "authentication_error", "invalid x-api-key")
        if self.headers.get("transfer-encoding") is not None:
            return self._refuse(400, "invalid_request_error", "chunked bodies are refused")
        try:
            length = int(self.headers.get("content-length", ""))
        except ValueError:
            return self._refuse(411, "invalid_request_error", "content-length required")
        if length < 0 or len(self.headers.get_all("content-length") or []) != 1:
            return self._refuse(400, "invalid_request_error", "bad content-length")
        if length > config.max_request_bytes:
            return self._refuse(413, "request_too_large", "request body too large")
        body = self.rfile.read(length)
        if len(body) != length:
            return self._refuse(400, "invalid_request_error", "incomplete body")
        if not gateway.try_acquire():
            return self._refuse(429, "rate_limit_error", "too many requests in flight")
        try:
            self._forward(path, query, body)
        finally:
            gateway.release()

    # -- forwarding -------------------------------------------------------------------

    def _forward(self, path: str, query: str, body: bytes) -> None:
        upstream = self.owner.config.upstream
        headers = {
            name: value
            for name, value in self.headers.items()
            if name.lower() in REQUEST_HEADERS or name.lower().startswith("x-stainless-")
        }
        headers["x-api-key"] = self.owner.config.key
        headers["content-length"] = str(len(body))
        headers["host"] = (
            upstream.host if upstream.port in (80, 443) else (f"{upstream.host}:{upstream.port}")
        )
        target = path + ("?" + query if query else "")
        connection: http.client.HTTPConnection
        if upstream.scheme == "https":
            connection = http.client.HTTPSConnection(
                upstream.host, upstream.port, timeout=UPSTREAM_TIMEOUT,
                context=ssl.create_default_context(),
            )  # fmt: skip
        else:
            connection = http.client.HTTPConnection(
                upstream.host, upstream.port, timeout=UPSTREAM_TIMEOUT
            )
        try:
            try:
                connection.request("POST", target, body=body, headers=headers)
                response = connection.getresponse()
            except (OSError, http.client.HTTPException):
                return self._refuse(502, "api_error", "the provider could not be reached")
            self.send_response(response.status)
            for name, value in response.getheaders():
                lower = name.lower()
                if lower in RESPONSE_HEADERS or lower.startswith("anthropic-"):
                    self.send_header(name, value)
            self.send_header("connection", "close")
            self.end_headers()
            sent = 0
            try:
                while chunk := response.read1(CHUNK):
                    self.wfile.write(chunk)
                    self.wfile.flush()
                    sent += len(chunk)
            except (OSError, http.client.HTTPException):
                self.close_connection = True  # the client sees a cut-off stream
            self._finish(response.status, len(body), sent)
        finally:
            connection.close()
            self.close_connection = True

    def _refuse(self, status: int, kind: str, message: str) -> None:
        payload = json.dumps(
            {"type": "error", "error": {"type": kind, "message": message}}
        ).encode()
        self.send_response(status)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(payload)))
        self.send_header("connection", "close")
        self.end_headers()
        self.wfile.write(payload)
        self.close_connection = True
        self._finish(status, 0, len(payload), refused=kind)

    def _finish(self, status: int, received: int, sent: int, refused: str = "") -> None:
        path = _PRINTABLE.sub("?", self.path.partition("?")[0])[:100]
        line = f"REQUEST {self.command} {path} {status} in={received} out={sent}"
        self.owner.log(line + (f" refused={refused}" if refused else ""))


def read_key(path: str) -> str:
    with open(path, encoding="utf-8") as handle:
        key = handle.read().strip()
    if not key or any(c in key for c in "\r\n\0"):
        raise ValueError("the key file must hold one non-empty line")
    return key


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").split("\n", 1)[0])
    parser.add_argument("--listen", default="0.0.0.0:8080")
    parser.add_argument("--token-sha256", required=True)
    parser.add_argument("--key-file", required=True)
    parser.add_argument("--upstream", default="https://api.anthropic.com")
    parser.add_argument("--allow-plain-http-upstream", action="store_true")
    parser.add_argument("--clients", required=True, help="comma-separated client networks")
    parser.add_argument("--max-request-bytes", type=int, default=4 * 1024 * 1024)
    parser.add_argument("--max-in-flight", type=int, default=4)
    args = parser.parse_args(argv)
    config = GatewayConfig(
        token_sha256=args.token_sha256,
        key=read_key(args.key_file),
        upstream=Upstream.parse(args.upstream, allow_plain_http=args.allow_plain_http_upstream),
        clients=tuple(ipaddress.ip_network(n, strict=False) for n in args.clients.split(",")),
        max_request_bytes=args.max_request_bytes,
        max_in_flight=args.max_in_flight,
    )
    host, _, port = args.listen.rpartition(":")
    socket.setdefaulttimeout(None)
    Gateway((host, int(port)), config).serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
