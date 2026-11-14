# SPDX-License-Identifier: Apache-2.0
"""Spending control for the model gateway (ADR-0037 §2, #111 P5).

Like ``model_gateway.py``, this module runs inside the gateway sidecar, so it
uses only the standard library and never imports ``ilmar``.

**What is hard and what is estimated.** One sandbox attempt has a budget ``B``
in micro-dollars, its ledger reservation.

* *Output is bounded by the provider.* A response never has more output
  tokens than the request's ``max_tokens``; thinking counts within it. The
  gateway sets ``max_tokens`` itself (below), so the output term of every
  forwarded request is a hard bound, at the output rate.
* *Input is only estimated.* What the provider bills as input is not a
  function of the request's bytes that anyone outside the provider can prove:
  it adds its own system and tool prompts and message framing, and tokenizes
  in its own way. So the input term is an **estimate**: the body's bytes, plus
  per-message, tool-prompt and (when allowed) per-image allowances, at the
  dearest input-side rate (input, either cache write or cache read; scaled to
  the long-context rates above the model's threshold).

**The rule.** A request is forwarded only if

    settled + in flight + this request's reservation <= B

and only while the attempt is not *halted*. Each finished request is charged
what the provider reported, in full, never capped at its reservation:

* a complete, valid usage report is charged at the attested prices;
* a missing, partial, malformed or unrecognised report, or a stream cut off
  before ``message_stop``, is charged its whole reservation, even when
  ``message_stop`` arrived, and **halts the attempt**: what the provider will
  bill for it is unknown, so the gateway forwards nothing more for it;
* a report that costs more than the reservation (the estimate was wrong) is
  charged in full, recorded as an *overrun*, and also **halts the attempt**.

Only a complete, valid report within the reservation lets the attempt go on.

**The guarantee that follows.** The attempt's spend is at most ``B`` plus the
overruns of the requests in flight when it halted, plus whatever the provider
bills for requests whose usage could not be trusted (at most the one that
halted it and those then in flight); with ``max_in_flight`` 1, one request.
Neither an overrun nor an untrusted request has a bound this gateway can prove.
That is why the provider's own workspace spend cap stays the hard backstop
(ADR-0029 §5), and why a halt is an operator-visible event whose charges also
reach the coordinator's ledger (P5 part 2).

**What is refused** (never forwarded): a model without an attested price; any
top-level field outside the allow-list; server tools; documents, URL images,
search results and every other content type outside the allow-list; images
unless the operator attested a per-image token bound; a ``service_tier`` other
than ``standard_only``; ``mcp_servers`` and ``container``; a thinking budget
not below ``max_tokens``; a body that is not one JSON object; and any
``anthropic-beta`` feature not on the operator's allow-list, or in a family
known to be billed separately (``BILLED_BETA_FAMILIES``).

**What is constrained.** ``max_tokens`` is lowered to ``max_output_tokens``,
then, if needed, to the largest value that fits ``B``, never below
``min_output_tokens``; below that, the request is refused
(``budget_exceeded``). Every lowering is reported in ``Admitted.capped_from``.

Prices are integers: micro-dollars per million tokens. Costs are integers in
micro-dollars, always rounded up.
"""

from __future__ import annotations

import json
import re
import threading
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

MILLION = 1_000_000
REPORTED = "reported"
"""The settlement reason of a complete, valid usage report."""

TOP_LEVEL_FIELDS = frozenset(
    {
        "model", "messages", "system", "max_tokens", "metadata", "stop_sequences",
        "stream", "temperature", "top_k", "top_p", "tools", "tool_choice", "thinking",
        "service_tier", "output_config", "context_management",
    }
)  # fmt: skip
"""Everything else is refused until it is priced and reviewed."""
CONTENT_TYPES = frozenset(
    {"text", "image", "tool_use", "tool_result", "thinking", "redacted_thinking"}
)
"""``document``, ``search_result``, server-tool blocks and anything new are refused."""
TOOL_RESULT_TYPES = frozenset({"text", "image"})
THINKING_TYPES = frozenset({"enabled", "disabled", "adaptive"})
BILLED_BETA_FAMILIES = (
    "context-1m",  # long-context pricing
    "code-execution",  # billed container time
    "web-search",
    "web-fetch",
    "mcp-client",  # remote tools
    "files-api",  # stored files read by the provider
    "computer-use",  # a hidden tool prompt and screenshots
    "skills",
)
"""Refused even when the operator allows them: priced outside this module's model."""

