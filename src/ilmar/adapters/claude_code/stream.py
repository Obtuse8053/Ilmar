# SPDX-License-Identifier: Apache-2.0
"""Incremental validation of Claude Code's ``stream-json`` output (ADR-0035).

Every line of the agent command's standard output passes through
:class:`StreamValidator`, chunk by chunk as it is read, whatever its position
in the stream. The validator's state is bounded, whatever the stream's length:

* the current incomplete line, at most :data:`MAX_EVENT_BYTES`. Bytes past
  that bound are counted and discarded, never buffered;
* the parsed result object, whose line was at most that size;
* a few counters;
* the first violation, as a kind and a line number, never the line itself.

Events fall into three closed classes (ADR-0035 §2):

* **agent activity** (``assistant``, ``user``, and ``system`` except the
  telemetry subtypes), which counts as progress;
* the single **result**, which also counts as progress;
* **telemetry** from :data:`VERIFIED_TELEMETRY`, which is accepted but never
  restarts the idle timer.

Everything else is a violation. The first violation is final: the caller ends
the command at once and the run fails without a retry.
"""

from __future__ import annotations

import enum
import json
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, NoReturn

from ilmar.domain.workspace import StreamVerdict

MAX_EVENT_BYTES = 1024 * 1024
"""The longest acceptable line (one event), newline excluded."""

MAX_TRAILING_TELEMETRY = 32
"""The most telemetry lines accepted after the result object."""

ACTIVITY_TYPES = frozenset({"assistant", "user", "system"})
RESULT_TYPE = "result"


@dataclass(frozen=True, slots=True)
class TelemetryEntry:
    """One verified telemetry event (ADR-0035 §2).

    ``subtype`` ``None`` matches the type whatever its subtype. ``observed_on``
    is the Claude Code version the entry was observed or documented on, and
    ``basis`` says how. Adding an entry, or allowing one after the result,
    needs a recorded sample from the pinned version, the live test and
    maintainer review.
    """

    type: str
    subtype: str | None
    after_result: bool
    observed_on: str
    basis: str


VERIFIED_TELEMETRY: tuple[TelemetryEntry, ...] = (
    TelemetryEntry(
        "rate_limit_event",
        None,
        after_result=True,
        observed_on="2.1.302",
        basis="upstream report anthropics/claude-code#103253 [R28]; not yet "
        "verified on the pinned version",
    ),
    TelemetryEntry(
        "system",
        "api_retry",
        after_result=False,
        observed_on="documented",
        basis="Claude Code headless documentation [R15]",
    ),
)


EventObserver = Callable[[str, str | None, bool], None]
"""Called with ``(type, subtype, after_result)`` for each well-formed event."""


class Violation(enum.Enum):
    """Why a stream was rejected (ADR-0035 §4)."""

    MALFORMED_EVENT = "malformed-event"
    OVERSIZED_LINE = "oversized-line"
    DUPLICATE_RESULT = "duplicate-result"
    DATA_AFTER_RESULT = "data-after-result"
    UNTERMINATED_LINE = "unterminated-line"
    MISSING_RESULT = "missing-result"


@dataclass(frozen=True, slots=True)
class Rejection:
    kind: Violation
    line: int
    """1-based number of the offending line (for ``missing-result``: lines seen)."""

    def __str__(self) -> str:
        return f"malformed stream: {self.kind.value} at line {self.line}"


class _Rejected(Exception):  # noqa: N818 - internal control flow, never escapes
    def __init__(self, kind: Violation) -> None:
        super().__init__(kind.value)
        self.kind = kind


def _reject(kind: Violation) -> NoReturn:
    raise _Rejected(kind)


def _no_constants(name: str) -> NoReturn:
    """JSON has no NaN or Infinity; Python's parser would accept them."""
    _reject(Violation.MALFORMED_EVENT)


