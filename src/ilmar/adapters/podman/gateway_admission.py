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
  ``message_stop`` arrived;
* a report that costs more than the reservation (the estimate was wrong) is
  charged in full, recorded as an *overrun*, and **halts the attempt**: the
  gateway forwards nothing more for it.

**The guarantee that follows.** The attempt's spend is at most ``B`` plus the
overruns of the requests in flight when the first overrun was seen; with
``max_in_flight`` 1, at most one request's overrun. The size of an overrun has
no bound this gateway can prove. That is why the provider's own workspace
spend cap stays the hard backstop (ADR-0029 §5), and why an overrun is an
operator-visible event that also reaches the coordinator's ledger as an extra
charge (P5 part 2).

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
import threading
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

MILLION = 1_000_000

TOP_LEVEL_FIELDS = frozenset(
    {
        "model", "messages", "system", "max_tokens", "metadata", "stop_sequences",
        "stream", "temperature", "top_k", "top_p", "tools", "tool_choice", "thinking",
        "service_tier",
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
        billed = sorted(b for b in self.allowed_betas if _billed_beta(b))
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


@dataclass(frozen=True, slots=True)
class Settled:
    charged: int
    reservation: int
    reason: str
    """``reported``, or why the whole reservation was kept (``incomplete``, a usage problem)."""

    @property
    def overrun(self) -> int:
        return max(0, self.charged - self.reservation)


@dataclass
class Budget:
    """One attempt's spending, shared by the gateway's request threads."""

    limit: int
    policy: AdmissionPolicy
    settled: int = 0
    overruns: int = 0
    halted: bool = False
    in_flight: dict[int, int] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    _next: int = 0

    def __post_init__(self) -> None:
        if self.limit < 1:
            raise ValueError("the attempt's budget must be positive")

    def admit(self, body: bytes, betas: Iterable[str] = ()) -> tuple[int, Admitted]:
        """Check, constrain and reserve; ``(ticket, admitted)``, or ``RequestRefused``."""
        _check_betas(betas, self.policy)
        request = parse(body)
        model, price, input_tokens, requested = assess(request, len(body), self.policy)
        input_cost = _cost(input_tokens, max(price.input_rates(input_tokens)))
        output_rate = price.output_rate(input_tokens)
        wanted = min(requested, self.policy.max_output_tokens)
        with self._lock:
            if self.halted:
                raise RequestRefused(
                    "budget_exceeded",
                    "this attempt is stopped: a request cost more than its reservation",
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
        return ticket, Admitted(body, reservation, model, granted, input_tokens, capped)

    def settle(self, ticket: int, settled: Settled) -> Settled:
        """Close a request at what it cost, in full. An overrun halts the attempt."""
        with self._lock:
            self.in_flight.pop(ticket)
            self.settled += settled.charged
            if settled.overrun:
                self.overruns += settled.overrun
                self.halted = True
        return settled

    def release(self, ticket: int) -> None:
        """Drop the reservation of a request that was never sent."""
        with self._lock:
            self.in_flight.pop(ticket)

    def spent(self) -> int:
        with self._lock:
            return self.settled + sum(self.in_flight.values())


# -- checking a request ---------------------------------------------------------------


def _billed_beta(beta: str) -> bool:
    return any(beta.startswith(family) for family in BILLED_BETA_FAMILIES)


def _check_betas(betas: Iterable[str], policy: AdmissionPolicy) -> None:
    for value in betas:
        for beta in (b.strip() for b in value.split(",")):
            if beta and (_billed_beta(beta) or beta not in policy.allowed_betas):
                raise RequestRefused("invalid_request_error", f"beta feature not allowed: {beta}")


def parse(body: bytes) -> dict[str, Any]:
    try:
        request = json.loads(body)
    except (ValueError, UnicodeDecodeError) as error:
        raise RequestRefused("invalid_request_error", "the body is not JSON") from error
    if not isinstance(request, dict):
        raise RequestRefused("invalid_request_error", "the body is not a JSON object")
    return request


def assess(
    request: Mapping[str, Any], body_bytes: int, policy: AdmissionPolicy
) -> tuple[str, ModelPrice, int, int]:
    """Validate; return the model, its price, the input estimate and the requested max."""
    unknown = sorted(set(request) - TOP_LEVEL_FIELDS)
    if unknown:
        raise RequestRefused("invalid_request_error", f"field not allowed here: {unknown[0]}")
    model = request.get("model")
    price = policy.prices.get(model) if isinstance(model, str) else None
    if price is None or not isinstance(model, str):
        raise RequestRefused("permission_error", "model not allowed by this gateway", 403)
    requested = request.get("max_tokens")
    if not isinstance(requested, int) or not _count(requested) or requested < 1:
        raise RequestRefused("invalid_request_error", "max_tokens must be a positive integer")
    if request.get("service_tier", "standard_only") != "standard_only":
        raise RequestRefused("invalid_request_error", "only the standard service tier")
    images, units = _content(request)
    if images and policy.image_tokens is None:
        raise RequestRefused("invalid_request_error", "images are not allowed by this gateway")
    tools = request.get("tools")
    if tools is not None:
        _check_tools(tools)
    _check_thinking_shape(request.get("thinking"))
    input_tokens = (
        body_bytes
        + units * policy.message_overhead_tokens
        + images * (policy.image_tokens or 0)
        + (policy.tool_overhead_tokens if tools else 0)
    )
    if price.long_context_threshold is None and input_tokens > 200_000:
        raise RequestRefused("request_too_large", "too large for this model's attested prices", 413)
    return model, price, input_tokens, requested


def _check_tools(tools: object) -> None:
    if not isinstance(tools, list):
        raise RequestRefused("invalid_request_error", "tools must be a list")
    for tool in tools:
        if not isinstance(tool, dict) or tool.get("type", "custom") != "custom":
            raise RequestRefused(
                "invalid_request_error", "only custom tools; server tools are refused"
            )


def _check_thinking_shape(thinking: object) -> None:
    if thinking is None:
        return
    if not isinstance(thinking, dict) or thinking.get("type") not in THINKING_TYPES:
        raise RequestRefused("invalid_request_error", "thinking type not allowed")
    budget = thinking.get("budget_tokens")
    if budget is not None and not _count(budget):
        raise RequestRefused("invalid_request_error", "thinking budget must be an integer")


def _check_thinking(request: Mapping[str, Any], max_tokens: int) -> None:
    thinking = request.get("thinking")
    budget = thinking.get("budget_tokens") if isinstance(thinking, dict) else None
    if isinstance(budget, int) and budget >= max_tokens:
        raise RequestRefused(
            "invalid_request_error",
            f"thinking budget must be below max_tokens ({max_tokens} after the gateway's limit)",
        )


def _content(request: Mapping[str, Any]) -> tuple[int, int]:
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
        raise RequestRefused("invalid_request_error", "system must be text or blocks")
    messages = request.get("messages")
    if not isinstance(messages, list):
        raise RequestRefused("invalid_request_error", "messages must be a list")
    for message in messages:
        if not isinstance(message, dict):
            raise RequestRefused("invalid_request_error", "a message must be an object")
        units += 1
        content = message.get("content")
        if isinstance(content, list):
            images += _blocks(content)
            units += len(content)
        elif not isinstance(content, str):
            raise RequestRefused("invalid_request_error", "content must be text or blocks")
    return images, units


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
    not a non-negative integer, an unknown usage field, server-tool use, a tier
    other than ``standard``, cache writes that do not add up, an ``error``
    event, or an unreadable event or body.
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
            if line.startswith(b"data:"):
                self._event(line[5:].strip())

    def finish(self) -> None:
        if self.streamed:
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
        self.delta = self.started  # a plain message reports its final usage at once
        self.stopped = message.get("stop_reason") is not None

    def _bad(self, why: str) -> None:
        self.invalid = self.invalid or why

    def _event(self, data: bytes) -> None:
        try:
            event = json.loads(data)
        except ValueError:
            self._bad("event unreadable")
            return
        if not isinstance(event, dict):
            self._bad("event unreadable")
            return
        kind = event.get("type")
        if kind == "message_start":
            message = event.get("message")
            self._usage(message.get("usage") if isinstance(message, dict) else None, start=True)
        elif kind == "message_delta":
            self._usage(event.get("usage"), start=False)
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
    if not meter.stopped:
        return Settled(reservation, reservation, "incomplete")
    if not meter.valid:
        return Settled(reservation, reservation, meter.invalid or "usage incomplete")
    tokens = (meter.input_tokens, meter.cache_write_5m, meter.cache_write_1h, meter.cache_read)
    total_input = sum(tokens)
    rates = price.input_rates(total_input)
    cost = sum(_cost(t, r) for t, r in zip(tokens, rates, strict=True))
    cost += _cost(meter.output_tokens, price.output_rate(total_input))
    return Settled(cost, reservation, "reported")


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