ADVANCED_TOOL_USE = "advanced-tool-use-2025-11-20"
CONTEXT_MANAGEMENT = "context-management-2025-06-27"
PER_TURN_CONTROL = "per-turn-control-2026-07-01"
TOOL_CHANGES = "mid-conversation-tool-changes-2026-07-01"
DISPLAY_UPDATES = "thinking-display-updates-2026-08-18"
INTERLEAVED_THINKING = "interleaved-thinking-2025-05-14"
EFFORT = "effort-2025-11-24"
THINKING_TOKEN_COUNT = "thinking-token-count-2026-05-13"  # noqa: S105 - a feature name
CANDIDATE_BETAS = frozenset(
    {
        "claude-code-20250219", INTERLEAVED_THINKING, EFFORT, PER_TURN_CONTROL,
        TOOL_CHANGES, DISPLAY_UPDATES, CONTEXT_MANAGEMENT, THINKING_TOKEN_COUNT,
    }
)  # fmt: skip
"""Reviewed for pinned Claude Code 2.1.318 and approved as candidates (maintainer,
2026-11-13). Still enabled only through ``allowed_betas``; nothing is allowed by
default. ``thinking-token-count``: the ``estimated_tokens`` it adds to
``thinking_delta`` events is not billable and never counted."""
NEVER_ALLOWED_BETAS = frozenset({ADVANCED_TOOL_USE})
"""Denied server-side capabilities: refused when their features are used, and
dropped from the header when they are not. They cannot be allowed."""
EFFORTS = frozenset({"low", "medium", "high", "xhigh", "max"})
THINKING_KEYS = frozenset({"type", "budget_tokens", "display"})
DISPLAYS = frozenset({"summarized", "omitted"})
"""``updates`` also, with ``thinking-display-updates``."""
TOOL_KEYS = frozenset({"type", "name", "description", "input_schema", "cache_control"})
"""Custom tools only. ``defer_loading``, ``allowed_callers``, ``input_examples``
and anything else are refused: they belong to denied or unreviewed features."""
MESSAGE_KEYS = frozenset({"role", "content"})
SYSTEM_MESSAGE_KEYS = frozenset({"role", "content", "output_config"})
SYSTEM_MESSAGE_BLOCKS = frozenset({"text", "tool_addition", "tool_removal"})
TOOL_CHANGE_BLOCK_KEYS = frozenset({"type", "tool", "cache_control"})
TEXT_BLOCK_KEYS = frozenset({"type", "text", "cache_control"})
CACHE_TTLS = frozenset({"5m", "1h"})
RESPONSE_BLOCKS = frozenset({"text", "thinking", "redacted_thinking", "tool_use"})
STREAM_EVENTS = frozenset(
    {
        "message_start", "message_delta", "message_stop", "content_block_start",
        "content_block_delta", "content_block_stop", "ping", "error",
    }
)  # fmt: skip
"""Stream events the meter understands; any other fails the report closed."""
DELTA_TYPES = frozenset({"text_delta", "input_json_delta", "thinking_delta", "signature_delta"})
"""Response content the meter understands; any other (a server tool's) halts."""
USAGE_KEYS = frozenset(
    {
        "input_tokens", "output_tokens", "cache_creation_input_tokens",
        "cache_read_input_tokens", "cache_creation", "server_tool_use", "service_tier",
    }
)  # fmt: skip
"""A usage report with any other key is not understood and keeps the reservation."""
CACHE_CREATION_KEYS = frozenset({"ephemeral_5m_input_tokens", "ephemeral_1h_input_tokens"})


