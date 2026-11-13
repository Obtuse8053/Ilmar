# SPDX-License-Identifier: Apache-2.0
"""Spending control for the model gateway (ADR-0037 §2, #111 P5).

What the tests pin, after the #121 review:

* output is the hard part: ``max_tokens`` is lowered before forwarding, and a
  request is forwarded only if its reservation fits the attempt's budget;
* input is an estimate, not a bound: a report above the reservation is charged
  in full, recorded as an overrun, and halts the attempt;
* a missing, partial or invalid usage report keeps the whole reservation, even
  when ``message_stop`` arrived;
* images, unreviewed beta features and separately billed ones are refused.
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
    Settled,
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
IMAGE = {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "AA"}}


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


def estimate(data: bytes, *, units: int = 1, images: int = 0, tools: bool = True,
             policy: AdmissionPolicy = POLICY) -> int:  # fmt: skip
    return (len(data) + units * policy.message_overhead_tokens
            + images * (policy.image_tokens or 0)
            + (policy.tool_overhead_tokens if tools else 0))  # fmt: skip


def reservation(data: bytes, max_tokens: int, **kwargs: Any) -> int:
    return ga._cost(estimate(data, **kwargs), PRICE.cache_write_1h) + ga._cost(
        max_tokens, PRICE.output
    )


# -- what is constrained --------------------------------------------------------------


def test_claude_codes_default_max_tokens_is_lowered_to_the_operator_limit() -> None:
    _, admitted = Budget(limit=50 * DOLLAR, policy=POLICY).admit(body())
    assert admitted.capped_from == 128_000
    assert admitted.max_tokens == 32_000
    assert json.loads(admitted.body)["max_tokens"] == 32_000
    assert admitted.input_estimate == estimate(body())
    assert admitted.reservation == reservation(body(), 32_000)


def test_a_request_within_the_limits_is_forwarded_byte_for_byte() -> None:
    data = body(max_tokens=4_000)
    _, admitted = Budget(limit=50 * DOLLAR, policy=POLICY).admit(data)
    assert admitted.body == data
    assert admitted.capped_from is None


def test_a_tight_budget_lowers_max_tokens_to_what_fits() -> None:
    data = body(max_tokens=32_000)
    limit = reservation(data, 5_000)
    _, admitted = Budget(limit=limit, policy=POLICY).admit(data)
    assert (admitted.max_tokens, admitted.capped_from) == (5_000, 32_000)
    assert admitted.reservation <= limit


def test_below_the_floor_the_request_is_refused_and_nothing_is_reserved() -> None:
    data = body(max_tokens=32_000)
    budget = Budget(limit=reservation(data, POLICY.min_output_tokens) - 1, policy=POLICY)
    with pytest.raises(RequestRefused) as refused:
        budget.admit(data)
    assert (refused.value.kind, refused.value.status) == ("budget_exceeded", 429)
    assert budget.spent() == 0


def test_in_flight_reservations_count_against_the_budget() -> None:
    data = body(max_tokens=4_000)
    one = reservation(data, 4_000)
    budget = Budget(limit=2 * one, policy=POLICY)
    first, _ = budget.admit(data)
    budget.admit(data)
    with pytest.raises(RequestRefused):
        budget.admit(data)
    budget.settle(first, Settled(0, one, "reported"))
    budget.admit(data)


def test_framing_is_estimated_per_message_and_block() -> None:
    blocks = [{"type": "text", "text": "a"}, {"type": "text", "text": "b"}]
    data = body(max_tokens=2_000, system="be brief",
                messages=[{"role": "user", "content": blocks}])  # fmt: skip
    _, admitted = Budget(limit=50 * DOLLAR, policy=POLICY).admit(data)
    assert admitted.input_estimate == estimate(data, units=4)  # system, message, two blocks


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


def test_images_are_refused_unless_a_per_image_bound_is_attested() -> None:
    data = body(max_tokens=1_024, messages=[{"role": "user", "content": [IMAGE, IMAGE]}])
    with pytest.raises(RequestRefused, match="images"):
        Budget(limit=50 * DOLLAR, policy=POLICY).admit(data)
    tool_image = body(max_tokens=1_024, messages=[{"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": "t", "content": [IMAGE]}]}])  # fmt: skip
    with pytest.raises(RequestRefused, match="images"):
        Budget(limit=50 * DOLLAR, policy=POLICY).admit(tool_image)
    allowed = AdmissionPolicy(prices={MODEL: PRICE}, image_tokens=5_000)
    _, admitted = Budget(limit=50 * DOLLAR, policy=allowed).admit(data)
    assert admitted.input_estimate == estimate(data, units=3, images=2, policy=allowed)


def test_only_reviewed_beta_features_are_forwarded() -> None:
    reviewed = AdmissionPolicy(prices={MODEL: PRICE}, allowed_betas=frozenset({"x-2025"}))
    Budget(limit=50 * DOLLAR, policy=reviewed).admit(body(), betas=["x-2025"])
    Budget(limit=50 * DOLLAR, policy=reviewed).admit(body(), betas=["x-2025, "])
    for betas in (["y-2025"], ["x-2025,y-2025"], ["context-1m-2025-08-07"]):
        with pytest.raises(RequestRefused, match="beta feature not allowed"):
            Budget(limit=50 * DOLLAR, policy=reviewed).admit(body(), betas=betas)
    with pytest.raises(RequestRefused, match="beta"):
        Budget(limit=50 * DOLLAR, policy=POLICY).admit(body(), betas=["anything"])


@pytest.mark.parametrize(
    "beta",
    ["context-1m-2025-08-07", "code-execution-2025-08-25", "web-fetch-2025-09-10",
     "mcp-client-2025-04-04", "files-api-2025-04-14", "computer-use-2025-01-24",
     "web-search-x", "skills-2025-10-02"],
)  # fmt: skip
def test_separately_billed_beta_features_cannot_even_be_allowed(beta: str) -> None:
    with pytest.raises(ValueError, match="billed separately"):
        AdmissionPolicy(prices={MODEL: PRICE}, allowed_betas=frozenset({beta}))


def test_an_input_above_the_standard_window_needs_long_context_prices() -> None:
    huge = body(messages=[{"role": "user", "content": "x" * 210_000}])
    with pytest.raises(RequestRefused) as refused:
        Budget(limit=50 * DOLLAR, policy=POLICY).admit(huge)
    assert refused.value.kind == "request_too_large"
    long = AdmissionPolicy(prices={MODEL: LONG})
    _, admitted = Budget(limit=50 * DOLLAR, policy=long).admit(huge)
    scaled = PRICE.cache_write_1h * 10_000_000 // PRICE.input  # the dearest input, scaled
    assert admitted.reservation == ga._cost(estimate(huge, policy=long), scaled) + ga._cost(
        32_000, 37_500_000
    )


def test_a_rewrite_that_would_grow_the_body_is_refused_and_releases_its_reservation() -> None:
    compact = json.dumps(json.loads(body()), separators=(",", ":"))
    tiny = '"temperature":1e-9,"top_p":1e-9,"top_k":1e-9,'  # each re-encodes one byte longer
    grown = compact.replace('"stream":true', tiny + '"stream":true').encode()
    budget = Budget(limit=50 * DOLLAR, policy=POLICY)
    with pytest.raises(RequestRefused, match="re-encoded"):
        budget.admit(grown)
    assert budget.spent() == 0


# -- settlement -----------------------------------------------------------------------


def sse(*events: dict[str, Any]) -> bytes:
    return b"".join(b"event: x\ndata: " + json.dumps(e).encode() + b"\n\n" for e in events)


def start(**usage: Any) -> dict[str, Any]:
    base = {"input_tokens": 1_000, "cache_read_input_tokens": 20_000,
            "cache_creation_input_tokens": 300,
            "cache_creation": {"ephemeral_5m_input_tokens": 300, "ephemeral_1h_input_tokens": 0},
            "output_tokens": 1}  # fmt: skip
    return {"type": "message_start", "message": {"usage": base | usage}}


def delta(**usage: Any) -> dict[str, Any]:
    return {"type": "message_delta", "usage": {"output_tokens": 700} | usage}


STOP = {"type": "message_stop"}


def metered(data: bytes, *, streamed: bool = True, split: int = 7) -> UsageMeter:
    meter = UsageMeter(streamed=streamed)
    for i in range(0, len(data), split):
        meter.feed(data[i : i + split])
    meter.finish()
    return meter


def test_a_complete_stream_is_charged_its_reported_usage() -> None:
    settled = ga.charge(metered(sse(start(), delta(), STOP)), PRICE, reservation=10 * DOLLAR)
    expected = (ga._cost(1_000, PRICE.input) + ga._cost(20_000, PRICE.cache_read)
                + ga._cost(300, PRICE.cache_write_5m) + ga._cost(700, PRICE.output))  # fmt: skip
    assert settled == Settled(expected, 10 * DOLLAR, "reported")


def strip(event: dict[str, Any], key: str) -> dict[str, Any]:
    copy: dict[str, Any] = json.loads(json.dumps(event))
    copy["message"]["usage"].pop(key)
    return copy


@pytest.mark.parametrize(
    "data",
    [
        sse(start(), delta()),  # cut off before message_stop
        sse(start(), delta())[:-30],  # cut off mid-event
        b"",
        sse(STOP),  # stop, but no usage at all
        sse({"type": "message_start", "message": {}}, delta(), STOP),
        sse(strip(start(), "input_tokens"), delta(), STOP),
        sse(strip(start(), "output_tokens"), delta(), STOP),
        sse(start(), STOP),  # no message_delta: the final output count never came
        sse(start(), {"type": "message_delta"}, STOP),
        sse(start(), delta(output_tokens="700"), STOP),
        sse(start(), delta(output_tokens=-1), STOP),
        sse(start(), delta(output_tokens=True), STOP),
        sse(start(input_tokens=None), delta(), STOP),
        sse(start(input_tokens=1.5), delta(), STOP),
        sse(start(web_fetch_requests=1), delta(), STOP),  # a usage key not understood
        sse(start(server_tool_use={"web_search_requests": 1}), delta(), STOP),
        sse(start(service_tier="priority"), delta(), STOP),
        sse(start(cache_creation_input_tokens=999), delta(), STOP),  # does not add up
        sse(start(cache_creation={"ephemeral_5m_input_tokens": 300, "other": 1}), delta(),
            STOP),
        sse(start(), {"type": "error", "error": {"type": "overloaded_error"}}, delta(), STOP),
        sse(start(), delta(), STOP) + b"data: {not json\n\n",
    ],
)  # fmt: skip
def test_a_missing_partial_or_invalid_report_keeps_the_whole_reservation(data: bytes) -> None:
    settled = ga.charge(metered(data), PRICE, reservation=123_456)
    assert settled.charged == 123_456
    assert settled.reason != "reported"


def test_a_report_above_the_reservation_is_charged_in_full_and_halts_the_attempt() -> None:
    data = body(max_tokens=2_000)
    budget = Budget(limit=50 * DOLLAR, policy=POLICY)
    ticket, admitted = budget.admit(data)
    huge_input = start(input_tokens=admitted.input_estimate * 3)  # the estimate was wrong
    meter = metered(sse(huge_input, delta(output_tokens=2_000), STOP))
    settled = budget.settle(ticket, ga.charge(meter, PRICE, admitted.reservation))
    assert settled.charged > admitted.reservation  # never capped
    assert settled.overrun == settled.charged - admitted.reservation
    assert budget.halted
    assert budget.overruns == settled.overrun
    assert budget.spent() == settled.charged
    with pytest.raises(RequestRefused, match="stopped") as refused:
        budget.admit(data)
    assert refused.value.kind == "budget_exceeded"


def test_writes_without_a_breakdown_are_charged_at_the_one_hour_rate() -> None:
    first = {"type": "message_start", "message": {"usage": {"input_tokens": 0,
             "cache_creation_input_tokens": 1_000, "output_tokens": 0}}}  # fmt: skip
    settled = ga.charge(metered(sse(first, delta(output_tokens=0), STOP)), PRICE, 10 * DOLLAR)
    assert settled.charged == ga._cost(1_000, PRICE.cache_write_1h)


def test_a_plain_response_is_settled_from_its_body() -> None:
    message = {"type": "message", "stop_reason": "end_turn",
               "usage": {"input_tokens": 10, "output_tokens": 5}}  # fmt: skip
    settled = ga.charge(metered(json.dumps(message).encode(), streamed=False), PRICE, DOLLAR)
    assert settled.charged == ga._cost(10, PRICE.input) + ga._cost(5, PRICE.output)
    for broken in (b"{oops", json.dumps(message | {"usage": {"input_tokens": 10}}).encode(),
                   json.dumps(message | {"stop_reason": None}).encode()):  # fmt: skip
        assert ga.charge(metered(broken, streamed=False), PRICE, 99).charged == 99


# -- the guarantee --------------------------------------------------------------------


def test_any_usage_within_the_estimate_and_max_tokens_fits_its_reservation() -> None:
    """When the input estimate holds, the reservation covers any billing split:
    output is bounded by the granted max_tokens, input by the estimate."""
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
            bound = admitted.input_estimate
            cuts = sorted(rng.randint(0, bound) for _ in range(3))
            parts = [cuts[0], cuts[1] - cuts[0], cuts[2] - cuts[1], bound - cuts[2]]
            meter = UsageMeter(streamed=True, input_tokens=parts[0], cache_write_5m=parts[1],
                               cache_write_1h=parts[2], cache_read=parts[3],
                               output_tokens=admitted.max_tokens, started=True, delta=True,
                               stopped=True)  # fmt: skip
            assert ga.charge(meter, price, admitted.reservation).overrun == 0


def test_spend_stays_within_the_budget_plus_the_overruns_in_flight() -> None:
    """Adversarial settlements, including overruns, from 8 threads: every
    admission fits the budget when made; after the first overrun, none is."""
    data = body(max_tokens=8_000)
    limit = 7 * reservation(data, 8_000) + 12_345
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
                peaks.append(budget.settled - budget.overruns + sum(budget.in_flight.values()))
            cost = rng.choice([0, admitted.reservation, 3 * admitted.reservation])
            budget.settle(ticket, Settled(cost, admitted.reservation, "reported"))

    threads = [threading.Thread(target=worker, args=(n,)) for n in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert max(peaks) <= limit
    assert budget.halted
    assert budget.spent() <= limit + budget.overruns


def test_the_policy_comes_from_json() -> None:
    rates = {"input": 1, "output": 2, "cache_write_5m": 3, "cache_write_1h": 4, "cache_read": 1}
    text = json.dumps({"prices": {MODEL: rates}, "max_output_tokens": 8_000,
                       "allowed_betas": ["x-2025"], "image_tokens": 4_000})  # fmt: skip
    policy = ga.policy_from_json(text)
    assert max(policy.prices[MODEL].input_rates(1)) == 4
    assert (policy.max_output_tokens, policy.image_tokens) == (8_000, 4_000)
    assert policy.allowed_betas == frozenset({"x-2025"})
    with pytest.raises(ValueError, match="positive"):
        ModelPrice(input=0, output=1, cache_write_5m=1, cache_write_1h=1, cache_read=1)
    with pytest.raises(ValueError, match="threshold"):
        ModelPrice(input=1, output=1, cache_write_5m=1, cache_write_1h=1, cache_read=1,
                   long_context_threshold=5)  # fmt: skip
