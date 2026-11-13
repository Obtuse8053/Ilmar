# SPDX-License-Identifier: Apache-2.0
"""Worst-case spending control for the model gateway (ADR-0037 §2, #111 P5).

The property that matters: whatever the agent sends and whatever the provider
reports, an attempt's settled plus in-flight spend never exceeds its budget,
and every response the provider could bill within the admitted request costs
no more than its reservation.
"""

from __future__ import annotations

import json
import random
import threading
from typing import Any

import pytest

from ilmar.adapters.podman import gateway_admission as ga
from ilmar.adapters.podman.gateway_admission import (
    AdmissionPolicy,
    Budget,
    ModelPrice,
    RequestRefused,
    UsageMeter,
)

MODEL = "claude-opus-5-5"
# Illustrative rates only (micro-dollars per million tokens), not attested prices.
PRICE = ModelPrice(
    input=5_000_000, output=25_000_000, cache_write_5m=6_250_000, cache_write_1h=10_000_000,
    cache_read=500_000,
)  # fmt: skip
LONG = ModelPrice(
    input=5_000_000, output=25_000_000, cache_write_5m=6_250_000, cache_write_1h=10_000_000,
    cache_read=500_000, long_context_threshold=200_000, long_context_input=10_000_000,
    long_context_output=37_500_000,
)  # fmt: skip
POLICY = AdmissionPolicy(prices={MODEL: PRICE})
DOLLAR = 1_000_000


def body(**fields: Any) -> bytes:
    request: dict[str, Any] = {
        "model": MODEL,
        "max_tokens": 128_000,  # what Claude Code 2.1.318 asks for (C0, #111)
        "stream": True,
        "messages": [{"role": "user", "content": "Reply with OK."}],
        "tools": [{"name": "Bash", "input_schema": {"type": "object"}}],
    }
    request.update(fields)
    return json.dumps(request).encode()


def worst(data: bytes, max_tokens: int, *, images: int = 0, tools: bool = True) -> int:
    tokens = (
        len(data) + images * POLICY.image_tokens + (POLICY.tool_overhead_tokens if tools else 0)
    )
    return ga._cost(tokens, PRICE.cache_write_1h) + ga._cost(max_tokens, PRICE.output)


# -- what is constrained --------------------------------------------------------------


def test_claude_codes_default_max_tokens_is_lowered_to_the_operator_limit() -> None:
    budget = Budget(limit=50 * DOLLAR, policy=POLICY)
    _, admitted = budget.admit(body())
    assert admitted.capped_from == 128_000
    assert admitted.max_tokens == POLICY.max_output_tokens == 32_000
    assert json.loads(admitted.body)["max_tokens"] == 32_000
    assert admitted.reservation == worst(body(), 32_000)


def test_a_request_within_the_limits_is_forwarded_byte_for_byte() -> None:
    data = body(max_tokens=4_000)
    _, admitted = Budget(limit=50 * DOLLAR, policy=POLICY).admit(data)
    assert admitted.body == data
    assert admitted.capped_from is None


def test_a_tight_budget_lowers_max_tokens_to_what_fits() -> None:
    data = body(max_tokens=32_000)
    input_cost = worst(data, 0)
    limit = input_cost + ga._cost(5_000, PRICE.output)
    _, admitted = Budget(limit=limit, policy=POLICY).admit(data)
    assert admitted.max_tokens == 5_000
    assert admitted.capped_from == 32_000
    assert admitted.reservation <= limit


def test_below_the_floor_the_request_is_refused_and_nothing_is_reserved() -> None:
    data = body(max_tokens=32_000)
    budget = Budget(limit=worst(data, POLICY.min_output_tokens) - 1, policy=POLICY)
    with pytest.raises(RequestRefused) as refused:
        budget.admit(data)
    assert refused.value.kind == "budget_exceeded"
    assert refused.value.status == 429
    assert budget.spent() == 0


def test_in_flight_reservations_count_against_the_budget() -> None:
    data = body(max_tokens=4_000)
    one = worst(data, 4_000)
    budget = Budget(limit=2 * one, policy=POLICY)
    first, _ = budget.admit(data)
    budget.admit(data)
    with pytest.raises(RequestRefused):
        budget.admit(data)
    budget.settle(first, 0)
    budget.admit(data)  # room again once one settled cheaply


# -- what is refused ------------------------------------------------------------------


