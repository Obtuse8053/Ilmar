# SPDX-License-Identifier: Apache-2.0
"""Worst-case spending control for the model gateway (ADR-0037 §2, #111 P5).

Like ``model_gateway.py``, this module runs inside the gateway sidecar, so it
uses only the standard library and never imports ``ilmar``.

**The guarantee.** One sandbox attempt has a budget ``B`` in micro-dollars, its
ledger reservation. The gateway forwards a request only if

    settled + in flight + worst case(request) <= B

and settles each finished request at no more than the worst case it reserved.
So the attempt's provider spend is at most ``B``, whatever the agent sends.

**A request's worst case** (``worst_case``):

* *Input:* every token of input is bounded by the request body's size in bytes
  (a token is at least one byte of the JSON text that carries it), plus a fixed
  allowance per image block and one per request for the provider's hidden
  tool prompt. That bound is charged at the highest input-side rate of the
  model: input, 5-minute cache write, 1-hour cache write or cache read. When
  the bound passes the model's long-context threshold, every input-side rate
  is scaled by the long-context input rate over the standard one, as in
  settlement; a price table without long-context rates refuses such a request.
* *Output:* ``max_tokens`` at the output rate (at the long-context output rate
  where that applies). Thinking is billed as output and counts against
  ``max_tokens``, so it is inside this term.

**What is refused** (never forwarded): a model without an attested price; any
top-level field outside the allow-list; server tools (any tool with a ``type``
other than ``custom``); documents, URL images, search results and every other
content type outside the allow-list; a ``service_tier`` other than
``standard_only``; ``mcp_servers`` and ``container``; a thinking budget that is
not below ``max_tokens``; and a body that is not one JSON object.

**What is constrained.** ``max_tokens`` is first lowered to the operator's
``max_output_tokens``. If the request still does not fit the budget, it is
lowered to the largest value that does, but never below ``min_output_tokens``.
Below that it is refused (``RequestRefused``, kind ``budget_exceeded``). Every
lowering is reported in ``Admitted.capped_from``, for the gateway to log.

**Settlement** (``settle``) charges the usage the provider reported, at the
rates of the request's model, and never more than the request's reservation. A
response without a complete usage report (a stream cut off before
``message_stop``, an unreadable body, an error after billing began) keeps its
full reservation.

Prices are integers: micro-dollars per million tokens. Costs are integers in
micro-dollars, always rounded up.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Mapping
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
        if not all(isinstance(r, int) and r > 0 for r in rates):
            raise ValueError("every rate is a positive integer")
        long = (self.long_context_threshold, self.long_context_input, self.long_context_output)
        if any(v is not None for v in long) and not all(isinstance(v, int) and v > 0 for v in long):
            raise ValueError("long-context pricing needs a threshold and both rates")

    @property
    def top_input(self) -> int:
        return max(self.input, self.cache_write_5m, self.cache_write_1h, self.cache_read)

    def long_context(self, input_tokens: int) -> bool:
        threshold = self.long_context_threshold
        return threshold is not None and input_tokens > threshold


@dataclass(frozen=True, slots=True)
class AdmissionPolicy:
    prices: Mapping[str, ModelPrice]
    max_output_tokens: int = 32_000
    min_output_tokens: int = 1_024
    image_tokens: int = 2_000
    """An allowance per image block, above what its base64 bytes already count."""
    tool_overhead_tokens: int = 1_000
    """The provider's hidden system prompt for tool use, per request."""

    def __post_init__(self) -> None:
        if not self.prices:
            raise ValueError("at least one priced model is needed")
        if not 1 <= self.min_output_tokens <= self.max_output_tokens:
            raise ValueError("need 1 <= min_output_tokens <= max_output_tokens")
        if self.image_tokens < 0 or self.tool_overhead_tokens < 0:
            raise ValueError("allowances must not be negative")


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
    capped_from: int | None = None
    """The ``max_tokens`` the client asked for, when it was lowered."""
    max_tokens: int = 0


