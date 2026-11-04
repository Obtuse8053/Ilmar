# SPDX-License-Identifier: Apache-2.0
"""The egress proxy's policy and behaviour (ADR-0011 §5, ADR-0015), on localhost."""

from __future__ import annotations

import ast
import io
import ipaddress
import socket
import threading
from collections.abc import Iterator, Sequence
from pathlib import Path

import pytest

from ilmar.adapters.podman import egress_proxy
from ilmar.adapters.podman.egress_proxy import Decision, EgressProxy, decide

ALLOWED = frozenset({"api.example.com"})
LOCAL = [ipaddress.ip_network("127.0.0.0/8")]


def head(request_line: str, *headers: str) -> bytes:
    return "\r\n".join([request_line, *headers]).encode()


# -- the policy ---------------------------------------------------------------


def test_allowed_connect() -> None:
    assert decide(head("CONNECT api.example.com:443 HTTP/1.1"), ALLOWED) == Decision(
        True, "api.example.com", "allowed", 200
    )
    assert decide(
        head("CONNECT API.Example.COM.:443 HTTP/1.1", "Host: api.example.com:443"), ALLOWED
    ).allowed
    assert decide(
        head("CONNECT api.example.com:443 HTTP/1.0", "Host: api.example.com"), ALLOWED
    ).allowed


@pytest.mark.parametrize(
    ("request_head", "reason"),
    [
        (head("GET http://api.example.com/ HTTP/1.1"), "method"),
        (head("GET / HTTP/1.1", "Host: api.example.com"), "method"),
        (head("POST https://api.example.com/ HTTP/1.1"), "method"),
        (head("CONNECT api.example.com:8443 HTTP/1.1"), "port"),
        (head("CONNECT api.example.com:80 HTTP/1.1"), "port"),
        (head("CONNECT api.example.com HTTP/1.1"), "host:port"),
        (head("CONNECT other.example.com:443 HTTP/1.1"), "not listed"),
        (head("CONNECT example.com:443 HTTP/1.1"), "not listed"),
        (head("CONNECT api.example.com.evil.test:443 HTTP/1.1"), "not listed"),
        (head("CONNECT 93.184.216.34:443 HTTP/1.1"), "not a hostname"),
        (head("CONNECT 2130706433:443 HTTP/1.1"), "not a hostname"),
        (head("CONNECT 0x7f.0.0.1:443 HTTP/1.1"), "not a hostname"),
        (head("CONNECT [2001:db8::1]:443 HTTP/1.1"), "host:port"),
        (head("CONNECT [::ffff:127.0.0.1]:443 HTTP/1.1"), "host:port"),
        (head("CONNECT api.example.com:443 HTTP/1.1", "Host: evil.test"), "Host header"),
        (head("CONNECT api.example.com:443 HTTP/1.1", "Host: evil.test:443"), "Host header"),
        (head("CONNECT api.example.com:443 HTTP/1.1", "Host: api.example.com:80"), "Host header"),
        (
            head(
                "CONNECT api.example.com:443 HTTP/1.1",
                "Host: api.example.com",
                "Host: evil.test",
            ),
            "several Host",
        ),
        (head("CONNECT api.example.com:443"), "malformed"),
        (head("CONNECT api.example.com:443 HTTP/1.1", "no-colon-header"), "malformed"),
        ("CONNECT api.example.com:443 HTTP/1.1\r\nX: \xff".encode("latin-1"), "non-ascii"),
    ],
)
def test_refused_requests(request_head: bytes, reason: str) -> None:
    decision = decide(request_head, ALLOWED)
    assert not decision.allowed
    assert reason in decision.reason


def test_allowed_hosts_must_be_hostnames() -> None:
    for bad in (["1.2.3.4"], ["localhost"], []):
        with pytest.raises(ValueError, match="hostnames"):
            EgressProxy(("127.0.0.1", 0), bad, LOCAL)


def test_proxy_module_is_standalone() -> None:
    """It runs inside the sidecar without Ilmar installed: standard library only."""
    tree = ast.parse(Path(egress_proxy.__file__).read_text())
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported |= {alias.name.split(".")[0] for alias in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    assert "ilmar" not in imported
    assert imported <= set(__import__("sys").stdlib_module_names) | {"__future__"}


# -- the server, end to end on localhost -------------------------------------------------


class Upstream:
    """Stands in for the allowed host: echoes with a prefix."""

    def __init__(self) -> None:
        self.socket = socket.create_server(("127.0.0.1", 0))
        self.port = self.socket.getsockname()[1]
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self) -> None:
        while True:
            try:
                connection, _ = self.socket.accept()
            except OSError:
                return
            with connection:
                data = connection.recv(1024)
                connection.sendall(b"upstream:" + data)


@pytest.fixture
def upstream() -> Iterator[Upstream]:
    server = Upstream()
    yield server
    server.socket.close()