@dataclass(frozen=True, slots=True)
class ModelPrice:
    """An operator-attested price, in micro-dollars per million tokens."""

    input: int
    output: int
    cache_write_5m: int
    cache_write_1h: int
    cache_read: int
    long_context_threshold: int | None = None
    """Input tokens above which the long-context rates apply."""
    long_context_input: int | None = None
    long_context_output: int | None = None

    def __post_init__(self) -> None:
        rates = (self.input, self.output, self.cache_write_5m, self.cache_write_1h,
                 self.cache_read)  # fmt: skip
        if not all(_count(r) and r > 0 for r in rates):
            raise ValueError("every rate is a positive integer")
        long = (self.long_context_threshold, self.long_context_input, self.long_context_output)
        if any(v is not None for v in long) and not all(
            isinstance(v, int) and _count(v) and v > 0 for v in long
        ):
            raise ValueError("long-context pricing needs a threshold and both rates")

    def long_context(self, input_tokens: int) -> bool:
        threshold = self.long_context_threshold
        return threshold is not None and input_tokens > threshold

    def input_rates(self, input_tokens: int) -> tuple[int, int, int, int]:
        """Input, 5-minute write, 1-hour write and read rates at this input size:
        above the threshold, each scaled by long-context input / input, rounded up."""
        rates = (self.input, self.cache_write_5m, self.cache_write_1h, self.cache_read)
        if not self.long_context(input_tokens) or self.long_context_input is None:
            return rates
        scale = self.long_context_input
        first, second, third, fourth = (-(-r * scale // self.input) for r in rates)
        return first, second, third, fourth

    def output_rate(self, input_tokens: int) -> int:
        if not self.long_context(input_tokens) or self.long_context_output is None:
            return self.output
        return max(self.output, self.long_context_output)


@dataclass(frozen=True, slots=True)
class AdmissionPolicy:
    prices: Mapping[str, ModelPrice]
    max_output_tokens: int = 32_000
    min_output_tokens: int = 1_024
    tool_overhead_tokens: int = 1_000
    """The provider's hidden tool-use prompt, per request with tools (an estimate)."""
    message_overhead_tokens: int = 32
    """Framing per message, content block and system block (an estimate)."""
    image_tokens: int | None = None
    """An attested bound per image block; ``None`` refuses images."""
    allowed_betas: frozenset[str] = frozenset()
    """``anthropic-beta`` values the operator reviewed; any other is refused."""

    def __post_init__(self) -> None:
        if not self.prices:
            raise ValueError("at least one priced model is needed")
        if not 1 <= self.min_output_tokens <= self.max_output_tokens:
            raise ValueError("need 1 <= min_output_tokens <= max_output_tokens")
        if self.tool_overhead_tokens < 0 or self.message_overhead_tokens < 0:
            raise ValueError("allowances must not be negative")
        if self.image_tokens is not None and self.image_tokens < 1:
            raise ValueError("image_tokens must be positive, or None to refuse images")
        billed = sorted(
            b for b in self.allowed_betas if _billed_beta(b) or b in NEVER_ALLOWED_BETAS
        )
        if billed:
            raise ValueError(f"beta features billed separately cannot be allowed: {billed}")


class RequestRefused(Exception):  # noqa: N818 - a refusal, answered to the client
    """Not forwarded. ``kind`` and the message go back in the provider's error shape."""

    def __init__(self, kind: str, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.kind = kind
        self.status = status


@dataclass(frozen=True, slots=True)
class Admitted:
    body: bytes
    """The body to forward: the original bytes, or re-encoded with a lower ``max_tokens``."""
    reservation: int
    model: str
    max_tokens: int
    input_estimate: int
    capped_from: int | None = None
    """The ``max_tokens`` the client asked for, when it was lowered."""
    betas: tuple[str, ...] = ()
    """The ``anthropic-beta`` values to forward, in the order sent."""
    dropped: tuple[str, ...] = ()
    """Values the client sent that are dropped because their feature is unused."""
    streamed: bool = False


@dataclass(frozen=True, slots=True)
class Settled:
    charged: int
    reservation: int
    reason: str
    """``reported``, or why the whole reservation was kept (``incomplete``, a usage problem)."""

    @property
    def overrun(self) -> int:
        return max(0, self.charged - self.reservation)

    @property
    def trusted(self) -> bool:
        """A complete, valid report: the only settlement that lets the attempt go on."""
        return self.reason == REPORTED


@dataclass
class Budget:
    """One attempt's spending, shared by the gateway's request threads."""

    limit: int
    policy: AdmissionPolicy
    settled: int = 0
    overruns: int = 0
    halted: bool = False
    halt_reason: str | None = None
    """Why the attempt stopped: the first untrusted report, or an overrun."""
    in_flight: dict[int, int] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    _next: int = 0

    def __post_init__(self) -> None:
        if self.limit < 1:
            raise ValueError("the attempt's budget must be positive")

    def admit(self, body: bytes, betas: Iterable[str] = ()) -> tuple[int, Admitted]:
        """Check, constrain and reserve; ``(ticket, admitted)``, or ``RequestRefused``."""
        request = parse(body)
        kept, dropped = resolve_betas(betas, request, self.policy)
        model, price, input_tokens, requested = assess(request, len(body), self.policy, kept)
        input_cost = _cost(input_tokens, max(price.input_rates(input_tokens)))
        output_rate = price.output_rate(input_tokens)
        wanted = min(requested, self.policy.max_output_tokens)
        with self._lock:
            if self.halted:
                raise RequestRefused(
                    "budget_exceeded",
                    f"this attempt is stopped: {self.halt_reason}",
                    status=429,
                )
            room = self.limit - self.settled - sum(self.in_flight.values()) - input_cost
            affordable = room * MILLION // output_rate if room > 0 else 0
            granted = min(wanted, affordable)
            if granted < self.policy.min_output_tokens:
                raise RequestRefused(
                    "budget_exceeded",
                    "this attempt's model budget cannot cover the request's reservation",
                    status=429,
                )
            _check_thinking(request, granted)
            reservation = input_cost + _cost(granted, output_rate)
            ticket = self._next
            self._next += 1
            self.in_flight[ticket] = reservation
        if granted != requested:
            request["max_tokens"] = granted
            rewritten = json.dumps(request, separators=(",", ":"), ensure_ascii=False).encode()
            if len(rewritten) > len(body):  # the estimate was taken from the original
                self.release(ticket)
                raise RequestRefused("invalid_request_error", "the request cannot be re-encoded")
            body = rewritten
        capped = requested if granted != requested else None
        streamed = request.get("stream") is True
        return ticket, Admitted(
            body, reservation, model, granted, input_tokens, capped, kept, dropped, streamed
        )

    def check(self, body: bytes, betas: Iterable[str] = ()) -> Admitted:
        """Validate a token-counting request: the same checks, nothing reserved
        (counting is not billed). The halt applies to it too."""
        request = parse(body)
        kept, dropped = resolve_betas(betas, request, self.policy)
        model, _, input_tokens, _ = assess(request, len(body), self.policy, kept, counting=True)
        with self._lock:
            if self.halted:
                raise RequestRefused(
                    "budget_exceeded", f"this attempt is stopped: {self.halt_reason}", status=429
                )
        return Admitted(body, 0, model, 0, input_tokens, None, kept, dropped)

    def settle(self, ticket: int, settled: Settled) -> Settled:
        """Close a request at what it cost, in full. An overrun, or a usage report
        that is missing, incomplete or not trusted, halts the attempt."""
        with self._lock:
            self.in_flight.pop(ticket)
            self.settled += settled.charged
            if settled.overrun:
                self.overruns += settled.overrun
            reason = None
            if not settled.trusted:
                reason = f"the provider's usage report was not trusted ({settled.reason})"
            elif settled.overrun:
                reason = "a request cost more than its reservation"
            if reason is not None and not self.halted:
                self.halted, self.halt_reason = True, reason
        return settled

    def release(self, ticket: int) -> None:
        """Drop the reservation of a request that was never sent."""
        with self._lock:
            self.in_flight.pop(ticket)

    def halt(self, reason: str) -> None:
        """Stop the attempt without a charge: for a request that is not billed
        (counting tokens) whose provider failed or could not be reached. What
        made it fail may affect billed requests too, so nothing more goes."""
        with self._lock:
            if not self.halted:
                self.halted, self.halt_reason = True, reason

    def spent(self) -> int:
        with self._lock:
            return self.settled + sum(self.in_flight.values())


# -- checking a request ---------------------------------------------------------------


def _billed_beta(beta: str) -> bool:
    return any(beta.startswith(family) for family in BILLED_BETA_FAMILIES)


_BETA_NAME = re.compile(r"^[a-z0-9][a-z0-9.-]{0,79}$")


def split_betas(values: Iterable[str]) -> list[str]:
    """Every value of every ``anthropic-beta`` header, split on commas, in order,
    without repeats. The provider reads all of them, so all are checked."""
    betas: list[str] = []
    for value in values:
        for beta in (part.strip() for part in value.split(",")):
            if beta and beta not in betas:
                betas.append(beta)
    return betas


def resolve_betas(
    values: Iterable[str], request: Mapping[str, Any], policy: AdmissionPolicy
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """``(kept, dropped)``. A beta is kept when the operator allowed it; dropped
    when it is not allowed but its feature is visibly unused in this request;
    otherwise the request is refused. A beta whose use cannot be seen in the
    request (no detector) is never dropped."""
    kept: list[str] = []
    dropped: list[str] = []
    for beta in split_betas(values):
        if not _BETA_NAME.match(beta):
            raise RequestRefused("invalid_request_error", "malformed anthropic-beta value")
        denied = _billed_beta(beta) or beta in NEVER_ALLOWED_BETAS
        if beta in policy.allowed_betas and not denied:
            kept.append(beta)
            continue
        detector = FEATURE_DETECTORS.get(beta)
        if detector is not None and not detector(request):
            dropped.append(beta)
            continue
        raise RequestRefused("invalid_request_error", f"beta feature not allowed: {beta}")
    return tuple(kept), tuple(dropped)


def _tools(request: Mapping[str, Any]) -> list[Any]:
    tools = request.get("tools")
    return tools if isinstance(tools, list) else []


def _system_messages(request: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    messages = request.get("messages")
    if not isinstance(messages, list):
        return []
    return [m for m in messages if isinstance(m, dict) and m.get("role") == "system"]


def _system_blocks(request: Mapping[str, Any]) -> list[Any]:
    blocks: list[Any] = []
    for message in _system_messages(request):
        content = message.get("content")
        if isinstance(content, list):
            blocks.extend(content)
    return blocks


def _all_block_types(request: Mapping[str, Any]) -> set[object]:
    kinds: set[object] = set()
    messages = request.get("messages")
    for message in messages if isinstance(messages, list) else []:
        content = message.get("content") if isinstance(message, dict) else None
        for block in content if isinstance(content, list) else []:
            kinds.add(block.get("type") if isinstance(block, dict) else None)
    return kinds


_ADVANCED_TOOL_KEYS = frozenset({"defer_loading", "allowed_callers", "input_examples"})
_ADVANCED_BLOCKS = ("server_tool_use", "tool_search", "tool_reference")


def _uses_advanced_tool_use(request: Mapping[str, Any]) -> bool:
    for tool in _tools(request):
        if not isinstance(tool, dict):
            return True  # cannot tell: treat as used
        kind = tool.get("type")
        if set(tool) & _ADVANCED_TOOL_KEYS or (
            isinstance(kind, str) and kind.startswith("tool_search")
        ):
            return True
    return any(
        isinstance(kind, str) and kind.startswith(_ADVANCED_BLOCKS)
        for kind in _all_block_types(request)
    )


def _thinking_field(request: Mapping[str, Any], key: str) -> object:
    thinking = request.get("thinking")
    return thinking.get(key) if isinstance(thinking, dict) else None


FEATURE_DETECTORS: Mapping[str, Any] = {
    ADVANCED_TOOL_USE: _uses_advanced_tool_use,
    CONTEXT_MANAGEMENT: lambda r: "context_management" in r,
    PER_TURN_CONTROL: lambda r: any("output_config" in m for m in _system_messages(r)),
    TOOL_CHANGES: lambda r: any(
        isinstance(b, dict) and b.get("type") in ("tool_addition", "tool_removal")
        for b in _system_blocks(r)
    ),
    DISPLAY_UPDATES: lambda r: _thinking_field(r, "display") == "updates",
    INTERLEAVED_THINKING: lambda r: _thinking_field(r, "type") == "enabled",
    EFFORT: lambda r: "output_config" in r,
}
"""Whether a request uses a beta's feature, for the betas whose use is visible
in the request. Only these can be dropped when not allowed."""


def parse(body: bytes) -> dict[str, Any]:
    try:
        request = json.loads(body)
    except (ValueError, UnicodeDecodeError) as error:
        raise RequestRefused("invalid_request_error", "the body is not JSON") from error
    if not isinstance(request, dict):
        raise RequestRefused("invalid_request_error", "the body is not a JSON object")
    return request


def assess(
    request: Mapping[str, Any],
    body_bytes: int,
    policy: AdmissionPolicy,
    betas: tuple[str, ...] = (),
    *,
    counting: bool = False,
) -> tuple[str, ModelPrice, int, int]:
    """Validate; return the model, its price, the input estimate and the requested
    max. ``betas`` are the kept beta values: a shape that needs one is refused
    without it. ``counting``: a token-counting request has no ``max_tokens``."""
    unknown = sorted(set(request) - TOP_LEVEL_FIELDS)
    if unknown:
        raise RequestRefused("invalid_request_error", f"field not allowed here: {unknown[0]}")
    model = request.get("model")
    price = policy.prices.get(model) if isinstance(model, str) else None
    if price is None or not isinstance(model, str):
        raise RequestRefused("permission_error", "model not allowed by this gateway", 403)
    requested: object = 0
    if not counting or "max_tokens" in request:
        requested = request.get("max_tokens")
        if not isinstance(requested, int) or not _count(requested) or requested < 1:
            raise _refuse("max_tokens must be a positive integer")
    if request.get("service_tier", "standard_only") != "standard_only":
        raise RequestRefused("invalid_request_error", "only the standard service tier")
    tools = request.get("tools")
    names = _check_tools(tools) if tools is not None else frozenset()
    images, units = _content(request, betas, names)
    if images and policy.image_tokens is None:
        raise RequestRefused("invalid_request_error", "images are not allowed by this gateway")
    _check_thinking_shape(request.get("thinking"), betas)
    if "output_config" in request:
        _check_output_config(request["output_config"])
    if "context_management" in request:
        _need(CONTEXT_MANAGEMENT, betas, "context_management")
        _check_context_management(request["context_management"])
    input_tokens = (
        body_bytes
        + units * policy.message_overhead_tokens
        + images * (policy.image_tokens or 0)
        + (policy.tool_overhead_tokens if tools else 0)
    )
    if price.long_context_threshold is None and input_tokens > 200_000:
        raise RequestRefused("request_too_large", "too large for this model's attested prices", 413)
    return model, price, input_tokens, requested if isinstance(requested, int) else 0


def _refuse(message: str) -> RequestRefused:
    return RequestRefused("invalid_request_error", message)


def _need(beta: str, betas: tuple[str, ...], what: str) -> None:
    if beta not in betas:
        raise _refuse(f"{what} needs the {beta} beta, which is not allowed")


def _keys(value: object, allowed: frozenset[str], what: str) -> Mapping[str, Any]:
    """``value`` as an object whose keys are all in ``allowed``."""
    if not isinstance(value, dict):
        raise _refuse(f"{what} must be an object")
    extra = sorted(set(value) - allowed)
    if extra:
        raise _refuse(f"{what}: field not allowed here: {extra[0]}")
    return value


def _check_cache_control(value: object) -> None:
    control = _keys(value, frozenset({"type", "ttl"}), "cache_control")
    if control.get("type") != "ephemeral" or control.get("ttl", "5m") not in CACHE_TTLS:
        raise _refuse("cache_control must be ephemeral, with a ttl of 5m or 1h")


def _check_tools(tools: object) -> frozenset[str]:
    """Custom tools only, with reviewed fields; their names."""
    if not isinstance(tools, list):
        raise _refuse("tools must be a list")
    names: set[str] = set()
    for tool in tools:
        if not isinstance(tool, dict) or tool.get("type", "custom") != "custom":
            raise _refuse("only custom tools; server tools are refused")
        _keys(tool, TOOL_KEYS, "tool")
        name = tool.get("name")
        if not isinstance(name, str) or not name:
            raise _refuse("a tool needs a name")
        if "cache_control" in tool:
            _check_cache_control(tool["cache_control"])
        names.add(name)
    return frozenset(names)


def _check_thinking_shape(thinking: object, betas: tuple[str, ...] = ()) -> None:
    if thinking is None:
        return
    shape = _keys(thinking, THINKING_KEYS, "thinking")
    if shape.get("type") not in THINKING_TYPES:
        raise _refuse("thinking type not allowed")
    budget = shape.get("budget_tokens")
    if budget is not None and not _count(budget):
        raise _refuse("thinking budget must be an integer")
    if "display" in shape:
        display = shape["display"]
        if display == "updates":
            _need(DISPLAY_UPDATES, betas, 'thinking.display "updates"')
        elif display not in DISPLAYS:
            raise _refuse("thinking.display not allowed")


def _check_thinking(request: Mapping[str, Any], max_tokens: int) -> None:
    thinking = request.get("thinking")
    budget = thinking.get("budget_tokens") if isinstance(thinking, dict) else None
    if isinstance(budget, int) and budget >= max_tokens:
        raise _refuse(
            f"thinking budget must be below max_tokens ({max_tokens} after the gateway's limit)"
        )


def _check_output_config(value: object) -> None:
    config = _keys(value, frozenset({"effort"}), "output_config")
    if config.get("effort") not in EFFORTS:
        raise _refuse("output_config.effort must be low, medium, high, xhigh or max")


def _counter(value: object, kinds: frozenset[str], what: str, *, positive: bool = False) -> None:
    counter = _keys(value, frozenset({"type", "value"}), what)
    number = counter.get("value")
    if counter.get("type") not in kinds or not _count(number):
        raise _refuse(f"{what} is not understood")
    if positive and number == 0:
        raise _refuse(f"{what} must be positive")


_CLEAR_TOOL_USES = "clear_tool_uses_20250919"
_CLEAR_THINKING = "clear_thinking_20251015"


def _check_context_management(value: object) -> None:
    edits = _keys(value, frozenset({"edits"}), "context_management").get("edits")
    if not isinstance(edits, list) or not 1 <= len(edits) <= 2:
        raise _refuse("context_management.edits must list one or two edits")
    seen: set[object] = set()
    for edit in edits:
        kind = edit.get("type") if isinstance(edit, dict) else None
        if kind in seen:
            raise _refuse("context_management: an edit type is repeated")
        seen.add(kind)
        if kind == _CLEAR_TOOL_USES:
            fields = frozenset({"type", "trigger", "keep", "clear_at_least", "exclude_tools",
                                "clear_tool_inputs"})  # fmt: skip
            shape = _keys(edit, fields, "context_management edit")
            if "trigger" in shape:
                _counter(shape["trigger"], frozenset({"input_tokens", "tool_uses"}), "trigger")
            if "keep" in shape:
                _counter(shape["keep"], frozenset({"tool_uses"}), "keep")
            if "clear_at_least" in shape:
                _counter(shape["clear_at_least"], frozenset({"input_tokens"}), "clear_at_least")
            exclude = shape.get("exclude_tools", [])
            if not isinstance(exclude, list) or not all(isinstance(n, str) for n in exclude):
                raise _refuse("exclude_tools must list tool names")
            if not isinstance(shape.get("clear_tool_inputs", False), bool):
                raise _refuse("clear_tool_inputs must be true or false")
        elif kind == _CLEAR_THINKING:
            shape = _keys(edit, frozenset({"type", "keep"}), "context_management edit")
            if "keep" in shape and shape["keep"] != "all":
                _counter(shape["keep"], frozenset({"thinking_turns"}), "keep", positive=True)
        else:
            raise _refuse(f"context_management edit not allowed: {kind}")


def _content(
    request: Mapping[str, Any], betas: tuple[str, ...] = (), tools: frozenset[str] = frozenset()
) -> tuple[int, int]:
    """(image blocks, framing units); refuse anything outside the allow-list.
    A framing unit is a system block, a message or a content block."""
    images = 0
    units = 0
    system = request.get("system")
    if isinstance(system, list):
        images += _blocks(system)
        units += len(system)
    elif isinstance(system, str):
        units += 1
    elif system is not None:
        raise _refuse("system must be text or blocks")
    messages = request.get("messages")
    if not isinstance(messages, list):
        raise _refuse("messages must be a list")
    for message in messages:
        if not isinstance(message, dict):
            raise _refuse("a message must be an object")
        units += 1
        role = message.get("role")
        if role == "system":
            units += _system_message(message, betas, tools)
            continue
        if role not in ("user", "assistant"):
            raise _refuse("message role not allowed")
        _keys(message, MESSAGE_KEYS, "message")
        content = message.get("content")
        if isinstance(content, list):
            images += _blocks(content)
            units += len(content)
        elif not isinstance(content, str):
            raise _refuse("content must be text or blocks")
    return images, units


def _system_message(
    message: Mapping[str, Any], betas: tuple[str, ...], tools: frozenset[str]
) -> int:
    """A mid-conversation system message: text, tool changes by reference to a
    declared custom tool, and per-turn effort. Its content blocks, counted."""
    _keys(message, SYSTEM_MESSAGE_KEYS, "system message")
    if "output_config" in message:
        _need(PER_TURN_CONTROL, betas, "a system message's output_config")
        _check_output_config(message["output_config"])
    content = message.get("content")
    if isinstance(content, str):
        return 0
    if not isinstance(content, list):
        raise _refuse("system message content must be text or blocks")
    for block in content:
        kind = block.get("type") if isinstance(block, dict) else None
        if kind == "text":
            shape = _keys(block, TEXT_BLOCK_KEYS, "text block")
            if not isinstance(shape.get("text"), str):
                raise _refuse("a text block needs text")
        elif kind in ("tool_addition", "tool_removal"):
            _need(TOOL_CHANGES, betas, str(kind))
            shape = _keys(block, TOOL_CHANGE_BLOCK_KEYS, str(kind))
            tool = _keys(shape.get("tool"), frozenset({"type", "name"}), f"{kind}.tool")
            if tool.get("type") != "tool_reference":
                raise _refuse("tool changes only by reference to a declared tool")
            if tool.get("name") not in tools:
                raise _refuse("a tool change must name a declared custom tool")
        else:
            raise _refuse(f"content type not allowed in a system message: {kind}")
        if "cache_control" in block:
            _check_cache_control(block["cache_control"])
    return len(content)


def _blocks(blocks: list[Any], allowed: frozenset[str] = CONTENT_TYPES) -> int:
    count = 0
    for block in blocks:
        kind = block.get("type") if isinstance(block, dict) else None
        if kind not in allowed:
            raise RequestRefused("invalid_request_error", f"content type not allowed: {kind}")
        if kind == "image":
            source = block.get("source")
            if not isinstance(source, dict) or source.get("type") != "base64":
                raise RequestRefused("invalid_request_error", "only base64 images")
            count += 1
        elif kind == "tool_result":
            inner = block.get("content")
            if isinstance(inner, list):
                count += _blocks(inner, TOOL_RESULT_TYPES)
            elif inner is not None and not isinstance(inner, str):
                raise RequestRefused("invalid_request_error", "tool result content")
    return count


# -- settling a response --------------------------------------------------------------


@dataclass
class UsageMeter:
    """Reads the usage a response reports, streamed or not.

    A report is *valid* only when it is complete and every field is understood:

    * streamed: ``message_start`` with a usage that has integer ``input_tokens``
      and ``output_tokens``, at least one ``message_delta`` with an integer
      ``output_tokens``, then ``message_stop``;
    * plain: a ``message`` with a ``stop_reason`` and such a usage.

    Anything else makes it invalid: a missing or partial usage, a count that is
    not a non-negative integer, an unknown usage field, server-tool use, response
    content outside ``RESPONSE_BLOCKS``, a stream event outside
    ``STREAM_EVENTS`` or a delta outside ``DELTA_TYPES``, an event after
    ``message_stop``, a stream that does not end on an event boundary
    (truncated or malformed trailing data), a tier other than ``standard``, cache
    writes that do not add up, an ``error`` event, an unreadable event or body,
    or a reason from outside the body (``reject``: a non-200 status).

    Content deltas are never counted: the ``estimated_tokens`` that
    ``thinking-token-count`` adds to ``thinking_delta`` is not billable, and only
    ``usage.output_tokens`` is charged.
    """

    streamed: bool
    input_tokens: int = 0
    cache_write_5m: int = 0
    cache_write_1h: int = 0
    cache_read: int = 0
    output_tokens: int = 0
    started: bool = False
    delta: bool = False
    stopped: bool = False
    invalid: str = ""
    _buffer: bytes = b""
    _plain: bytearray = field(default_factory=bytearray)
    _name: bytes | None = None
    _data: list[bytes] = field(default_factory=list)

    @property
    def valid(self) -> bool:
        return not self.invalid and self.started and self.delta and self.stopped

    def feed(self, chunk: bytes) -> None:
        if not self.streamed:
            self._plain += chunk
            return
        self._buffer += chunk
        while b"\n" in self._buffer:
            line, _, self._buffer = self._buffer.partition(b"\n")
            self._line(line.removesuffix(b"\r"))

    def reject(self, why: str) -> None:
        """Mark the report untrusted for a reason outside the body (a non-200
        status, a cut-off connection): the whole reservation is charged."""
        self._bad(why)

    def _line(self, line: bytes) -> None:
        """One server-sent-events line. Only ``event:`` and ``data:`` fields
        are understood; a blank line ends an event."""
        if not line:
            self._dispatch()
            return
        if line.startswith(b":"):
            return  # a comment, as the format allows
        name, _, value = line.partition(b":")
        value = value.removeprefix(b" ")
        if name == b"event":
            if self._name is not None:
                self._bad("event name repeated")
            self._name = value
        elif name == b"data":
            self._data.append(value)
        else:
            self._bad("stream field not understood")

    def _dispatch(self) -> None:
        if self._name is None and not self._data:
            return  # blank lines between events
        name, data = self._name, b"\n".join(self._data)
        self._name, self._data = None, []
        if not data:
            self._bad("event without data")
            return
        self._event(data, name)

    def finish(self) -> None:
        if self.streamed:
            # A stream must end on an event boundary: anything left is truncated
            # or malformed, so nothing in it can be trusted.
            if self._buffer.strip() or self._name is not None or self._data:
                self._bad("stream truncated")
            return
        try:
            message = json.loads(bytes(self._plain))
        except ValueError:
            self._bad("body unreadable")
            return
        if not isinstance(message, dict) or message.get("type") != "message":
            self._bad("not a message")
            return
        self._usage(message.get("usage"), start=True)
        content = message.get("content")
        for block in content if isinstance(content, list) else [None]:
            self._block(block.get("type") if isinstance(block, dict) else None)
        self.delta = self.started  # a plain message reports its final usage at once
        self.stopped = message.get("stop_reason") is not None

    def _bad(self, why: str) -> None:
        self.invalid = self.invalid or why

    def _block(self, kind: object) -> None:
        """Response content outside ``RESPONSE_BLOCKS`` (a server tool's, or
        anything new) may be billed in ways this meter cannot see: untrusted."""
        if kind not in RESPONSE_BLOCKS:
            self._bad(f"response content not understood: {kind}")

    def _event(self, data: bytes, name: bytes | None = None) -> None:
        try:
            event = json.loads(data)
        except ValueError:
            self._bad("event unreadable")
            return
        if not isinstance(event, dict):
            self._bad("event unreadable")
            return
        kind = event.get("type")
        if kind not in STREAM_EVENTS:
            self._bad(f"stream event not understood: {kind}")  # fail closed
            return
        if name is not None and name.decode("utf-8", "replace") != kind:
            self._bad("event name and type disagree")
            return
        if self.stopped:
            self._bad("event after message_stop")
            return
        if kind == "message_start":
            message = event.get("message")
            self._usage(message.get("usage") if isinstance(message, dict) else None, start=True)
        elif kind == "message_delta":
            self._usage(event.get("usage"), start=False)
        elif kind == "content_block_start":
            block = event.get("content_block")
            self._block(block.get("type") if isinstance(block, dict) else None)
        elif kind == "content_block_delta":
            delta = event.get("delta")
            if not isinstance(delta, dict) or delta.get("type") not in DELTA_TYPES:
                self._bad("content delta not understood")
        elif kind == "message_stop":
            self.stopped = True
        elif kind == "error":
            self._bad("error event")

    def _usage(self, usage: object, *, start: bool) -> None:
        if not isinstance(usage, dict):
            self._bad("usage missing")
            return
        unknown = sorted(set(usage) - USAGE_KEYS)
        if unknown:
            self._bad(f"usage field not understood: {unknown[0]}")
            return
        required = ("input_tokens", "output_tokens") if start else ("output_tokens",)
        if not all(_count(usage.get(key)) for key in required):
            self._bad("usage counts missing or invalid")
            return
        counts = ("input_tokens", "output_tokens", "cache_creation_input_tokens",
                  "cache_read_input_tokens")  # fmt: skip
        if not all(usage.get(k) is None or _count(usage.get(k)) for k in counts):
            self._bad("usage counts invalid")
            return
        if usage.get("service_tier") not in (None, "standard"):
            self._bad("service tier not standard")
        tools = usage.get("server_tool_use")
        if tools is not None and (
            not isinstance(tools, dict) or any(v != 0 for v in tools.values())
        ):
            self._bad("server tool use")

        def number(key: str) -> int:
            value = usage.get(key)
            return value if isinstance(value, int) else 0

        self.input_tokens = max(self.input_tokens, number("input_tokens"))
        self.cache_read = max(self.cache_read, number("cache_read_input_tokens"))
        self.output_tokens = max(self.output_tokens, number("output_tokens"))  # cumulative
        writes = number("cache_creation_input_tokens")
        creation = usage.get("cache_creation")
        if creation is not None:
            if (
                not isinstance(creation, dict)
                or set(creation) - CACHE_CREATION_KEYS
                or not all(_count(v) for v in creation.values())
            ):
                self._bad("cache_creation invalid")
                return
            five = creation.get("ephemeral_5m_input_tokens", 0)
            hour = creation.get("ephemeral_1h_input_tokens", 0)
            if usage.get("cache_creation_input_tokens") is not None and writes != five + hour:
                self._bad("cache writes do not add up")
            self.cache_write_5m = max(self.cache_write_5m, five)
            self.cache_write_1h = max(self.cache_write_1h, hour)
        elif writes:
            # No breakdown: charge every write at the dearer, 1-hour rate.
            self.cache_write_1h = max(self.cache_write_1h, writes)
        if start:
            self.started = True
        else:
            self.delta = True


def charge(meter: UsageMeter, price: ModelPrice, reservation: int) -> Settled:
    """What the request cost: its reported usage in full (above its reservation
    too), or its whole reservation when the report is not complete and valid."""
    if meter.invalid:  # the first problem seen, from the body or from outside it
        return Settled(reservation, reservation, meter.invalid)
    if not meter.stopped:
        return Settled(reservation, reservation, "incomplete")
    if not meter.valid:
        return Settled(reservation, reservation, meter.invalid or "usage incomplete")
    tokens = (meter.input_tokens, meter.cache_write_5m, meter.cache_write_1h, meter.cache_read)
    total_input = sum(tokens)
    rates = price.input_rates(total_input)
    cost = sum(_cost(t, r) for t, r in zip(tokens, rates, strict=True))
    cost += _cost(meter.output_tokens, price.output_rate(total_input))
    return Settled(cost, reservation, REPORTED)


def _count(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _cost(tokens: int, rate: int) -> int:
    return -(-tokens * rate // MILLION)  # ceiling, in micro-dollars


def policy_from_json(text: str) -> AdmissionPolicy:
    """The policy the coordinator passes to the sidecar, as JSON."""
    data = json.loads(text)
    prices = {name: ModelPrice(**rates) for name, rates in data.pop("prices").items()}
    if "allowed_betas" in data:
        data["allowed_betas"] = frozenset(data["allowed_betas"])
    return AdmissionPolicy(prices=prices, **data)
