# SPDX-License-Identifier: Apache-2.0
"""Beta handling and strict request shapes at the gateway (P5 part 2).

Maintainer decisions on #111 (2026-11-13):

* every ``anthropic-beta`` header is read, duplicates included, and split on
  commas;
* a beta that is not allowed is dropped only when its feature is visibly unused;
  otherwise the request is refused;
* ``advanced-tool-use`` server-side capabilities and separately billed tools are
  denied and can never be allowed;
* ``output_config``, ``context_management``, system messages, tool additions and
  removals and ``thinking.display`` pass only strict nested allow-lists, and no
  alternate shape lets a server tool through;
* ``estimated_tokens`` in ``thinking_delta`` is not billable and never counted.
"""

from __future__ import annotations

import json
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
PRICE = ModelPrice(
    input=5_000_000, output=25_000_000, cache_write_5m=6_250_000,
    cache_write_1h=10_000_000, cache_read=500_000,
)  # fmt: skip
DOLLAR = 1_000_000
CANDIDATES = AdmissionPolicy(prices={MODEL: PRICE}, allowed_betas=ga.CANDIDATE_BETAS)
NONE = AdmissionPolicy(prices={MODEL: PRICE})
TOOL = {"name": "Bash", "input_schema": {"type": "object"}}
USER = {"role": "user", "content": "Reply with OK."}


def request(**fields: Any) -> dict[str, Any]:
    base: dict[str, Any] = {"model": MODEL, "max_tokens": 4_000, "stream": True,
                            "messages": [USER], "tools": [TOOL]}  # fmt: skip
    base.update(fields)
    return base


def admit(
    fields: dict[str, Any] | None = None,
    betas: list[str] | tuple[str, ...] = (),
    policy: AdmissionPolicy = CANDIDATES,
) -> ga.Admitted:
    data = json.dumps(request(**(fields or {}))).encode()
    _, admitted = Budget(limit=50 * DOLLAR, policy=policy).admit(data, betas)
    return admitted


def refused(fields: dict[str, Any] | None = None, betas: tuple[str, ...] = (),
            policy: AdmissionPolicy = CANDIDATES) -> str:  # fmt: skip
    with pytest.raises(RequestRefused) as caught:
        admit(fields, betas, policy)
    return str(caught.value)


def system(*blocks: Any, **extra: Any) -> dict[str, Any]:
    return {"role": "system", "content": list(blocks), **extra}


def addition(name: str = "Bash", **tool: Any) -> dict[str, Any]:
    return {"type": "tool_addition", "tool": {"type": "tool_reference", "name": name, **tool}}


# -- reading the headers ---------------------------------------------------------------


def test_every_header_and_every_comma_separated_value_is_read() -> None:
    values = ["claude-code-20250219, effort-2025-11-24", "context-management-2025-06-27",
              "effort-2025-11-24"]  # fmt: skip
    assert ga.split_betas(values) == [
        "claude-code-20250219", "effort-2025-11-24", "context-management-2025-06-27"
    ]  # fmt: skip


def test_a_refused_beta_in_a_second_header_is_still_refused() -> None:
    """A gateway reading only the first header could be bypassed."""
    message = refused(betas=("claude-code-20250219", "web-search-2025-03-05"))
    assert "web-search" in message


@pytest.mark.parametrize("value", ["UPPER", "a b", "x;y", "a" * 81, "-lead"])
def test_a_malformed_value_is_refused(value: str) -> None:
    assert "malformed" in refused(betas=(value,))


def test_kept_betas_are_forwarded_in_order_without_repeats() -> None:
    admitted = admit(betas=["effort-2025-11-24,claude-code-20250219", "effort-2025-11-24"])
    assert admitted.betas == ("effort-2025-11-24", "claude-code-20250219")
    assert admitted.dropped == ()


# -- allowed, dropped or refused ------------------------------------------------------


