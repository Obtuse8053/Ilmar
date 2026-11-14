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
import os
import re
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from ilmar.adapters.podman import model_gateway as mg

MODEL = "claude-opus-5-5"
POLICY = json.dumps({
    "prices": {MODEL: {"input": 5_000_000, "output": 25_000_000, "cache_write_5m": 6_250_000,
                       "cache_write_1h": 10_000_000, "cache_read": 500_000}},
    "allowed_betas": ["claude-code-20250219"],
})  # fmt: skip
BUDGET = 10_000_000  # ten dollars
USER = {"role": "user", "content": "Reply with OK."}
BODY = json.dumps(
    {"model": MODEL, "max_tokens": 2_000, "stream": True, "messages": [USER]}
).encode()


def sse(*events: dict[str, object]) -> bytes:
    return b"".join(
        b"event: " + str(e.get("type")).encode() + b"\ndata: " + json.dumps(e).encode() + b"\n\n"
        for e in events
    )


GOOD = sse(
    {"type": "message_start", "message": {"usage": {"input_tokens": 12, "output_tokens": 1}}},
    {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
    {"type": "message_delta", "usage": {"output_tokens": 3}},
    {"type": "message_stop"},
)
TOKEN = "ilmar-throwaway-token-0123456789abcdef"
KEY = "sk-ant-real-key-never-in-the-sandbox-42"
LOOPBACK = (ipaddress.ip_network("127.0.0.0/8"),)


@dataclass
class Provider:
    """Records what reached it; answers with a short SSE stream."""

    seen: list[dict[str, object]] = field(default_factory=list)
    hold: threading.Event | None = None
    payload: bytes = GOOD
    status: int = 200
    entered: threading.Event = field(default_factory=threading.Event)

    def handler(self) -> type[BaseHTTPRequestHandler]:
        provider = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args: object) -> None:
                pass

            def do_POST(self) -> None:
                body = self.rfile.read(int(self.headers["content-length"]))
                provider.seen.append({
                    "path": self.path, "headers": dict(self.headers.items()), "body": body,
                    "betas": self.headers.get_all("anthropic-beta") or [],
                })  # fmt: skip
                provider.entered.set()
                if provider.hold is not None:
                    provider.hold.wait(5)
                payload = provider.payload
                self.send_response(provider.status)
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
        body: bytes = BODY,
        headers: dict[str, str] | None = None,
        method: str = "POST",
        betas: tuple[str, ...] = (),
    ) -> tuple[int, dict[str, str], bytes]:
        host, port = self.gateway.address
        connection = http.client.HTTPConnection(host, port, timeout=10)
        sent = {"x-api-key": TOKEN, "content-type": "application/json"}
        sent.update(headers or {})
        connection.putrequest(method, path, skip_accept_encoding=True)
        for name, value in sent.items():
            connection.putheader(name, value)
        for beta in betas:  # each its own header line, as a client may send
            connection.putheader("anthropic-beta", beta)
        connection.putheader("content-length", str(len(body)))
        connection.endheaders(body)
        response = connection.getresponse()
        data = response.read()
        connection.close()
        return response.status, {k.lower(): v for k, v in response.getheaders()}, data


def start(
    provider: Provider, *, policy: str = POLICY, budget: int = BUDGET, **limits: int
) -> Iterator[Running]:
    upstream = ThreadingHTTPServer(("127.0.0.1", 0), provider.handler())
    threading.Thread(target=upstream.serve_forever, daemon=True).start()
    config = mg.GatewayConfig(
        token_sha256=hashlib.sha256(TOKEN.encode()).hexdigest(),
        key=KEY,
        upstream=mg.Upstream("http", "127.0.0.1", upstream.server_address[1]),
        clients=LOOPBACK,
        policy=policy,
        budget=budget,
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
            "anthropic-beta": "claude-code-20250219",
            "x-stainless-lang": "js",
            "cookie": "c=1",
            "proxy-authorization": "Basic Zm9v",
            "x-forwarded-for": "10.0.0.1",
        }
    )
    assert status == 200
    assert data == GOOD
    (seen,) = running.provider.seen
    sent = {k.lower(): v for k, v in seen["headers"].items()}  # type: ignore[attr-defined]
    assert sent["x-api-key"] == KEY
    assert seen["path"] == "/v1/messages?beta=true"
    assert seen["body"] == BODY
    assert seen["betas"] == ["claude-code-20250219"]
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
    connection.request("POST", "/v1/messages", body=BODY,
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
    for running in start(Provider(), max_request_bytes=len(BODY)):
        status, _, data = running.post(body=BODY + b" ")
        assert status == 413
        assert json.loads(data)["error"]["type"] == "request_too_large"
        assert running.post(body=BODY)[0] == 200
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
        policy=POLICY,
        budget=BUDGET,
    )
    log = io.StringIO()
    gateway = mg.Gateway(("127.0.0.1", 0), config, log=log)
    threading.Thread(target=gateway.serve_forever, daemon=True).start()
    try:
        client = Running(gateway, Provider(), log)
        status, _, data = client.post()
        assert status == 502
        assert json.loads(data)["error"]["type"] == "api_error"
        # It may have reached the provider: the whole reservation is charged and
        # the attempt halts (fail closed on unknown billing).
        assert "reason=provider_unreachable" in log.getvalue()
        assert gateway.budget.halted
        status, _, data = client.post()
        assert (status, json.loads(data)["error"]["type"]) == (429, "budget_exceeded")
    finally:
        gateway.shutdown()