@dataclass
class Budget:
    """One attempt's spending, shared by the gateway's request threads."""

    limit: int
    policy: AdmissionPolicy
    settled: int = 0
    in_flight: dict[int, int] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    _next: int = 0

    def __post_init__(self) -> None:
        if self.limit < 1:
            raise ValueError("the attempt's budget must be positive")

    def admit(self, body: bytes) -> tuple[int, Admitted]:
        """Check, constrain and reserve; ``(ticket, admitted)``, or ``RequestRefused``."""
        request = parse(body)
        model, _price, input_cost, output_rate, requested = assess(request, len(body), self.policy)
        wanted = min(requested, self.policy.max_output_tokens)
        with self._lock:
            room = self.limit - self.settled - sum(self.in_flight.values()) - input_cost
            affordable = room * MILLION // output_rate if room > 0 else 0
            granted = min(wanted, affordable)
            if granted < self.policy.min_output_tokens:
                raise RequestRefused(
                    "budget_exceeded",
                    "this attempt's model budget cannot cover the request's worst case",
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
            if len(rewritten) > len(body):  # the input bound was taken from the original
                self.settle(ticket, 0)
                raise RequestRefused("invalid_request_error", "the request cannot be re-encoded")
            body = rewritten
        capped = requested if granted != requested else None
        return ticket, Admitted(body, reservation, model, capped, granted)

    def settle(self, ticket: int, charge: int) -> int:
        """Close a request at ``charge``, never above its reservation. Returns the charge."""
        with self._lock:
            reserved = self.in_flight.pop(ticket)
            charged = min(max(charge, 0), reserved)
            self.settled += charged
            return charged

    def spent(self) -> int:
        with self._lock:
            return self.settled + sum(self.in_flight.values())


# -- checking a request ---------------------------------------------------------------


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
) -> tuple[str, ModelPrice, int, int, int]:
    """Validate; return model, price, worst input cost, output rate and requested max."""
    unknown = sorted(set(request) - TOP_LEVEL_FIELDS)
    if unknown:
        raise RequestRefused("invalid_request_error", f"field not allowed here: {unknown[0]}")
    model = request.get("model")
    price = policy.prices.get(model) if isinstance(model, str) else None
    if price is None or not isinstance(model, str):
        raise RequestRefused("permission_error", "model not allowed by this gateway", 403)
    requested = request.get("max_tokens")
    if not isinstance(requested, int) or isinstance(requested, bool) or requested < 1:
        raise RequestRefused("invalid_request_error", "max_tokens must be a positive integer")
    if request.get("service_tier", "standard_only") != "standard_only":
        raise RequestRefused("invalid_request_error", "only the standard service tier")
    images = _images(request)
    tools = request.get("tools")
    if tools is not None:
        _check_tools(tools)
    _check_thinking_shape(request.get("thinking"))
    input_tokens = (
        body_bytes + images * policy.image_tokens + (policy.tool_overhead_tokens if tools else 0)
    )
    if price.long_context(input_tokens):
        assert price.long_context_input is not None  # noqa: S101 - checked in ModelPrice
        assert price.long_context_output is not None  # noqa: S101
        # Settlement scales every input-side rate by long-context input / input;
        # the reservation must use the dearest of them, scaled the same way.
        in_rate = -(-price.top_input * price.long_context_input // price.input)
        out_rate = max(price.output, price.long_context_output)
    elif price.long_context_threshold is None and _over_any_threshold(input_tokens):
        raise RequestRefused("request_too_large", "too large for this model's attested prices", 413)
    else:
        in_rate, out_rate = price.top_input, price.output
    return model, price, _cost(input_tokens, in_rate), out_rate, requested


def _over_any_threshold(input_tokens: int) -> bool:
    """Without long-context prices, nothing above the standard window is priced."""
    return input_tokens > 200_000


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
    if budget is not None and (not isinstance(budget, int) or isinstance(budget, bool)):
        raise RequestRefused("invalid_request_error", "thinking budget must be an integer")


def _check_thinking(request: Mapping[str, Any], max_tokens: int) -> None:
    thinking = request.get("thinking")
    budget = thinking.get("budget_tokens") if isinstance(thinking, dict) else None
    if isinstance(budget, int) and budget >= max_tokens:
        raise RequestRefused(
            "invalid_request_error",
            f"thinking budget must be below max_tokens ({max_tokens} after the gateway's limit)",
        )


def _images(request: Mapping[str, Any]) -> int:
    """Count image blocks; refuse any content type outside the allow-list."""
    count = 0
    system = request.get("system")
    if isinstance(system, list):
        count += _blocks(system)
    elif system is not None and not isinstance(system, str):
        raise RequestRefused("invalid_request_error", "system must be text or blocks")
    messages = request.get("messages")
    if not isinstance(messages, list):
        raise RequestRefused("invalid_request_error", "messages must be a list")
    for message in messages:
        if not isinstance(message, dict):
            raise RequestRefused("invalid_request_error", "a message must be an object")
        content = message.get("content")
        if isinstance(content, list):
            count += _blocks(content)
        elif not isinstance(content, str):
            raise RequestRefused("invalid_request_error", "content must be text or blocks")
    return count


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
    """Reads the usage a response reports, streamed or not. ``complete`` only
    once the provider has said the message ended."""

    streamed: bool
    input_tokens: int = 0
    cache_write_5m: int = 0
    cache_write_1h: int = 0
    cache_read: int = 0
    output_tokens: int = 0
    server_tool_use: bool = False
    complete: bool = False
    _buffer: bytes = b""
    _plain: bytearray = field(default_factory=bytearray)

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
            return
        if isinstance(message, dict) and message.get("type") == "message":
            self._usage(message.get("usage"))
            self.complete = message.get("stop_reason") is not None

    def _event(self, data: bytes) -> None:
        try:
            event = json.loads(data)
        except ValueError:
            return
        if not isinstance(event, dict):
            return
        kind = event.get("type")
        if kind == "message_start" and isinstance(event.get("message"), dict):
            self._usage(event["message"].get("usage"))
        elif kind == "message_delta":
            self._usage(event.get("usage"))
        elif kind == "message_stop":
            self.complete = True

    def _usage(self, usage: object) -> None:
        if not isinstance(usage, dict):
            return

        def number(key: str) -> int | None:
            value = usage.get(key)
            return value if isinstance(value, int) and not isinstance(value, bool) else None

        if (value := number("input_tokens")) is not None:
            self.input_tokens = max(self.input_tokens, value)
        if (value := number("cache_read_input_tokens")) is not None:
            self.cache_read = max(self.cache_read, value)
        if (value := number("output_tokens")) is not None:
            self.output_tokens = max(self.output_tokens, value)  # cumulative in deltas
        creation = usage.get("cache_creation")
        if isinstance(creation, dict):
            five = creation.get("ephemeral_5m_input_tokens")
            hour = creation.get("ephemeral_1h_input_tokens")
            self.cache_write_5m = max(self.cache_write_5m, five if isinstance(five, int) else 0)
            self.cache_write_1h = max(self.cache_write_1h, hour if isinstance(hour, int) else 0)
        elif (value := number("cache_creation_input_tokens")) is not None:
            # No breakdown: charge every write at the dearer, 1-hour rate.
            self.cache_write_1h = max(self.cache_write_1h, value)
        tools = usage.get("server_tool_use")
        if isinstance(tools, dict) and any(isinstance(v, int) and v > 0 for v in tools.values()):
            self.server_tool_use = True


def charge(meter: UsageMeter, price: ModelPrice, reservation: int) -> int:
    """The cost of what the provider reported, or the full reservation when the
    report is incomplete or shows something that was never allowed."""
    if not meter.complete or meter.server_tool_use:
        return reservation
    tokens = (meter.input_tokens, meter.cache_write_5m, meter.cache_write_1h, meter.cache_read)
    total_input = sum(tokens)
    rates: tuple[int, ...] = (
        price.input,
        price.cache_write_5m,
        price.cache_write_1h,
        price.cache_read,
    )
    output_rate = price.output
    if price.long_context(total_input):
        assert price.long_context_input is not None  # noqa: S101 - checked in ModelPrice
        assert price.long_context_output is not None  # noqa: S101
        # Every input-side rate scales with the long-context input rate, rounded up.
        rates = tuple(-(-r * price.long_context_input // price.input) for r in rates)
        output_rate = price.long_context_output
    cost = sum(_cost(t, r) for t, r in zip(tokens, rates, strict=True))
    cost += _cost(meter.output_tokens, output_rate)
    return min(cost, reservation)


def _cost(tokens: int, rate: int) -> int:
    return -(-tokens * rate // MILLION)  # ceiling, in micro-dollars


def policy_from_json(text: str) -> AdmissionPolicy:
    """The policy the coordinator passes to the sidecar, as JSON."""
    data = json.loads(text)
    prices = {name: ModelPrice(**rates) for name, rates in data.pop("prices").items()}
    return AdmissionPolicy(prices=prices, **data)
