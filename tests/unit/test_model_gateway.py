# SPDX-License-Identifier: Apache-2.0
"""The model gateway sidecar, in process (ADR-0037 §1, #111 P4).

A stand-in provider runs on loopback over plain HTTP; the gateway forwards to
it. The real-Podman topology (a separate sidecar on the sandbox's private
network) is tested in ``tests/integration/test_podman_gateway.py``.
"""

from __future__ import annotations

import hashlib
import http.client
import io
import ipaddress
import json
import threading
from collections.abc import Iterator
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from ilmar.adapters.podman import model_gateway as mg

TOKEN = "ilmar-throwaway-token-0123456789abcdef"
KEY = "sk-ant-real-key-never-in-the-sandbox-42"
LOOPBACK = (ipaddress.ip_network("127.0.0.0/8"),)


@dataclass
class Provider:
    """Records what reached it; answers with a short SSE stream."""

    seen: list[dict[str, object]] = field(default_factory=list)
    hold: threading.Event | None = None
    entered: threading.Event = field(default_factory=threading.Event)

    def handler(self) -> type[BaseHTTPRequestHandler]:
        provider = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args: object) -> None:
                pass

            def do_POST(self) -> None:
                body = self.rfile.read(int(self.headers["content-length"]))
                provider.seen.append(
                    {"path": self.path, "headers": dict(self.headers.items()), "body": body}
                )
                provider.entered.set()
                if provider.hold is not None:
                    provider.hold.wait(5)
                payload = b"event: message_stop\ndata: {}\n\n"
                self.send_response(200)
                self.send_header("content-type", "text/event-stream")
                self.send_header("request-id", "req_1")
                self.send_header("anthropic-ratelimit-requests-remaining", "9")
                self.send_header("set-cookie", "tracking=1")
                self.send_header("content-length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

        return Handler


@dataclass
class Running:
    gateway: mg.Gateway
    provider: Provider
    log: io.StringIO

    def post(
        self,
        path: str = "/v1/messages?beta=true",
        *,
        body: bytes = b'{"model":"m","max_tokens":1}',
        headers: dict[str, str] | None = None,
        method: str = "POST",
    ) -> tuple[int, dict[str, str], bytes]:
        host, port = self.gateway.address
        connection = http.client.HTTPConnection(host, port, timeout=10)
        sent = {"x-api-key": TOKEN, "content-type": "application/json"}
        sent.update(headers or {})
        connection.request(method, path, body=body, headers=sent)
        response = connection.getresponse()
        data = response.read()
        connection.close()
        return response.status, {k.lower(): v for k, v in response.getheaders()}, data


def start(provider: Provider, **limits: int) -> Iterator[Running]:
    upstream = ThreadingHTTPServer(("127.0.0.1", 0), provider.handler())
    threading.Thread(target=upstream.serve_forever, daemon=True).start()
    config = mg.GatewayConfig(
        token_sha256=hashlib.sha256(TOKEN.encode()).hexdigest(),
        key=KEY,
        upstream=mg.Upstream("http", "127.0.0.1", upstream.server_address[1]),
        clients=LOOPBACK,
        **limits,
    )
    log = io.StringIO()
    gateway = mg.Gateway(("127.0.0.1", 0), config, log=log)
    threading.Thread(target=gateway.serve_forever, daemon=True).start()
    try:
        yield Running(gateway, provider, log)
    finally:
        gateway.shutdown()
        upstream.shutdown()
        upstream.server_close()


@pytest.fixture
def running() -> Iterator[Running]:
    yield from start(Provider())


def test_a_request_is_forwarded_with_the_real_key_and_never_the_token(running: Running) -> None:
    status, headers, data = running.post(
        headers={
            "anthropic-version": "2023-06-01",
            "anthropic-beta": "x",
            "x-stainless-lang": "js",
            "cookie": "c=1",
            "proxy-authorization": "Basic Zm9v",
            "x-forwarded-for": "10.0.0.1",
        }
    )
    assert status == 200
    assert data == b"event: message_stop\ndata: {}\n\n"
    (seen,) = running.provider.seen
    sent = {k.lower(): v for k, v in seen["headers"].items()}  # type: ignore[attr-defined]
    assert sent["x-api-key"] == KEY
    assert seen["path"] == "/v1/messages?beta=true"
    assert seen["body"] == b'{"model":"m","max_tokens":1}'
    for dropped in ("authorization", "cookie", "proxy-authorization", "x-forwarded-for"):
        assert dropped not in sent
    assert sent["anthropic-version"] == "2023-06-01"
    assert sent["x-stainless-lang"] == "js"
    assert TOKEN not in json.dumps(sent)
    assert headers["request-id"] == "req_1"
    assert headers["anthropic-ratelimit-requests-remaining"] == "9"
    assert "set-cookie" not in headers


def test_a_bearer_token_is_accepted_too(running: Running) -> None:
    status, _, _ = running.post(headers={"x-api-key": "", "authorization": f"Bearer {TOKEN}"})
    assert status == 401  # an empty x-api-key next to it is a wrong credential
    host, port = running.gateway.address
    connection = http.client.HTTPConnection(host, port, timeout=10)
    connection.request("POST", "/v1/messages", body=b"{}",
                       headers={"authorization": f"Bearer {TOKEN}"})  # fmt: skip
    assert connection.getresponse().status == 200


@pytest.mark.parametrize(
    "headers",
    [
        {"x-api-key": "wrong"},
        {"x-api-key": KEY},  # not even the real key
        {"x-api-key": TOKEN, "authorization": "Bearer wrong"},
        {"x-api-key": TOKEN, "authorization": f"Basic {TOKEN}"},
    ],
)
def test_any_other_credential_is_refused_locally(running: Running, headers: dict[str, str]) -> None:
    status, _, data = running.post(headers=headers)
    assert status == 401
    assert json.loads(data)["error"]["type"] == "authentication_error"
    assert running.provider.seen == []


def test_no_credential_is_refused(running: Running) -> None:
    host, port = running.gateway.address
    connection = http.client.HTTPConnection(host, port, timeout=10)
    connection.request("POST", "/v1/messages", body=b"{}")
    assert connection.getresponse().status == 401
    assert running.provider.seen == []


@pytest.mark.parametrize(
    ("method", "path", "status"),
    [
        ("POST", "/v1/complete", 404),
        ("POST", "/v1/messages/batches", 404),
        ("POST", "/v1/files", 404),
        ("POST", "/v1/messages?beta=true&x=1", 404),
        ("POST", "/v1/messages/../files", 404),
        ("GET", "/v1/models", 405),
        ("PUT", "/v1/messages", 405),
    ],
)
def test_only_the_messages_endpoints_are_served(
    running: Running, method: str, path: str, status: int
) -> None:
    assert running.post(path, method=method)[0] == status
    assert running.provider.seen == []


def test_count_tokens_is_served(running: Running) -> None:
    assert running.post("/v1/messages/count_tokens?beta=true")[0] == 200


def test_an_oversized_body_is_refused() -> None:
    for running in start(Provider(), max_request_bytes=10):
        status, _, data = running.post(body=b"x" * 11)
        assert status == 413
        assert json.loads(data)["error"]["type"] == "request_too_large"
        assert running.post(body=b"x" * 10)[0] == 200
        assert len(running.provider.seen) == 1


def test_a_chunked_body_is_refused(running: Running) -> None:
    host, port = running.gateway.address
    connection = http.client.HTTPConnection(host, port, timeout=10)
    connection.request("POST", "/v1/messages", body=iter([b"{}"]),
                       headers={"x-api-key": TOKEN})  # fmt: skip  # chunked
    assert connection.getresponse().status == 400
    assert running.provider.seen == []


def test_a_client_outside_the_sandbox_network_is_refused() -> None:
    provider = Provider()
    for running in start(provider):
        object.__setattr__(
            running.gateway.config, "clients", (ipaddress.ip_network("10.99.0.0/16"),)
        )
        assert running.post()[0] == 403
        assert provider.seen == []


def test_requests_in_flight_are_limited() -> None:
    provider = Provider(hold=threading.Event())
    for running in start(provider, max_in_flight=1):
        first: list[int] = []

        def post_first(running: Running = running, first: list[int] = first) -> None:
            first.append(running.post()[0])

        thread = threading.Thread(target=post_first)
        thread.start()
        assert provider.entered.wait(5)
        status, _, data = running.post()
        assert status == 429
        assert json.loads(data)["error"]["type"] == "rate_limit_error"
        provider.hold.set()  # type: ignore[union-attr]
        thread.join(5)
        assert first == [200]
        assert len(provider.seen) == 1


def test_an_unreachable_provider_is_a_local_502() -> None:
    config = mg.GatewayConfig(
        token_sha256=hashlib.sha256(TOKEN.encode()).hexdigest(),
        key=KEY,
        upstream=mg.Upstream("http", "127.0.0.1", 9),  # discard: nothing listens
        clients=LOOPBACK,
    )
    gateway = mg.Gateway(("127.0.0.1", 0), config, log=io.StringIO())
    threading.Thread(target=gateway.serve_forever, daemon=True).start()
    try:
        status, _, data = Running(gateway, Provider(), io.StringIO()).post()
        assert status == 502
        assert json.loads(data)["error"]["type"] == "api_error"
    finally:
        gateway.shutdown()


def test_the_log_holds_no_credential_header_or_body(running: Running) -> None:
    running.post(body=b'{"secret-prompt":"hello"}')
    running.post(headers={"x-api-key": "wrong"})
    text = running.log.getvalue()
    assert "ilmar-gateway READY" in text
    assert "REQUEST POST /v1/messages 200" in text
    assert "refused=authentication_error" in text
    for leaked in (KEY, TOKEN, "secret-prompt", "wrong"):
        assert leaked not in text
    assert KEY not in repr(running.gateway.config)


# -- configuration --------------------------------------------------------------------


def test_the_upstream_must_be_https_unless_plain_http_is_allowed_for_tests() -> None:
    assert mg.Upstream.parse("https://api.anthropic.com", allow_plain_http=False) == (
        mg.Upstream("https", "api.anthropic.com", 443)
    )
    with pytest.raises(ValueError, match="https"):
        mg.Upstream.parse("http://api.anthropic.com", allow_plain_http=False)
    for bad in ("https://h/v1", "https://u:p@h", "ftp://h", "https://h?x=1"):
        with pytest.raises(ValueError, match="upstream"):
            mg.Upstream.parse(bad, allow_plain_http=True)


@pytest.mark.parametrize("content", ["", "\n", "two\nlines\n", "nul\0"])
def test_a_bad_key_file_is_refused(tmp_path: Path, content: str) -> None:
    (tmp_path / "key").write_text(content)
    with pytest.raises(ValueError, match="one non-empty line"):
        mg.read_key(str(tmp_path / "key"))


def test_the_key_file_is_read_as_one_line(tmp_path: Path) -> None:
    (tmp_path / "key").write_text(KEY + "\n")
    assert mg.read_key(str(tmp_path / "key")) == KEY


def test_the_gateway_is_given_a_digest_never_the_token() -> None:
    with pytest.raises(ValueError, match="token_sha256"):
        mg.GatewayConfig(token_sha256=TOKEN, key=KEY, upstream=mg.Upstream("https", "h", 443),
                         clients=LOOPBACK)  # fmt: skip


def test_no_configuration_can_route_an_agent_through_the_gateway_yet() -> None:
    """Until the worst-case cost reservation (ADR-0037 §2, P5) lands, nothing
    that builds a deployment from configuration may choose ``GATEWAY``."""
    src = Path(__file__).resolve().parents[2] / "src" / "ilmar"
    for module in ("config.py", "adapters/builtin.py", "adapters/registry.py", "cli.py"):
        assert "GATEWAY" not in (src / module).read_text(), module
        assert "GatewaySettings" not in (src / module).read_text(), module