@pytest.mark.parametrize(
    ("data", "kind"),
    [
        (b"not json", "invalid_request_error"),
        (b"[1]", "invalid_request_error"),
        (body(model="claude-other"), "permission_error"),
        (body(max_tokens=True), "invalid_request_error"),
        (body(max_tokens=0), "invalid_request_error"),
        (body(mcp_servers=[]), "invalid_request_error"),
        (body(container="c"), "invalid_request_error"),
        (body(context_management={}), "invalid_request_error"),  # unpriced field
        (body(service_tier="auto"), "invalid_request_error"),
        (body(tools=[{"type": "web_search_20250305", "name": "web_search"}]),
         "invalid_request_error"),
        (body(messages=[{"role": "user", "content": [
            {"type": "document", "source": {"type": "base64", "data": "x"}}]}]),
         "invalid_request_error"),
        (body(messages=[{"role": "user", "content": [
            {"type": "image", "source": {"type": "url", "url": "https://x"}}]}]),
         "invalid_request_error"),
        (body(messages=[{"role": "user", "content": [{"type": "tool_result",
            "tool_use_id": "t", "content": [{"type": "document"}]}]}]),
         "invalid_request_error"),
        (body(thinking={"type": "enabled", "budget_tokens": 40_000}), "invalid_request_error"),
        (body(thinking={"type": "unknown"}), "invalid_request_error"),
    ],
)  # fmt: skip
def test_unpriced_or_unknown_requests_are_refused(data: bytes, kind: str) -> None:
    budget = Budget(limit=50 * DOLLAR, policy=POLICY)
    with pytest.raises(RequestRefused) as refused:
        budget.admit(data)
    assert refused.value.kind == kind
    assert budget.spent() == 0


def test_a_thinking_budget_below_the_lowered_max_is_admitted() -> None:
    _, admitted = Budget(limit=50 * DOLLAR, policy=POLICY).admit(
        body(thinking={"type": "enabled", "budget_tokens": 16_000})
    )
    assert admitted.max_tokens == 32_000


def test_an_input_above_the_standard_window_needs_long_context_prices() -> None:
    huge = body(messages=[{"role": "user", "content": "x" * 210_000}])
    with pytest.raises(RequestRefused) as refused:
        Budget(limit=50 * DOLLAR, policy=POLICY).admit(huge)
    assert refused.value.kind == "request_too_large"
    long = AdmissionPolicy(prices={MODEL: LONG})
    _, admitted = Budget(limit=50 * DOLLAR, policy=long).admit(huge)
    tokens = len(huge) + long.tool_overhead_tokens
    in_rate = PRICE.cache_write_1h * 10_000_000 // PRICE.input  # the dearest input, scaled
    assert admitted.reservation == ga._cost(tokens, in_rate) + ga._cost(32_000, 37_500_000)


def test_images_add_their_allowance() -> None:
    image = {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "AA"}}
    data = body(max_tokens=1_024, messages=[{"role": "user", "content": [image, image]}])
    _, admitted = Budget(limit=50 * DOLLAR, policy=POLICY).admit(data)
    assert admitted.reservation == worst(data, 1_024, images=2)


# -- settlement -----------------------------------------------------------------------


def sse(*events: dict[str, Any]) -> bytes:
    return b"".join(b"event: x\ndata: " + json.dumps(e).encode() + b"\n\n" for e in events)


START = {"type": "message_start", "message": {"usage": {
    "input_tokens": 1_000, "cache_read_input_tokens": 20_000,
    "cache_creation": {"ephemeral_5m_input_tokens": 300, "ephemeral_1h_input_tokens": 0},
    "output_tokens": 1}}}  # fmt: skip
DELTA = {"type": "message_delta", "usage": {"output_tokens": 700}}
STOP = {"type": "message_stop"}


def metered(data: bytes, *, streamed: bool = True, split: int = 7) -> UsageMeter:
    meter = UsageMeter(streamed=streamed)
    for i in range(0, len(data), split):
        meter.feed(data[i : i + split])
    meter.finish()
    return meter


def test_a_complete_stream_is_charged_its_reported_usage() -> None:
    meter = metered(sse(START, DELTA, STOP))
    expected = (ga._cost(1_000, PRICE.input) + ga._cost(20_000, PRICE.cache_read)
                + ga._cost(300, PRICE.cache_write_5m) + ga._cost(700, PRICE.output))  # fmt: skip
    assert ga.charge(meter, PRICE, reservation=10 * DOLLAR) == expected


@pytest.mark.parametrize(
    "data",
    [
        sse(START, DELTA),  # cut off before message_stop
        sse(START, DELTA)[:-30],  # cut off mid-event
        b"",
        sse({"type": "message_start", "message": {"usage": {
            "input_tokens": 1, "server_tool_use": {"web_search_requests": 1}}}}, STOP),
    ],
)  # fmt: skip
def test_an_incomplete_or_disallowed_report_keeps_the_whole_reservation(data: bytes) -> None:
    assert ga.charge(metered(data), PRICE, reservation=123_456) == 123_456


def test_a_charge_never_exceeds_the_reservation() -> None:
    huge = {"type": "message_delta", "usage": {"output_tokens": 10**9}}
    assert ga.charge(metered(sse(START, huge, STOP)), PRICE, reservation=5) == 5