class StreamValidator:
    """A :class:`~ilmar.ports.container_runtime.StreamConsumer` for ``stream-json``."""

    def __init__(
        self,
        *,
        telemetry: tuple[TelemetryEntry, ...] = VERIFIED_TELEMETRY,
        max_event_bytes: int = MAX_EVENT_BYTES,
        max_trailing_telemetry: int = MAX_TRAILING_TELEMETRY,
        observer: EventObserver | None = None,
    ) -> None:
        if max_event_bytes < 1 or max_trailing_telemetry < 0:
            raise ValueError("bounds must be positive")
        self._telemetry = telemetry
        self._max = max_event_bytes
        self._max_trailing = max_trailing_telemetry
        self._observer = observer
        self._partial = bytearray()
        self._overflow = False  # the current line passed the bound: its bytes are dropped
        self._closed = False
        self.result: dict[str, Any] | None = None
        self.rejection: Rejection | None = None
        self.lines = 0
        self.events = 0
        self.telemetry_after_result = 0
        self.bytes = 0
        self.held_peak = 0
        """The most bytes the validator ever held for one line (for the bound's tests)."""

    @property
    def held(self) -> int:
        """Bytes of stream data the validator holds right now."""
        return len(self._partial)

    def feed(self, chunk: bytes) -> StreamVerdict:
        if self._closed:
            raise ValueError("the stream is closed")
        self.bytes += len(chunk)
        if self.rejection is not None:
            return StreamVerdict.NONE  # final: the rest is drained, never looked at
        progress = False
        start = 0
        try:
            while start < len(chunk):
                end = chunk.find(b"\n", start)
                if end == -1:
                    self._append(chunk[start:])
                    break
                self._append(chunk[start:end])
                progress |= self._complete_line()
                start = end + 1
        except _Rejected as rejected:
            self._fail(rejected.kind, self.lines)
            return StreamVerdict.VIOLATION
        return StreamVerdict.PROGRESS if progress else StreamVerdict.NONE

    def close(self) -> StreamVerdict:
        if self._closed:
            raise ValueError("the stream is already closed")
        self._closed = True
        if self.rejection is not None:
            return StreamVerdict.NONE
        if self._partial or self._overflow:
            self._fail(Violation.UNTERMINATED_LINE, self.lines + 1)
            return StreamVerdict.VIOLATION
        if self.result is None:
            self._fail(Violation.MISSING_RESULT, self.lines)
            return StreamVerdict.VIOLATION
        return StreamVerdict.NONE

    # -- internals ----------------------------------------------------------------

    def _append(self, piece: bytes) -> None:
        if self._overflow:
            return
        if len(self._partial) + len(piece) > self._max:
            # Detected while the line is read: never buffer past the bound.
            self._overflow = True
            self._partial.clear()
            self.lines += 1
            _reject(Violation.OVERSIZED_LINE)
        self._partial += piece
        self.held_peak = max(self.held_peak, len(self._partial))

    def _complete_line(self) -> bool:
        """Validate the finished line; return whether it is progress."""
        self.lines += 1
        line = bytes(self._partial)
        self._partial.clear()
        event = self._parse(line)
        kind, subtype = event["type"], event.get("subtype")
        entry = self._telemetry_entry(kind, subtype)
        self._observe(kind, subtype)
        if entry is not None:
            if self.result is not None:
                if not entry.after_result:
                    _reject(Violation.DATA_AFTER_RESULT)
                self.telemetry_after_result += 1
                if self.telemetry_after_result > self._max_trailing:
                    _reject(Violation.DATA_AFTER_RESULT)
            self.events += 1
            return False
        if kind == RESULT_TYPE:
            if self.result is not None:
                _reject(Violation.DUPLICATE_RESULT)
            self.result = event
            self.events += 1
            return True
        if kind in ACTIVITY_TYPES:
            if self.result is not None:
                _reject(Violation.DATA_AFTER_RESULT)
            self.events += 1
            return True
        _reject(Violation.MALFORMED_EVENT)

    def _parse(self, line: bytes) -> dict[str, Any]:
        try:
            text = line.decode("utf-8")
            data = json.loads(text, parse_constant=_no_constants)
        except (UnicodeDecodeError, ValueError, RecursionError):
            _reject(Violation.MALFORMED_EVENT)
        if not isinstance(data, dict) or not isinstance(data.get("type"), str):
            _reject(Violation.MALFORMED_EVENT)
        subtype = data.get("subtype")
        if subtype is not None and not isinstance(subtype, str):
            _reject(Violation.MALFORMED_EVENT)
        return data

    def _observe(self, kind: str, subtype: object) -> None:
        """Only the type, the subtype and whether the result was already seen:
        never any content (the live test records these for the pinned version)."""
        if self._observer is not None:
            self._observer(
                kind, subtype if isinstance(subtype, str) else None, self.result is not None
            )

    def _telemetry_entry(self, kind: str, subtype: object) -> TelemetryEntry | None:
        for entry in self._telemetry:
            if entry.type == kind and (entry.subtype is None or entry.subtype == subtype):
                return entry
        return None

    def _fail(self, kind: Violation, line: int) -> None:
        self.rejection = Rejection(kind, line)
        self._partial = bytearray()  # nothing of the stream is kept after a violation
        self.result = None