def test_nothing_is_allowed_by_default() -> None:
    assert NONE.allowed_betas == frozenset()
    assert "not allowed" in refused(betas=("claude-code-20250219",), policy=NONE)


def test_the_candidates_are_the_reviewed_eight() -> None:
    assert {
        "claude-code-20250219",
        "interleaved-thinking-2025-05-14",
        "effort-2025-11-24",
        "per-turn-control-2026-07-01",
        "mid-conversation-tool-changes-2026-07-01",
        "thinking-display-updates-2026-08-18",
        "context-management-2025-06-27",
        "thinking-token-count-2026-05-13",
    } == ga.CANDIDATE_BETAS


def test_an_unused_feature_s_beta_is_dropped_when_not_allowed() -> None:
    admitted = admit(betas=("context-management-2025-06-27", "per-turn-control-2026-07-01"),
                     policy=NONE)  # fmt: skip
    assert admitted.betas == ()
    assert admitted.dropped == ("context-management-2025-06-27", "per-turn-control-2026-07-01")


def test_a_used_feature_s_beta_is_refused_when_not_allowed() -> None:
    fields = {"context_management": {"edits": [{"type": "clear_tool_uses_20250919"}]}}
    assert "context-management" in refused(fields, ("context-management-2025-06-27",), NONE)


def test_a_beta_whose_use_cannot_be_seen_is_never_dropped() -> None:
    assert "not allowed" in refused(betas=("thinking-token-count-2026-05-13",), policy=NONE)
    assert "not allowed" in refused(betas=("some-new-thing-2027-01-01",))


def test_advanced_tool_use_is_dropped_when_unused() -> None:
    admitted = admit(betas=("advanced-tool-use-2025-11-20", "effort-2025-11-24"))
    assert admitted.betas == ("effort-2025-11-24",)
    assert admitted.dropped == ("advanced-tool-use-2025-11-20",)


@pytest.mark.parametrize(
    "tools",
    [
        [TOOL | {"defer_loading": True}],
        [TOOL | {"allowed_callers": ["code_execution_20250825"]}],
        [TOOL | {"input_examples": [{}]}],
        [{"type": "tool_search_tool_regex_20251119", "name": "tool_search_tool_regex"}, TOOL],
    ],
)
def test_advanced_tool_use_is_refused_when_its_features_are_used(tools: list[Any]) -> None:
    assert "advanced-tool-use" in refused({"tools": tools}, ("advanced-tool-use-2025-11-20",))


def test_denied_and_billed_betas_can_never_be_allowed() -> None:
    for beta in ("advanced-tool-use-2025-11-20", "web-search-2025-03-05", "context-1m-2025-08-07"):
        with pytest.raises(ValueError, match="cannot be allowed"):
            AdmissionPolicy(prices={MODEL: PRICE}, allowed_betas=frozenset({beta}))


def test_a_billed_beta_is_refused_even_when_unused() -> None:
    assert "context-1m" in refused(betas=("context-1m-2025-08-07",))


# -- server tools never pass through another shape ------------------------------------


@pytest.mark.parametrize(
    "fields",
    [
        {"tools": [{"type": "web_search_20250305", "name": "web_search"}]},
        {"tools": [{"type": "code_execution_20250825", "name": "code_execution"}]},
        {"tools": [TOOL | {"defer_loading": True}]},
        {"tools": [TOOL | {"strict": True}]},
        {
            "messages": [
                USER,
                system(
                    {
                        "type": "tool_addition",
                        "tool": {
                            "type": "tool_definition",
                            "definition": {"type": "web_search_20250305", "name": "web_search"},
                        },
                    }
                ),
            ]
        },
        {
            "messages": [
                USER,
                system(
                    {
                        "type": "tool_addition",
                        "tool": {"type": "mcp_toolset_reference", "server_name": "x"},
                    }
                ),
            ]
        },
        {"messages": [USER, system(addition("web_search"))]},  # not a declared tool
        {"messages": [USER, system(addition(extra=1))]},
        {
            "messages": [
                USER,
                {
                    "role": "assistant",
                    "content": [
                        {"type": "server_tool_use", "id": "s", "name": "web_search", "input": {}}
                    ],
                },
            ]
        },
        {"mcp_servers": [{"type": "url", "url": "https://x", "name": "x"}]},
        {"container": "c"},
    ],
)
def test_no_alternate_shape_lets_a_server_tool_through(fields: dict[str, Any]) -> None:
    refused(fields, ("mid-conversation-tool-changes-2026-07-01",))