def test_writes_without_a_breakdown_are_charged_at_the_one_hour_rate() -> None:
    start = {"type": "message_start", "message": {"usage": {"input_tokens": 0,
             "cache_creation_input_tokens": 1_000, "output_tokens": 0}}}  # fmt: skip
    cost = ga.charge(metered(sse(start, STOP)), PRICE, reservation=10 * DOLLAR)
    assert cost == ga._cost(1_000, PRICE.cache_write_1h)


def test_a_plain_response_is_settled_from_its_body() -> None:
    message = {"type": "message", "stop_reason": "end_turn",
               "usage": {"input_tokens": 10, "output_tokens": 5}}  # fmt: skip
    meter = metered(json.dumps(message).encode(), streamed=False)
    assert ga.charge(meter, PRICE, 10 * DOLLAR) == (
        ga._cost(10, PRICE.input) + ga._cost(5, PRICE.output)
    )
    assert ga.charge(metered(b"{oops", streamed=False), PRICE, 99) == 99


# -- the bound ------------------------------------------------------------------------


def test_any_billable_usage_within_the_admitted_request_fits_its_reservation() -> None:
    """Soundness: the provider bills at most the input bound across the four
    input-side kinds, and at most ``max_tokens`` of output."""
    rng = random.Random(1137)  # noqa: S311 - a fixed, reproducible sample
    for price in (PRICE, LONG):
        policy = AdmissionPolicy(prices={MODEL: price})
        for _ in range(300):
            size = rng.choice([10, 1_000, 50_000, 190_000, 260_000])
            data = body(max_tokens=rng.randint(1_024, 200_000),
                        messages=[{"role": "user", "content": "y" * size}])  # fmt: skip
            try:
                _, admitted = Budget(limit=500 * DOLLAR, policy=policy).admit(data)
            except RequestRefused:
                assert price is PRICE  # only the long input without long-context prices
                continue
            bound = len(admitted.body) + policy.tool_overhead_tokens
            cuts = sorted(rng.randint(0, bound) for _ in range(3))
            parts = [cuts[0], cuts[1] - cuts[0], cuts[2] - cuts[1], bound - cuts[2]]
            meter = UsageMeter(streamed=True, input_tokens=parts[0], cache_write_5m=parts[1],
                               cache_write_1h=parts[2], cache_read=parts[3],
                               output_tokens=admitted.max_tokens, complete=True)  # fmt: skip
            unclamped = ga.charge(meter, price, reservation=10**15)
            assert unclamped <= admitted.reservation


def test_concurrent_admissions_never_exceed_the_budget() -> None:
    data = body(max_tokens=8_000)
    limit = 7 * worst(data, 8_000) + 12_345
    budget = Budget(limit=limit, policy=POLICY)
    peaks: list[int] = []
    lock = threading.Lock()

    def worker(seed: int) -> None:
        rng = random.Random(seed)  # noqa: S311 - test data
        for _ in range(200):
            try:
                ticket, admitted = budget.admit(data)
            except RequestRefused:
                continue
            with lock:
                peaks.append(budget.spent())
            budget.settle(ticket, rng.choice([0, admitted.reservation, 10**12]))

    threads = [threading.Thread(target=worker, args=(n,)) for n in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert max(peaks) <= limit
    assert budget.spent() <= limit


def test_the_policy_comes_from_json() -> None:
    rates = {"input": 1, "output": 2, "cache_write_5m": 3, "cache_write_1h": 4, "cache_read": 1}
    text = json.dumps({"prices": {MODEL: rates}, "max_output_tokens": 8_000})
    policy = ga.policy_from_json(text)
    assert policy.prices[MODEL].top_input == 4
    assert policy.max_output_tokens == 8_000
    with pytest.raises(ValueError, match="positive"):
        ModelPrice(input=0, output=1, cache_write_5m=1, cache_write_1h=1, cache_read=1)
    with pytest.raises(ValueError, match="threshold"):
        ModelPrice(input=1, output=1, cache_write_5m=1, cache_write_1h=1, cache_read=1,
                   long_context_threshold=5)  # fmt: skip


def test_a_rewrite_that_would_grow_the_body_is_refused_and_releases_its_reservation() -> None:
    compact = json.dumps(json.loads(body()), separators=(",", ":"))
    tiny = '"temperature":1e-9,"top_p":1e-9,"top_k":1e-9,'  # each re-encodes one byte longer
    grown = compact.replace('"stream":true', tiny + '"stream":true').encode()
    assert len(json.dumps(json.loads(grown), separators=(",", ":"))) > len(grown)
    budget = Budget(limit=50 * DOLLAR, policy=POLICY)
    with pytest.raises(RequestRefused, match="re-encoded"):
        budget.admit(grown)
    assert budget.spent() == 0
