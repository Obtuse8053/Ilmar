# SPDX-License-Identifier: Apache-2.0
"""Ilmar's egress proxy: ``CONNECT <listed-hostname>:443`` and nothing else.

ADR-0011 §5 and ADR-0015. This file runs on its own inside the proxy sidecar
container (``python3 -I egress_proxy.py …``), so it uses only the standard
library and must never import ``ilmar``. The Podman adapter mounts it
read-only.

Policy, applied to the request head before anything else happens:

* only ``CONNECT`` in authority form, only port 443;
* the target must be a hostname on the allow-list. IP literals, IPv6
  brackets and numeric hosts are refused, even for an allowed host's address;
* a ``Host`` header, if present, must name the same host;
* only clients from the given networks (the sandbox's internal subnet) are
  served.

DNS is resolved here, and only for a name that has passed the policy, so a
refused name is never looked up (each lookup is logged as ``RESOLVE``). After
``200 Connection established`` the bytes are relayed unchanged; the proxy
never sees inside TLS.
"""

from __future__ import annotations

import argparse
import contextlib
import ipaddress
import re
import select
import socket
import socketserver
import sys
import threading
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from typing import IO

ALLOWED_PORT = 443
MAX_HEAD_BYTES = 8192
HEAD_TIMEOUT = 10.0
CONNECT_TIMEOUT = 10.0
IDLE_TIMEOUT = 300.0

_LABEL = re.compile(r"^(?!-)[a-z0-9-]{1,63}(?<!-)$")
_PRINTABLE = re.compile(r"[^\x21-\x7e]")

Resolver = Callable[[str, int], Sequence[str]]
Connector = Callable[[str, int, float], socket.socket]


@dataclass(frozen=True, slots=True)
class Decision:
    allowed: bool
    host: str
    reason: str
    status: int = 403


def hostname(value: str) -> str | None:
    """Normalised DNS hostname, or ``None`` for an address or anything malformed."""
    name = value.strip().lower().removesuffix(".")
    labels = name.split(".")
    if not name or len(name) > 253 or len(labels) < 2 or labels[-1].isdigit():
        return None
    if not all(_LABEL.match(label) for label in labels):
        return None
    try:
        ipaddress.ip_address(name)
    except ValueError:
        return name
    return None


def decide(head: bytes, allowed: frozenset[str]) -> Decision:
    """Apply the policy to a complete request head (without the blank line)."""
    try:
        text = head.decode("ascii")
    except UnicodeDecodeError:
        return Decision(False, "", "non-ascii request", 400)
    lines = text.split("\r\n")
    parts = lines[0].split(" ")
    if len(parts) != 3 or not parts[2].startswith("HTTP/1."):
        return Decision(False, "", "malformed request line", 400)
    method, target, _ = parts
    if method != "CONNECT":
        return Decision(False, "", f"method {method[:16]} refused")
    if target.startswith("[") or target.count(":") != 1:
        return Decision(False, "", "target is not host:port")
    raw_host, _, port = target.partition(":")
    if port != str(ALLOWED_PORT):
        return Decision(False, "", "port refused")
    host = hostname(raw_host)
    if host is None:
        return Decision(False, "", "target is not a hostname")
    if host not in allowed:
        return Decision(False, host, "host not listed")
    hosts_seen = []
    for line in lines[1:]:
        name, sep, value = line.partition(":")
        if not sep:
            return Decision(False, host, "malformed header", 400)
        if name.strip().lower() == "host":
            hosts_seen.append(value.strip())
    if len(hosts_seen) > 1:
        return Decision(False, host, "several Host headers")
    if hosts_seen:
        header_host, _, header_port = hosts_seen[0].rpartition(":")
        if not header_host or header_port != str(ALLOWED_PORT):
            header_host = hosts_seen[0]
        if hostname(header_host) != host:
            return Decision(False, host, "Host header does not match the target")
    return Decision(True, host, "allowed", 200)


def resolve_with_dns(host: str, port: int) -> Sequence[str]:
    infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    return [str(info[4][0]) for info in infos]


def connect_tcp(address: str, port: int, timeout: float) -> socket.socket:
    return socket.create_connection((address, port), timeout=timeout)


class _Server(socketserver.ThreadingTCPServer):
    daemon_threads = True
    allow_reuse_address = True