def test_the_log_holds_no_credential_header_or_body(running: Running) -> None:
    running.post(body=BODY.replace(b"Reply with OK.", b"secret-prompt hello"))
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


def hand_over(fifo: Path, data: bytes) -> threading.Thread:
    """The coordinator's side: once the gateway announces the FIFO, write into it."""

    def write() -> None:
        while not fifo.exists():
            time.sleep(0.01)
        descriptor = os.open(fifo, os.O_WRONLY)
        os.write(descriptor, data)
        os.close(descriptor)

    writer = threading.Thread(target=write, daemon=True)
    writer.start()
    return writer


def test_the_key_comes_through_a_fifo_that_is_removed_after(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    fifo = tmp_path / "key"
    writer = hand_over(fifo, (KEY + "\n").encode())
    assert mg.read_key(str(fifo)) == KEY
    writer.join(5)
    assert not fifo.exists()  # gone once read: nothing is left to find
    assert capsys.readouterr().out == mg.AWAITING_KEY + "\n"


@pytest.mark.parametrize("content", [b"", b"\n", b"two\nlines\n", b"nul\0", b"x" * 5000])
def test_a_bad_key_is_refused_and_the_fifo_still_removed(tmp_path: Path, content: bytes) -> None:
    fifo = tmp_path / "key"
    hand_over(fifo, content)
    with pytest.raises(ValueError, match="key"):
        mg.read_key(str(fifo))
    assert not fifo.exists()


def test_the_gateway_is_given_a_digest_never_the_token() -> None:
    with pytest.raises(ValueError, match="token_sha256"):
        mg.GatewayConfig(token_sha256=TOKEN, key=KEY, upstream=mg.Upstream("https", "h", 443),
                         clients=LOOPBACK, policy=POLICY, budget=BUDGET)  # fmt: skip


def test_no_configuration_can_route_an_agent_through_the_gateway_yet() -> None:
    """Until the worst-case cost reservation (ADR-0037 §2, P5) lands, nothing
    that builds a deployment from configuration may choose ``GATEWAY``."""
    src = Path(__file__).resolve().parents[2] / "src" / "ilmar"
    for module in ("config.py", "adapters/builtin.py", "adapters/registry.py", "cli.py"):
        assert "GATEWAY" not in (src / module).read_text(), module
        assert "GatewaySettings" not in (src / module).read_text(), module


# -- spending controls (P5 part 2) ----------------------------------------------------


def request_body(**fields: object) -> bytes:
    base: dict[str, object] = json.loads(BODY)
    base.update(fields)
    return json.dumps(base).encode()


def charges(log: io.StringIO) -> list[str]:
    return [line for line in log.getvalue().splitlines() if line.startswith("CHARGE ")]


def test_max_tokens_is_lowered_to_the_policy_before_forwarding(running: Running) -> None:
    status, _, _ = running.post(body=request_body(max_tokens=128_000))
    assert status == 200
    (seen,) = running.provider.seen
    assert json.loads(seen["body"])["max_tokens"] == 32_000  # type: ignore[arg-type]
    assert "max_tokens_from=128000" in running.log.getvalue()


def test_max_tokens_is_lowered_to_what_the_budget_affords() -> None:
    provider = Provider()
    for running in start(provider, budget=200_000):  # twenty cents
        assert running.post(body=request_body(max_tokens=32_000))[0] == 200
        sent = json.loads(provider.seen[0]["body"])["max_tokens"]  # type: ignore[arg-type]
        assert 1_024 <= sent < 8_000  # 25 dollars per million output tokens
        (line,) = charges(running.log)
        assert "reason=reported" in line
        assert "halted=false" in line


def test_a_request_the_budget_cannot_cover_is_never_forwarded() -> None:
    provider = Provider()
    for running in start(provider, budget=20_000):  # two cents: below 1024 tokens
        status, _, data = running.post()
        assert (status, json.loads(data)["error"]["type"]) == (429, "budget_exceeded")
        assert provider.seen == []


def test_a_good_report_is_charged_from_usage_and_the_attempt_goes_on(running: Running) -> None:
    for _ in range(3):
        assert running.post()[0] == 200
    lines = charges(running.log)
    assert len(lines) == 3
    assert all("reason=reported" in line and "halted=false" in line for line in lines)
    assert "charged=135 " in lines[0]  # 12 input and 3 output tokens, rounded up


@pytest.mark.parametrize(
    ("payload", "status"),
    [
        (sse({"type": "message_start", "message": {"usage": {"input_tokens": 1}}},
             {"type": "message_stop"}), 200),
        (GOOD.replace(b'"type": "text"', b'"type": "server_tool_use"'), 200),
        (GOOD.replace(b'"output_tokens": 3}', b'"output_tokens": 3, "new_thing": 1}'), 200),
        (b'{"type": "error", "error": {"type": "overloaded_error"}}', 529),
        (b'{"type": "error", "error": {"type": "invalid_request_error"}}', 400),
    ],
)  # fmt: skip
def test_untrusted_usage_or_an_error_halts_and_nothing_more_is_forwarded(
    payload: bytes, status: int
) -> None:
    provider = Provider(payload=payload, status=status)
    for running in start(provider):
        assert running.post()[0] == status
        (line,) = charges(running.log)
        assert "halted=true" in line
        assert "reason=reported" not in line
        for _ in range(3):
            code, _, data = running.post()
            assert (code, json.loads(data)["error"]["type"]) == (429, "budget_exceeded")
        assert len(provider.seen) == 1


def test_every_beta_header_is_read_and_one_refused_value_refuses_the_request(
    running: Running,
) -> None:
    status, _, data = running.post(betas=("claude-code-20250219", "web-search-2025-03-05"))
    assert status == 400
    assert "web-search" in json.loads(data)["error"]["message"]
    assert running.provider.seen == []


def test_kept_betas_are_rebuilt_into_one_header_and_unused_denied_ones_dropped(
    running: Running,
) -> None:
    betas = ("claude-code-20250219", "advanced-tool-use-2025-11-20", "claude-code-20250219")
    assert running.post(betas=betas)[0] == 200
    (seen,) = running.provider.seen
    assert seen["betas"] == ["claude-code-20250219"]
    assert "dropped_betas=advanced-tool-use-2025-11-20" in running.log.getvalue()


def test_a_server_tool_is_refused_before_anything_is_sent(running: Running) -> None:
    body = request_body(tools=[{"type": "web_search_20250305", "name": "web_search"}])
    assert running.post(body=body)[0] == 400
    assert running.provider.seen == []
    assert charges(running.log) == []


def test_counting_tokens_is_checked_but_never_charged(running: Running) -> None:
    body = json.dumps({"model": MODEL, "messages": [{"role": "user", "content": "x"}]})
    assert running.post("/v1/messages/count_tokens", body=body.encode())[0] == 200
    assert charges(running.log) == []
    assert running.gateway.budget.spent() == 0
    bad = json.dumps({"model": MODEL, "messages": [], "tools": [{"type": "web_search_x"}]})
    assert running.post("/v1/messages/count_tokens", body=bad.encode())[0] == 400


def test_the_gateway_refuses_to_start_without_a_valid_policy() -> None:
    with pytest.raises((ValueError, KeyError)):
        mg.Gateway(("127.0.0.1", 0), mg.GatewayConfig(
            token_sha256=hashlib.sha256(TOKEN.encode()).hexdigest(), key=KEY,
            upstream=mg.Upstream("https", "h", 443), clients=LOOPBACK,
            policy=json.dumps({"prices": {}}), budget=BUDGET))  # fmt: skip
    with pytest.raises(ValueError, match="positive"):
        mg.GatewayConfig(token_sha256=hashlib.sha256(TOKEN.encode()).hexdigest(), key=KEY,
                         upstream=mg.Upstream("https", "h", 443), clients=LOOPBACK,
                         policy=POLICY, budget=0)  # fmt: skip


def test_the_charge_log_holds_no_body_or_provider_text() -> None:
    payload = GOOD.replace(b'"output_tokens": 3}', b'"output_tokens": 3, "x y\\u00e9<z>": 1}')
    for running in start(Provider(payload=payload)):
        running.post(body=request_body(messages=[{"role": "user", "content": "secret-prompt"}]))
        (line,) = charges(running.log)
        assert "secret-prompt" not in running.log.getvalue()
        reason = line.split("reason=")[1].split(" ")[0]
        assert re.fullmatch(r"[A-Za-z0-9.:_-]+", reason)


PLAIN_GOOD = json.dumps({"type": "message", "stop_reason": "end_turn",
                         "content": [{"type": "text", "text": "OK"}],
                         "usage": {"input_tokens": 12, "output_tokens": 3}}).encode()  # fmt: skip


@pytest.mark.parametrize(
    ("payload", "status"),
    [
        (GOOD, 500),  # a well-formed report under an error status
        (GOOD, 529),
        (GOOD, 201),  # any status but 200
        (PLAIN_GOOD, 400),
        (b"<html>bad gateway</html>", 502),
        (b"", 503),
        (b"not json at all", 429),
    ],
)
def test_every_non_200_response_halts_whatever_its_body(payload: bytes, status: int) -> None:
    provider = Provider(payload=payload, status=status)
    for running in start(provider):
        assert running.post()[0] == status
        (line,) = charges(running.log)
        reservation = line.split("reservation=")[1].split(" ")[0]
        assert f"charged={reservation} " in line  # the whole reservation is kept
        assert f"reason=provider_status_{status}" in line
        assert "halted=true" in line
        for _ in range(2):
            code, _, data = running.post()
            assert (code, json.loads(data)["error"]["type"]) == (429, "budget_exceeded")
        assert len(provider.seen) == 1  # nothing more reached the provider


@pytest.mark.parametrize(
    "payload",
    [
        GOOD.replace(b"event: message_stop", b"event: message_stop\n\nevent: billing")[:-2],
        GOOD + b'event: message_billing_notice\ndata: {"type": "message_billing_notice"}\n\n',
        GOOD[:-1],  # the final blank line is missing
        GOOD + b"data: {trunc",
    ],
)
def test_a_malformed_or_truncated_stream_halts(payload: bytes) -> None:
    provider = Provider(payload=payload)
    for running in start(provider):
        assert running.post()[0] == 200
        (line,) = charges(running.log)
        reservation = line.split("reservation=")[1].split(" ")[0]
        assert f"charged={reservation} " in line
        assert "halted=true" in line
        assert running.post()[0] == 429
        assert len(provider.seen) == 1


# -- counting requests: never charged, but a provider failure halts (#124 review) ------

COUNT = json.dumps({"model": MODEL, "messages": [USER]}).encode()


def halts(log: io.StringIO) -> list[str]:
    return [line for line in log.getvalue().splitlines() if line.startswith("HALT ")]


@pytest.mark.parametrize("status", [400, 429, 500, 503, 529, 201])
def test_a_failed_counting_request_halts_without_a_charge(status: int) -> None:
    provider = Provider(payload=b'{"type": "error"}', status=status)
    for running in start(provider):
        assert running.post("/v1/messages/count_tokens", body=COUNT)[0] == status
        assert charges(running.log) == []
        assert running.gateway.budget.spent() == 0  # nothing charged
        (line,) = halts(running.log)
        assert f"reason=provider_status_{status}" in line
        assert running.gateway.budget.halted
        for path, body in (("/v1/messages", BODY), ("/v1/messages/count_tokens", COUNT)):
            code, _, data = running.post(path, body=body)
            assert (code, json.loads(data)["error"]["type"]) == (429, "budget_exceeded")
        assert len(provider.seen) == 1  # nothing more reached the provider


def test_an_unreachable_provider_on_counting_halts_without_a_charge() -> None:
    config = mg.GatewayConfig(
        token_sha256=hashlib.sha256(TOKEN.encode()).hexdigest(), key=KEY,
        upstream=mg.Upstream("http", "127.0.0.1", 9), clients=LOOPBACK,
        policy=POLICY, budget=BUDGET,
    )  # fmt: skip
    log = io.StringIO()
    gateway = mg.Gateway(("127.0.0.1", 0), config, log=log)
    threading.Thread(target=gateway.serve_forever, daemon=True).start()
    try:
        client = Running(gateway, Provider(), log)
        assert client.post("/v1/messages/count_tokens", body=COUNT)[0] == 502
        assert charges(log) == []
        assert gateway.budget.spent() == 0
        assert "reason=provider_unreachable" in halts(log)[0]
        code, _, data = client.post()
        assert (code, json.loads(data)["error"]["type"]) == (429, "budget_exceeded")
    finally:
        gateway.shutdown()


def test_a_good_counting_request_neither_charges_nor_halts(running: Running) -> None:
    running.provider.payload = b'{"input_tokens": 12}'
    assert running.post("/v1/messages/count_tokens", body=COUNT)[0] == 200
    assert (charges(running.log), halts(running.log)) == ([], [])
    assert not running.gateway.budget.halted
    running.provider.payload = GOOD
    assert running.post()[0] == 200


# -- closing and the final record (ADR-0039) -----------------------------------------


def record(running: Running) -> dict[str, object]:
    return mg.parse_spend(running.log.getvalue().splitlines())


def test_closing_writes_a_final_record_that_matches_the_lines_before_it(
    running: Running,
) -> None:
    for _ in range(2):
        assert running.post()[0] == 200
    assert record(running)["closed"] is False  # no final record yet: maybe incomplete
    running.gateway.close(1.0)
    running.gateway.close(1.0)  # idempotent: one final record
    found = record(running)
    assert found["closed"] is True
    assert (found["requests"], found["charged"], found["uncertain"]) == (2, 270, None)
    (closed,) = [line for line in running.log.getvalue().splitlines() if line.startswith("CLOSED")]
    assert closed == "CLOSED admitted=2 settled=2 charged=270 open=0"


def test_a_closed_gateway_admits_nothing_more(running: Running) -> None:
    running.gateway.close(1.0)
    status, _, data = running.post()
    assert (status, json.loads(data)["error"]["type"]) == (503, "overloaded_error")
    assert running.provider.seen == []
    assert "ADMIT" not in running.log.getvalue()


def test_closing_waits_for_a_request_in_flight_to_settle() -> None:
    provider = Provider(hold=threading.Event())
    for running in start(provider):
        sent = threading.Thread(target=running.post)
        sent.start()
        assert provider.entered.wait(5)
        closer = threading.Thread(target=running.gateway.close, args=(5.0,))
        closer.start()
        time.sleep(0.2)
        assert "CLOSED" not in running.log.getvalue()  # still draining
        provider.hold.set()  # type: ignore[union-attr]
        closer.join(5)
        sent.join(5)
        found = record(running)
        assert (found["closed"], found["requests"], found["uncertain"]) == (True, 1, None)


def test_a_request_outlasting_the_drain_is_uncertain() -> None:
    provider = Provider(hold=threading.Event())
    for running in start(provider):
        sent = threading.Thread(target=running.post)
        sent.start()
        assert provider.entered.wait(5)
        running.gateway.close(0.1)
        found = record(running)
        assert found["closed"] is True
        assert found["uncertain"] == "1 request(s) still in flight when the gateway closed"
        provider.hold.set()  # type: ignore[union-attr]
        sent.join(5)
        assert record(running)["requests"] == 1  # its late charge still counts


R = "ilmar-gateway READY"
A = "ADMIT ticket=1 reservation=500"
C = ("CHARGE ticket=1 charged=100 reservation=500 overrun=0 reason=reported "
     "spent=100 halted=false")  # fmt: skip


@pytest.mark.parametrize(
    "lines",
    [
        [A, C],  # no READY: an ADMIT before the gateway started
        [R, A, R, C],  # restarted: its earlier state is lost
        [R, C],  # a charge without its admission
        [R, A, A],  # admitted twice
        [R, A, "CLOSED admitted=1 settled=0 charged=0 open=1", "CLOSED admitted=1 settled=0 "
         "charged=0 open=1"],  # two final records
        [R, A, C, "CLOSED admitted=2 settled=1 charged=100 open=0"],  # a line is missing
        [R, A, C, "CLOSED admitted=1 settled=1 charged=90 open=0"],  # totals differ
        [R, "CLOSED admitted=0 settled=0 charged=0 open=0", A],  # admitted after closing
        [R, "ADMIT ticket=x reservation=1"],  # not understood
        [R, "CLOSED admitted=0"],
        ["ilmar-gateway READY again"],
    ],
)  # fmt: skip
def test_an_inconsistent_record_is_not_trusted(lines: list[str]) -> None:
    with pytest.raises(ValueError, match="gateway"):
        mg.parse_spend(lines)


def test_an_empty_or_cut_off_record_is_never_complete() -> None:
    assert mg.parse_spend([])["closed"] is False
    assert mg.parse_spend([R, A, C])["closed"] is False
    complete = mg.parse_spend([R, A, C, "CLOSED admitted=1 settled=1 charged=100 open=0"])
    assert (complete["closed"], complete["charged"], complete["uncertain"]) == (True, 100, None)