def test_tool_changes_by_reference_to_a_declared_tool_pass_with_their_beta() -> None:
    blocks = (addition(), {"type": "tool_removal", "tool": {"type": "tool_reference",
              "name": "Bash"}, "cache_control": {"type": "ephemeral", "ttl": "1h"}})  # fmt: skip
    messages = [USER, system({"type": "text", "text": "now"}, *blocks)]
    admitted = admit({"messages": messages}, ("mid-conversation-tool-changes-2026-07-01",))
    assert admitted.betas == ("mid-conversation-tool-changes-2026-07-01",)
    assert "mid-conversation-tool-changes" in refused({"messages": messages})


# -- strict nested allow-lists ---------------------------------------------------------


def test_text_only_system_messages_need_no_beta() -> None:
    admit({"messages": [USER, system({"type": "text", "text": "x"})]})
    admit({"messages": [USER, {"role": "system", "content": "x"}]})


@pytest.mark.parametrize("effort", sorted(ga.EFFORTS))
def test_effort_values(effort: str) -> None:
    admit({"output_config": {"effort": effort}}, ("effort-2025-11-24",))
    messages = [USER, system(output_config={"effort": effort})]
    admit({"messages": messages}, ("per-turn-control-2026-07-01",))


@pytest.mark.parametrize(
    "config",
    [
        {"effort": "turbo"},
        {"effort": "high", "format": {"type": "json_schema"}},
        {},
        "high",
        {"task_budget": 1},
    ],
)
def test_output_config_is_strict(config: object) -> None:
    refused({"output_config": config}, ("effort-2025-11-24",))
    messages = [USER, system(output_config=config)]
    refused({"messages": messages}, ("per-turn-control-2026-07-01",))


def test_per_turn_effort_needs_its_beta() -> None:
    messages = [USER, system(output_config={"effort": "low"})]
    assert "per-turn-control" in refused({"messages": messages})


@pytest.mark.parametrize(
    "message",
    [
        system({"type": "text", "text": "x"}, clear_at="next_user_message"),
        system({"type": "image", "source": {"type": "base64"}}),
        system({"type": "text", "text": "x", "citations": []}),
        system({"type": "text", "text": "x", "cache_control": {"type": "persistent"}}),
        {"role": "user", "content": "x", "output_config": {"effort": "low"}},
        {"role": "developer", "content": "x"},
    ],
)
def test_system_and_message_shapes_are_strict(message: dict[str, Any]) -> None:
    refused({"messages": [USER, message]}, tuple(ga.CANDIDATE_BETAS))


GOOD_EDITS: list[dict[str, Any]] = [
    {"type": "clear_thinking_20251015", "keep": {"type": "thinking_turns", "value": 2}},
    {"type": "clear_tool_uses_20250919", "trigger": {"type": "input_tokens", "value": 30_000},
     "keep": {"type": "tool_uses", "value": 3},
     "clear_at_least": {"type": "input_tokens", "value": 5_000},
     "exclude_tools": ["Bash"], "clear_tool_inputs": False},
]  # fmt: skip


def test_context_management_passes_its_documented_shape() -> None:
    admit({"context_management": {"edits": GOOD_EDITS}}, ("context-management-2025-06-27",))
    keep_all = [{"type": "clear_thinking_20251015", "keep": "all"}]
    admit({"context_management": {"edits": keep_all}}, ("context-management-2025-06-27",))