class EgressProxy:
    """The proxy server. ``serve_forever`` blocks; ``shutdown`` stops it."""

    def __init__(
        self,
        listen: tuple[str, int],
        allowed_hosts: Iterable[str],
        clients: Iterable[ipaddress.IPv4Network | ipaddress.IPv6Network],
        *,
        resolver: Resolver = resolve_with_dns,
        connector: Connector = connect_tcp,
        log: IO[str] | None = None,
        idle_timeout: float = IDLE_TIMEOUT,
    ) -> None:
        names = {hostname(host) for host in allowed_hosts}
        if None in names or not names:
            raise ValueError("allowed hosts must be valid hostnames")
        self.allowed = frozenset(name for name in names if name is not None)
        self.clients = tuple(clients)
        self._resolver = resolver
        self._connector = connector
        self._log_stream = log or sys.stderr
        self._log_lock = threading.Lock()
        self._idle_timeout = idle_timeout
        proxy = self

        class Handler(socketserver.BaseRequestHandler):
            def handle(self) -> None:
                proxy._handle(self.request, self.client_address)

        self._server = _Server(listen, Handler)

    @property
    def address(self) -> tuple[str, int]:
        host, port = self._server.server_address[:2]
        return str(host), int(port)

    def serve_forever(self) -> None:
        self.log("READY", listen=f"{self.address[0]}:{self.address[1]}")
        self._server.serve_forever(poll_interval=0.2)

    def shutdown(self) -> None:
        self._server.shutdown()
        self._server.server_close()

    def log(self, event: str, **fields: str) -> None:
        detail = " ".join(f"{key}={_printable(value)}" for key, value in fields.items())
        with self._log_lock:
            self._log_stream.write(f"ilmar-egress {event} {detail}\n")
            self._log_stream.flush()

    # -- one connection ------------------------------------------------------

    def _handle(self, client: socket.socket, peer: tuple[str, int]) -> None:
        if not self._client_allowed(peer[0]):
            self.log("REFUSE-CLIENT", client=peer[0])
            return
        head, rest = _read_head(client)
        if head is None:
            self.log("DENY", reason="incomplete or oversized request head")
            _reply(client, 400, "Bad Request")
            return
        decision = decide(head, self.allowed)
        if not decision.allowed:
            self.log("DENY", reason=decision.reason, target=_request_target(head))
            _reply(
                client, decision.status, "Forbidden" if decision.status == 403 else "Bad Request"
            )
            return
        upstream = self._open_upstream(decision.host)
        if upstream is None:
            _reply(client, 502, "Bad Gateway")
            return
        with upstream:
            client.sendall(b"HTTP/1.1 200 Connection established\r\n\r\n")
            if rest:
                upstream.sendall(rest)
            _relay(client, upstream, self._idle_timeout)

    def _client_allowed(self, address: str) -> bool:
        try:
            ip = ipaddress.ip_address(address)
        except ValueError:
            return False
        return any(ip in network for network in self.clients)

    def _open_upstream(self, host: str) -> socket.socket | None:
        self.log("RESOLVE", host=host)
        try:
            addresses = list(self._resolver(host, ALLOWED_PORT))
        except OSError as error:
            self.log("FAIL", host=host, reason=f"resolve: {error.__class__.__name__}")
            return None
        for address in addresses:
            try:
                upstream = self._connector(address, ALLOWED_PORT, CONNECT_TIMEOUT)
            except OSError:
                continue
            self.log("ALLOW", host=host, address=address)
            return upstream
        self.log("FAIL", host=host, reason="no address reachable")
        return None


def _read_head(client: socket.socket) -> tuple[bytes | None, bytes]:
    client.settimeout(HEAD_TIMEOUT)
    data = b""
    try:
        while b"\r\n\r\n" not in data:
            if len(data) > MAX_HEAD_BYTES:
                return None, b""
            chunk = client.recv(4096)
            if not chunk:
                return None, b""
            data += chunk
    except OSError:
        return None, b""
    head, _, rest = data.partition(b"\r\n\r\n")
    if len(head) > MAX_HEAD_BYTES:
        return None, b""
    return head, rest


def _relay(a: socket.socket, b: socket.socket, idle_timeout: float) -> None:
    a.setblocking(False)
    b.setblocking(False)
    peers = {a: b, b: a}
    open_ends = {a, b}
    while open_ends:
        readable, _, _ = select.select(list(open_ends), [], [], idle_timeout)
        if not readable:
            return  # idle
        for source in readable:
            try:
                data = source.recv(65536)
            except BlockingIOError:
                continue
            except OSError:
                return
            target = peers[source]
            if not data:
                open_ends.discard(source)
                try:
                    target.shutdown(socket.SHUT_WR)
                except OSError:
                    return
                continue
            try:
                target.setblocking(True)
                target.sendall(data)
                target.setblocking(False)
            except OSError:
                return


def _reply(client: socket.socket, status: int, text: str) -> None:
    with contextlib.suppress(OSError):
        client.sendall(
            f"HTTP/1.1 {status} {text}\r\nContent-Length: 0\r\nConnection: close\r\n\r\n".encode()
        )


def _request_target(head: bytes) -> str:
    first = head.split(b"\r\n", 1)[0].decode("ascii", errors="replace").split(" ")
    return first[1] if len(first) > 1 else ""


def _printable(value: str) -> str:
    """One log token: spaces become ``_``, other non-printables ``?``."""
    return _PRINTABLE.sub("?", value.replace(" ", "_"))[:200]


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--listen", default="0.0.0.0:3128")
    parser.add_argument("--allow", required=True, help="comma-separated hostnames")
    parser.add_argument("--clients", required=True, help="comma-separated client networks")
    args = parser.parse_args(argv)
    host, _, port = args.listen.rpartition(":")
    proxy = EgressProxy(
        (host, int(port)),
        args.allow.split(","),
        [ipaddress.ip_network(net, strict=False) for net in args.clients.split(",")],
    )
    proxy.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