class Running:
    def __init__(self, proxy: EgressProxy, log: io.StringIO, calls: list[str]) -> None:
        self.proxy, self.log, self.calls = proxy, log, calls

    def request(self, raw: bytes, payload: bytes = b"") -> bytes:
        with socket.create_connection(self.proxy.address, timeout=5) as client:
            client.sendall(raw + payload)
            client.shutdown(socket.SHUT_WR)
            chunks = []
            while chunk := client.recv(4096):
                chunks.append(chunk)
            return b"".join(chunks)


def start(
    upstream: Upstream,
    *,
    clients: Sequence[ipaddress.IPv4Network | ipaddress.IPv6Network] = tuple(LOCAL),
) -> Running:
    log = io.StringIO()
    calls: list[str] = []

    def resolver(host: str, port: int) -> list[str]:
        calls.append(f"resolve {host}:{port}")
        return ["127.0.0.1"]

    def connector(address: str, port: int, timeout: float) -> socket.socket:
        calls.append(f"connect {address}:{port}")
        assert port == 443  # the policy port is what the proxy dials
        return socket.create_connection((address, upstream.port), timeout=timeout)

    proxy = EgressProxy(
        ("127.0.0.1", 0),
        ["api.example.com"],
        clients,
        resolver=resolver,
        connector=connector,
        log=log,
    )
    threading.Thread(target=proxy.serve_forever, daemon=True).start()
    return Running(proxy, log, calls)


@pytest.fixture
def running(upstream: Upstream) -> Iterator[Running]:
    proxy = start(upstream)
    yield proxy
    proxy.proxy.shutdown()


def test_allowed_connect_is_tunnelled(running: Running) -> None:
    reply = running.request(
        b"CONNECT api.example.com:443 HTTP/1.1\r\nHost: api.example.com\r\n\r\n", b"hello"
    )
    assert reply == b"HTTP/1.1 200 Connection established\r\n\r\nupstream:hello"
    assert running.calls == ["resolve api.example.com:443", "connect 127.0.0.1:443"]
    assert "ilmar-egress ALLOW host=api.example.com address=127.0.0.1" in running.log.getvalue()


@pytest.mark.parametrize(
    "raw",
    [
        b"CONNECT other.example.com:443 HTTP/1.1\r\n\r\n",
        b"CONNECT 127.0.0.1:443 HTTP/1.1\r\n\r\n",
        b"CONNECT [::1]:443 HTTP/1.1\r\n\r\n",
        b"CONNECT api.example.com:22 HTTP/1.1\r\n\r\n",
        b"GET http://api.example.com/ HTTP/1.1\r\nHost: api.example.com\r\n\r\n",
        b"CONNECT api.example.com:443 HTTP/1.1\r\nHost: other.example.com\r\n\r\n",
    ],
)
def test_refused_requests_are_never_resolved_or_connected(running: Running, raw: bytes) -> None:
    reply = running.request(raw)
    assert reply.startswith(b"HTTP/1.1 403 ")
    assert running.calls == []
    log = running.log.getvalue()
    assert "ilmar-egress DENY" in log
    assert "RESOLVE" not in log


def test_oversized_or_incomplete_heads_are_refused(running: Running) -> None:
    assert running.request(
        b"CONNECT api.example.com:443 HTTP/1.1\r\nX: " + b"a" * 10000
    ).startswith(b"HTTP/1.1 400 ")
    assert running.request(b"CONNECT api.example.com:443 HTTP/1.1\r\n").startswith(b"HTTP/1.1 400 ")
    assert running.calls == []


def test_clients_outside_the_sandbox_network_are_refused(upstream: Upstream) -> None:
    proxy = start(upstream, clients=[ipaddress.ip_network("10.89.0.0/24")])
    try:
        reply = proxy.request(b"CONNECT api.example.com:443 HTTP/1.1\r\n\r\n")
    finally:
        proxy.proxy.shutdown()
    assert reply == b""
    assert proxy.calls == []
    assert "REFUSE-CLIENT client=127.0.0.1" in proxy.log.getvalue()


def test_unreachable_upstream_is_a_502(upstream: Upstream) -> None:
    log = io.StringIO()

    def connector(address: str, port: int, timeout: float) -> socket.socket:
        raise ConnectionRefusedError

    proxy = EgressProxy(
        ("127.0.0.1", 0),
        ["api.example.com"],
        LOCAL,
        resolver=lambda host, port: ["192.0.2.1", "192.0.2.2"],
        connector=connector,
        log=log,
    )
    threading.Thread(target=proxy.serve_forever, daemon=True).start()
    try:
        reply = Running(proxy, log, []).request(b"CONNECT api.example.com:443 HTTP/1.1\r\n\r\n")
    finally:
        proxy.shutdown()
    assert reply.startswith(b"HTTP/1.1 502 ")
    assert "FAIL host=api.example.com reason=no_address_reachable" in log.getvalue()


def test_log_fields_are_sanitised(running: Running) -> None:
    running.request(b"CONNECT evil\x1b[31m.test:443 HTTP/1.1\r\n\r\n")
    assert "\x1b" not in running.log.getvalue()