@pytest.mark.parametrize(
    "management",
    [
        {"edits": []},
        {"edits": [{"type": "compact_20260112"}]},
        {"edits": [GOOD_EDITS[1] | {"extra": 1}]},
        {"edits": [{"type": "clear_tool_uses_20250919", "trigger": {"type": "turns", "value": 1}}]},
        {
            "edits": [
                {"type": "clear_tool_uses_20250919", "keep": {"type": "tool_uses", "value": -1}}
            ]
        },
        {"edits": [{"type": "clear_tool_uses_20250919", "clear_tool_inputs": "yes"}]},
        {"edits": [{"type": "clear_tool_uses_20250919", "exclude_tools": "Bash"}]},
        {
            "edits": [
                {"type": "clear_thinking_20251015", "keep": {"type": "thinking_turns", "value": 0}}
            ]
        },
        {"edits": [GOOD_EDITS[1], GOOD_EDITS[1]]},
        {"edits": GOOD_EDITS, "other": True},
    ],
)
def test_context_management_is_strict(management: dict[str, Any]) -> None:
    refused({"context_management": management}, ("context-management-2025-06-27",))


@pytest.mark.parametrize("display", ["summarized", "omitted"])
def test_thinking_display(display: str) -> None:
    admit({"thinking": {"type": "adaptive", "display": display}})


def test_thinking_display_updates_needs_its_beta() -> None:
    fields = {"thinking": {"type": "adaptive", "display": "updates"}}
    admit(fields, ("thinking-display-updates-2026-08-18",))
    assert "thinking-display-updates" in refused(fields)
    refused({"thinking": {"type": "adaptive", "display": "full"}})
    refused({"thinking": {"type": "adaptive", "extra": 1}})


# -- counting tokens -------------------------------------------------------------------


def test_a_counting_request_is_checked_but_reserves_nothing() -> None:
    budget = Budget(limit=DOLLAR, policy=CANDIDATES)
    data = json.dumps({"model": MODEL, "messages": [USER], "tools": [TOOL]}).encode()
    checked = budget.check(data, ["claude-code-20250219"])
    assert checked.reservation == 0
    assert budget.spent() == 0
    with pytest.raises(RequestRefused):
        budget.check(json.dumps({"model": MODEL, "messages": [USER],
                                 "tools": [{"type": "web_search_20250305"}]}).encode())  # fmt: skip


# -- the response side ----------------------------------------------------------------


def sse(*events: dict[str, Any]) -> bytes:
    return b"".join(
        b"event: " + str(e.get("type")).encode() + b"\ndata: " + json.dumps(e).encode() + b"\n\n"
        for e in events
    )


START = {"type": "message_start", "message": {"usage": {"input_tokens": 100, "output_tokens": 1}}}
DELTA = {"type": "message_delta", "usage": {"output_tokens": 50}}
STOP = {"type": "message_stop"}


def metered(data: bytes) -> UsageMeter:
    meter = UsageMeter(streamed=True)
    meter.feed(data)
    meter.finish()
    return meter


def test_estimated_thinking_tokens_are_never_counted() -> None:
    thinking = [
        {"type": "content_block_start", "index": 0, "content_block": {"type": "thinking"}},
        {
            "type": "content_block_delta",
            "index": 0,
            "delta": {"type": "thinking_delta", "thinking": "", "estimated_tokens": 90_000},
        },
    ]
    meter = metered(sse(START, *thinking, DELTA, STOP))
    assert meter.valid
    assert meter.output_tokens == 50
    settled = ga.charge(meter, PRICE, DOLLAR)
    assert settled.charged == ga._cost(100, PRICE.input) + ga._cost(50, PRICE.output)


@pytest.mark.parametrize("kind", ["server_tool_use", "web_search_tool_result",
                                  "tool_search_tool_result", "container_upload", None])  # fmt: skip
