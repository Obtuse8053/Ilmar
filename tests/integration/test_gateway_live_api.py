# SPDX-License-Identifier: Apache-2.0
"""The gateway's spending controls against the real model API (#111, P5 part 2).

Run by hand, with a key from a workspace that has a **small spend cap**:

    ANTHROPIC_API_KEY=… pytest -m integration -v tests/integration/test_gateway_live_api.py

The key stays in the test process's memory and the in-process gateway's; it is
never printed or written (the report is checked for it). Every request is
tiny: the whole module costs well under one US cent at current prices.
Optional: ``ILMAR_LIVE_GATEWAY_MODEL`` (default ``claude-haiku-5-5``),
``ILMAR_LIVE_REPORT_DIR`` for a credential-free record. Skipped without a key,
failed instead under ``ILMAR_LIVE_REQUIRED=1``.

What it confirms on the real API:

* a lowered ``max_tokens`` is what the provider honours (``stop_reason``
  ``max_tokens``, at most that many output tokens), streamed and not;
* the provider's real usage reports are complete and understood, so a normal
  request settles as ``reported`` and the attempt goes on;
* the charge never exceeds the reservation;
* the reviewed beta candidates the agent sends are accepted (or, if not, the
  gap is recorded), and ``thinking-token-count``'s estimates are never charged;
* counting tokens is checked and never charged.

Prices here are deliberately high (an over-estimate): this checks the
mechanics, not the provider's price list.
"""

from __future__ import annotations

import hashlib
import http.client
import io
import ipaddress
import json
import os
import threading
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from ilmar.adapters.podman import gateway_admission as ga
from ilmar.adapters.podman import model_gateway as mg
from live_harness import Secret, write_report

pytestmark = pytest.mark.integration

KEY = os.environ.get("ANTHROPIC_API_KEY", "")
MODEL = os.environ.get("ILMAR_LIVE_GATEWAY_MODEL", "claude-haiku-5-5")
REQUIRED = os.environ.get("ILMAR_LIVE_REQUIRED") == "1"
TOKEN = "ilmar-live-gateway-token-" + "0" * 16
RATES = {"input": 15_000_000, "output": 75_000_000, "cache_write_5m": 18_750_000,
         "cache_write_1h": 30_000_000, "cache_read": 1_500_000}  # fmt: skip
OUTPUT_CAP = 64
COUNT = "Count from 1 to 400, one number per line, nothing else."


def policy(**overrides: Any) -> str:
    base: dict[str, Any] = {
        "prices": {MODEL: RATES}, "max_output_tokens": OUTPUT_CAP, "min_output_tokens": 16,
        "allowed_betas": sorted(ga.CANDIDATE_BETAS),
    }  # fmt: skip
    base.update(overrides)
    return json.dumps(base)


@dataclass
class Live:
    gateway: mg.Gateway
    log: io.StringIO

    def post(self, body: dict[str, Any], *, path: str = "/v1/messages",
             betas: tuple[str, ...] = ()) -> tuple[int, bytes]:  # fmt: skip
        host, port = self.gateway.address
        connection = http.client.HTTPConnection(host, port, timeout=120)
        data = json.dumps(body).encode()
        connection.putrequest("POST", path, skip_accept_encoding=True)
        for name, value in {"x-api-key": TOKEN, "content-type": "application/json",
                            "anthropic-version": "2023-06-01",
                            "content-length": str(len(data))}.items():  # fmt: skip
            connection.putheader(name, value)
        for beta in betas:
            connection.putheader("anthropic-beta", beta)
        connection.endheaders(data)
        response = connection.getresponse()
        payload = response.read()
        connection.close()
        return response.status, payload

    def charges(self) -> list[str]:
        return [x for x in self.log.getvalue().splitlines() if x.startswith("CHARGE ")]


RECORD: list[dict[str, Any]] = []


def gateway(budget: int = 200_000, **overrides: Any) -> Iterator[Live]:
    if not KEY:
        if REQUIRED:
            pytest.fail("ILMAR_LIVE_REQUIRED=1 but ANTHROPIC_API_KEY is not set")
        pytest.skip("needs ANTHROPIC_API_KEY (a key with a small spend cap)")
    config = mg.GatewayConfig(
        token_sha256=hashlib.sha256(TOKEN.encode()).hexdigest(), key=KEY,
        upstream=mg.Upstream.parse("https://api.anthropic.com", allow_plain_http=False),
        clients=(ipaddress.ip_network("127.0.0.0/8"),),
        policy=policy(**overrides), budget=budget, max_in_flight=1,
    )  # fmt: skip
    log = io.StringIO()
    server = mg.Gateway(("127.0.0.1", 0), config, log=log)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield Live(server, log)
    finally:
        server.shutdown()
        RECORD.append({"log": log.getvalue().splitlines()})


@pytest.fixture
def live() -> Iterator[Live]:
    yield from gateway()


@pytest.fixture(scope="module", autouse=True)
def report() -> Iterator[None]:
    yield
    directory = os.environ.get("ILMAR_LIVE_REPORT_DIR")
    if directory and KEY:
        write_report(Path(directory), "issue-111-gateway-live-api", {
            "provenance": "recorded", "model": MODEL, "at": datetime.now(UTC).isoformat(),
            "gateways": RECORD,
        }, [Secret("ANTHROPIC_API_KEY", KEY)])  # fmt: skip


def events(data: bytes) -> list[dict[str, Any]]:
    out = []
    for line in data.splitlines():
        if line.startswith(b"data:"):
            out.append(json.loads(line[5:]))
    return out


def message(**fields: Any) -> dict[str, Any]:
    base: dict[str, Any] = {"model": MODEL, "max_tokens": 128_000,
                            "messages": [{"role": "user", "content": COUNT}]}  # fmt: skip
    base.update(fields)
    return base


def settled_reported(live: Live) -> str:
    (line,) = live.charges()[-1:]
    assert "reason=reported" in line, line  # real usage reports are understood
    assert "overrun=0" in line, line
    assert "halted=false" in line, line
    return line


def test_a_lowered_max_tokens_is_honoured_plain(live: Live) -> None:
    status, payload = live.post(message())
    assert status == 200, payload[:300]
    body = json.loads(payload)
    assert body["stop_reason"] == "max_tokens"
    assert body["usage"]["output_tokens"] <= OUTPUT_CAP
    settled_reported(live)
    assert "max_tokens_from=128000" in live.log.getvalue()


def test_a_lowered_max_tokens_is_honoured_streamed(live: Live) -> None:
    status, payload = live.post(message(stream=True))
    assert status == 200, payload[:300]
    stream = events(payload)
    deltas = [e for e in stream if e.get("type") == "message_delta"]
    assert deltas[-1]["delta"]["stop_reason"] == "max_tokens"
    assert deltas[-1]["usage"]["output_tokens"] <= OUTPUT_CAP
    settled_reported(live)


def test_a_request_the_budget_cannot_cover_is_refused_locally() -> None:
    for live in gateway(budget=100):  # a hundredth of a cent
        status, payload = live.post(message())
        assert (status, json.loads(payload)["error"]["type"]) == (429, "budget_exceeded")
        assert live.charges() == []


def test_the_reviewed_betas_and_shapes_are_accepted(live: Live) -> None:
    """What pinned Claude Code sends, in the shapes the gateway now allows."""
    tool = {
        "name": "Bash",
        "description": "Run a command.",
        "input_schema": {"type": "object", "properties": {"cmd": {"type": "string"}}},
    }
    body = message(
        stream=True, tools=[tool], max_tokens=OUTPUT_CAP,
        thinking={"type": "adaptive", "display": "omitted"},
        output_config={"effort": "low"},
        context_management={"edits": [{"type": "clear_thinking_20251015", "keep": "all"},
                                      {"type": "clear_tool_uses_20250919"}]},
        messages=[{"role": "user", "content": "Reply with OK."},
                  {"role": "system", "content": [], "output_config": {"effort": "low"}}],
    )  # fmt: skip
    status, payload = live.post(body, betas=tuple(sorted(ga.CANDIDATE_BETAS)))
    assert status == 200, payload[:500]
    settled_reported(live)


def test_estimated_thinking_tokens_are_never_charged(live: Live) -> None:
    body = message(
        stream=True,
        max_tokens=OUTPUT_CAP,
        thinking={"type": "adaptive", "display": "summarized"},
        output_config={"effort": "high"},
        messages=[{"role": "user", "content": "Is 391 prime? Think, then answer."}],
    )
    status, payload = live.post(body, betas=("thinking-token-count-2026-05-13",))
    assert status == 200, payload[:500]
    stream = events(payload)
    start = next(e for e in stream if e.get("type") == "message_start")["message"]["usage"]
    output = [e for e in stream if e.get("type") == "message_delta"][-1]["usage"]["output_tokens"]
    price = ga.ModelPrice(**RATES)
    expected = ga._cost(start["input_tokens"], price.input) + ga._cost(output, price.output)
    line = settled_reported(live)
    assert f"charged={expected} " in line, (line, start, output)


def test_counting_tokens_is_checked_and_never_charged(live: Live) -> None:
    body = {"model": MODEL, "messages": [{"role": "user", "content": "Hello"}]}
    status, payload = live.post(body, path="/v1/messages/count_tokens")
    assert status == 200, payload[:300]
    assert json.loads(payload)["input_tokens"] > 0
    assert live.charges() == []
    assert live.gateway.budget.spent() == 0


def test_a_closed_gateway_leaves_a_complete_record_of_real_requests(live: Live) -> None:
    """ADR-0039 against the real provider: after a plain and a streamed request,
    closing writes a final record whose totals match the charges, and nothing
    in it is uncertain."""
    for stream in (False, True):
        status, payload = live.post(message(stream=stream))
        assert status == 200, payload[:300]
        settled_reported(live)
    live.gateway.close(30.0)
    found = mg.parse_spend(live.log.getvalue().splitlines())
    assert found["closed"] is True
    assert (found["requests"], found["unsettled"], found["uncertain"]) == (2, 0, None)
    charged = sum(int(line.split("charged=")[1].split()[0]) for line in live.charges())
    assert found["charged"] == charged > 0
    status, _ = live.post(message())
    assert status == 503  # closed: nothing more reaches the provider