def test_response_content_outside_the_allow_list_is_untrusted(kind: object) -> None:
    block = {"type": "content_block_start", "index": 0, "content_block": {"type": kind}}
    meter = metered(sse(START, block, DELTA, STOP))
    assert not meter.valid
    settled = ga.charge(meter, PRICE, DOLLAR)
    assert settled.charged == DOLLAR
    assert not settled.trusted


# -- stream framing fails closed (#124 review, 2026-11-14) ---------------------------


def framed(*parts: bytes) -> UsageMeter:
    meter = UsageMeter(streamed=True)
    for part in parts:
        meter.feed(part)
    meter.finish()
    return meter


def raw(event: dict[str, Any], name: str | None = None) -> bytes:
    head = f"event: {name or event['type']}\n".encode()
    return head + b"data: " + json.dumps(event).encode() + b"\n\n"


GOOD_STREAM = sse(START, DELTA, STOP)


def test_a_well_formed_stream_with_comments_pings_and_crlf_is_trusted() -> None:
    ping = {"type": "ping"}
    stop_crlf = raw(STOP).replace(b"\n", b"\r\n")
    meter = framed(b": keep-alive\n\n", raw(START), raw(ping), raw(DELTA), stop_crlf)
    assert meter.valid, meter.invalid


@pytest.mark.parametrize(
    "stream",
    [
        raw(START) + raw({"type": "message_billing_notice", "cost": 1}) + raw(DELTA) + raw(STOP),
        raw(START) + raw({"type": "server_tool_progress"}) + raw(DELTA) + raw(STOP),
        raw(START) + raw({"type": "content_block_delta", "index": 0,
                          "delta": {"type": "web_search_delta"}}) + raw(DELTA) + raw(STOP),
        raw(START, name="message_delta") + raw(DELTA) + raw(STOP),  # name and type disagree
        raw(START) + b"id: 7\n" + raw(DELTA) + raw(STOP),  # an unknown field
        raw(START) + raw(DELTA) + raw(STOP) + raw(DELTA),  # an event after message_stop
        raw(START) + b"event: message_delta\n\n" + raw(DELTA) + raw(STOP),  # no data
    ],
)  # fmt: skip
def test_an_unknown_event_or_field_fails_the_report_closed(stream: bytes) -> None:
    meter = framed(stream)
    assert not meter.valid
    settled = ga.charge(meter, PRICE, DOLLAR)
    assert (settled.charged, settled.trusted) == (DOLLAR, False)


@pytest.mark.parametrize(
    "tail",
    [
        b'event: message_delta\ndata: {"type": "message_del',  # cut mid-line
        b"event: message_delta\ndata: " + json.dumps(DELTA).encode() + b"\n",  # no blank line
        b"event: ping\n",  # a name and nothing more
        b"garbage-without-newline",
        b"data: {not json}\n\n",
    ],
)
def test_truncated_or_malformed_trailing_data_invalidates_settlement(tail: bytes) -> None:
    meter = framed(GOOD_STREAM[: -len(b"\n")] + b"\n", tail)  # a complete report, then the tail
    assert not meter.valid
    settled = ga.charge(meter, PRICE, DOLLAR)
    assert (settled.charged, settled.trusted) == (DOLLAR, False)


def test_a_report_cut_before_its_final_blank_line_is_truncated() -> None:
    meter = framed(GOOD_STREAM[:-1])  # message_stop's data line, without the blank line
    assert not meter.valid
    assert meter.invalid == "stream truncated"


def test_a_rejection_from_outside_the_body_always_wins() -> None:
    meter = UsageMeter(streamed=True)
    meter.feed(GOOD_STREAM)
    meter.reject("provider status 500")
    meter.finish()
    assert not meter.valid
    assert ga.charge(meter, PRICE, DOLLAR).charged == DOLLAR
