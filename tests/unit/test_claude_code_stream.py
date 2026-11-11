# SPDX-License-Identifier: Apache-2.0
"""The ``stream-json`` validator (ADR-0035 §2, §4, §5).

Every line is validated as it arrives, wherever it is in the stream; state is
bounded; the first violation is final and is reported by kind and line
number only.
"""

from __future__ import annotations

import json
import random
import re
from collections.abc import Sequence
from pathlib import Path

import pytest

from ilmar.adapters.claude_code.stream import (
    MAX_EVENT_BYTES,
    MAX_TRAILING_TELEMETRY,
    VERIFIED_TELEMETRY,
    Rejection,
    StreamValidator,
    Violation,
)
from ilmar.domain.workspace import StreamVerdict

from claude_code_support import (
    API_RETRY,
    ASSISTANT,
    INIT,
    RATE_LIMIT,
    SUCCESS_RESULT,
    TOOL_RESULT,
)

V = StreamVerdict
LONG_MIDDLE = [ASSISTANT, TOOL_RESULT] * 2000  # about 400 KB of ordinary events


def lines(*events: str) -> bytes:
    return "".join(f"{event}\n" for event in events).encode()


def run(data: bytes, *, chunk: int = 65536) -> StreamValidator:
    validator = StreamValidator()
    for start in range(0, len(data), chunk):
        if validator.feed(data[start : start + chunk]) is V.VIOLATION:
            return validator
    validator.close()
    return validator


def rejection(data: bytes, **kwargs: int) -> Rejection | None:
    return run(data, **kwargs).rejection


# -- a valid stream ------------------------------------------------------------------------


def test_a_valid_stream_keeps_only_the_result() -> None:
    validator = run(lines(INIT, *LONG_MIDDLE, SUCCESS_RESULT))
    assert validator.rejection is None
    assert validator.result is not None
    assert validator.result["subtype"] == "success"
    assert validator.events == len(LONG_MIDDLE) + 2
    assert validator.held == 0


def test_progress_comes_from_agent_activity_and_the_result_only() -> None:
    validator = StreamValidator()
    assert validator.feed(lines(INIT)) is V.PROGRESS
    assert validator.feed(lines(ASSISTANT, RATE_LIMIT)) is V.PROGRESS
    assert validator.feed(lines(RATE_LIMIT)) is V.NONE  # telemetry is no progress
    assert validator.feed(lines(API_RETRY)) is V.NONE
    assert validator.feed(b'{"type": "assist') is V.NONE  # no complete line yet
    assert validator.feed(b'ant"}\n') is V.PROGRESS
    assert validator.feed(lines(SUCCESS_RESULT)) is V.PROGRESS
    assert validator.close() is V.NONE
    assert validator.rejection is None


# -- every violation, in the middle of a long stream, before a valid result ----------------


@pytest.mark.parametrize(
    ("bad", "kind"),
    [
        ("not json at all", Violation.MALFORMED_EVENT),
        ("", Violation.MALFORMED_EVENT),  # empty line
        ("[1, 2]", Violation.MALFORMED_EVENT),
        ('"just a string"', Violation.MALFORMED_EVENT),
        ('{"no": "type"}', Violation.MALFORMED_EVENT),
        ('{"type": 7}', Violation.MALFORMED_EVENT),
        ('{"type": "telemetry_event"}', Violation.MALFORMED_EVENT),  # undocumented
        ('{"type": "rate_limit"}', Violation.MALFORMED_EVENT),  # near a verified name
        ('{"type": "system", "subtype": 3}', Violation.MALFORMED_EVENT),
        ('{"type": "assistant", "x": NaN}', Violation.MALFORMED_EVENT),
        ('{"type": "assistant", "x": Infinity}', Violation.MALFORMED_EVENT),
        ('{"type": "assistant"', Violation.MALFORMED_EVENT),  # truncated object
        ("[" * 100000 + "]" * 100000, Violation.MALFORMED_EVENT),  # deep nesting
    ],
)
def test_a_malformed_middle_fails_the_run_despite_a_valid_result(bad: str, kind: Violation) -> None:
    middle = len(LONG_MIDDLE) // 2
    events = [INIT, *LONG_MIDDLE[:middle], bad, *LONG_MIDDLE[middle:], SUCCESS_RESULT]
    validator = run(lines(*events))
    assert validator.rejection == Rejection(kind, middle + 2)
    assert validator.result is None
    assert str(validator.rejection) == f"malformed stream: {kind.value} at line {middle + 2}"


def test_invalid_utf8_is_malformed() -> None:
    data = lines(INIT) + b'{"type": "assistant", "x": "\xff"}\n' + lines(SUCCESS_RESULT)
    assert rejection(data) == Rejection(Violation.MALFORMED_EVENT, 2)


def test_an_oversized_line_in_the_middle_is_never_buffered() -> None:
    """A 50 MiB line without a newline: rejected while it is read, and the
    validator never holds more than ``MAX_EVENT_BYTES`` of it."""
    validator = StreamValidator()
    assert validator.feed(lines(INIT)) is V.PROGRESS
    block = b"x" * 65536
    verdicts = []
    for _ in range(50 * 16):  # 50 MiB in 64 KiB chunks
        verdicts.append(validator.feed(block))
        assert validator.held <= MAX_EVENT_BYTES
    validator.feed(b"\n" + lines(SUCCESS_RESULT))
    validator.close()
    assert V.VIOLATION in verdicts
    assert validator.rejection == Rejection(Violation.OVERSIZED_LINE, 2)
    assert validator.held_peak <= MAX_EVENT_BYTES
    assert validator.held == 0
    assert validator.result is None


def test_a_line_of_exactly_the_bound_is_accepted() -> None:
    padding = MAX_EVENT_BYTES - len('{"type": "assistant", "p": ""}')
    line = '{"type": "assistant", "p": "' + "a" * padding + '"}'
    assert len(line.encode()) == MAX_EVENT_BYTES
    assert rejection(lines(INIT, line, SUCCESS_RESULT)) is None
    longer = line[:-2] + 'a"}'
    assert rejection(lines(INIT, longer, SUCCESS_RESULT)) == Rejection(Violation.OVERSIZED_LINE, 2)


def test_a_second_result_is_rejected() -> None:
    data = lines(INIT, SUCCESS_RESULT, ASSISTANT, SUCCESS_RESULT)
    assert rejection(data) == Rejection(Violation.DATA_AFTER_RESULT, 3)
    data = lines(INIT, SUCCESS_RESULT, SUCCESS_RESULT)
    assert rejection(data) == Rejection(Violation.DUPLICATE_RESULT, 3)


@pytest.mark.parametrize("after", [ASSISTANT, TOOL_RESULT, INIT, API_RETRY])
def test_agent_activity_or_unverified_telemetry_after_the_result_is_rejected(after: str) -> None:
    assert rejection(lines(INIT, SUCCESS_RESULT, after)) == Rejection(
        Violation.DATA_AFTER_RESULT, 3
    )


def test_an_unterminated_last_line_is_rejected() -> None:
    data = lines(INIT) + SUCCESS_RESULT.encode()  # no newline after the result
    assert rejection(data) == Rejection(Violation.UNTERMINATED_LINE, 2)


def test_a_stream_without_a_result_is_rejected() -> None:
    assert rejection(lines(INIT, ASSISTANT)) == Rejection(Violation.MISSING_RESULT, 2)
    assert rejection(b"") == Rejection(Violation.MISSING_RESULT, 0)


def test_the_first_violation_is_final() -> None:
    validator = StreamValidator()
    assert validator.feed(lines(INIT, "garbage")) is V.VIOLATION
    assert validator.feed(lines(SUCCESS_RESULT)) is V.NONE  # drained, never looked at
    assert validator.close() is V.NONE
    assert validator.rejection == Rejection(Violation.MALFORMED_EVENT, 2)
    assert validator.result is None


def test_a_violation_never_holds_or_quotes_the_line() -> None:
    secret = "sk-ant-should-never-appear-0123456789"
    validator = run(lines(INIT, f"oops {secret}", SUCCESS_RESULT))
    assert validator.rejection is not None
    assert secret not in str(validator.rejection)
    assert secret not in repr(validator.rejection)
    assert validator.held == 0


# -- telemetry (ADR-0035 §2) ---------------------------------------------------------------


def test_rate_limit_events_are_accepted_before_between_and_after_the_result() -> None:
    data = lines(RATE_LIMIT, INIT, RATE_LIMIT, ASSISTANT, API_RETRY, SUCCESS_RESULT, RATE_LIMIT)
    validator = run(data)
    assert validator.rejection is None
    assert validator.result is not None
    assert validator.telemetry_after_result == 1


def test_trailing_telemetry_is_bounded() -> None:
    within = lines(INIT, SUCCESS_RESULT, *[RATE_LIMIT] * MAX_TRAILING_TELEMETRY)
    assert rejection(within) is None
    beyond = lines(INIT, SUCCESS_RESULT, *[RATE_LIMIT] * (MAX_TRAILING_TELEMETRY + 1))
    assert rejection(beyond) == Rejection(Violation.DATA_AFTER_RESULT, MAX_TRAILING_TELEMETRY + 3)


def test_the_verified_list_is_closed_and_recorded() -> None:
    """Each entry says where it was observed; only rate_limit_event may follow
    the result. Changing this list needs a recorded sample, the live test and
    maintainer review (ADR-0035 §2)."""
    entries = {(e.type, e.subtype): e for e in VERIFIED_TELEMETRY}
    assert set(entries) == {("rate_limit_event", None), ("system", "api_retry")}
    assert entries["rate_limit_event", None].after_result
    assert not entries["system", "api_retry"].after_result
    assert all(e.observed_on and e.basis for e in VERIFIED_TELEMETRY)


# -- chunking ----------------------------------------------------------------------------


def outcome(validator: StreamValidator) -> tuple[Rejection | None, object]:
    result = validator.result
    return validator.rejection, None if result is None else json.dumps(result, sort_keys=True)


STREAMS: Sequence[bytes] = (
    lines(INIT, ASSISTANT, SUCCESS_RESULT, RATE_LIMIT),
    lines(INIT, '{"type": "assistant", "t": "naïve ☃ 漢字"}', "oops", SUCCESS_RESULT),
    lines(INIT, SUCCESS_RESULT, SUCCESS_RESULT),
    lines(INIT, SUCCESS_RESULT, ASSISTANT),
    lines(INIT, ASSISTANT) + b'{"type": "result"',
    lines(RATE_LIMIT, INIT, '{"type": "user", "u": "\\u00e9"}', SUCCESS_RESULT),
)


@pytest.mark.parametrize("data", STREAMS)
def test_every_split_gives_the_same_verdict(data: bytes) -> None:
    """All two-way splits, one-byte chunks and seeded random splits, across
    UTF-8 sequences and JSON tokens."""
    expected = outcome(run(data))
    assert outcome(run(data, chunk=1)) == expected
    for cut in range(len(data) + 1):
        validator = StreamValidator()
        if (
            validator.feed(data[:cut]) is not V.VIOLATION
            and validator.feed(data[cut:]) is not V.VIOLATION
        ):
            validator.close()
        assert outcome(validator) == expected, cut
    rng = random.Random(len(data))  # noqa: S311 - seeded test splits, not security
    for _ in range(50):
        validator = StreamValidator()
        position = 0
        while position < len(data):
            step = rng.randint(1, 37)
            if validator.feed(data[position : position + step]) is V.VIOLATION:
                break
            position += step
        else:
            validator.close()
        assert outcome(validator) == expected


# -- usage errors -------------------------------------------------------------------------


def test_feeding_after_close_is_a_usage_error() -> None:
    validator = run(lines(INIT, SUCCESS_RESULT))
    with pytest.raises(ValueError, match="closed"):
        validator.feed(b"x")
    with pytest.raises(ValueError, match="closed"):
        validator.close()


# -- event-type observer and pinned-version fixtures (ADR-0035 §6) -----------------------

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures" / "claude_code_streams"


def test_the_observer_sees_types_and_order_but_never_content() -> None:
    seen: list[tuple[str, str | None, bool]] = []
    secret = "sk-ant-very-secret-content"
    data = lines(
        INIT,
        json.dumps({"type": "assistant", "message": secret}),
        SUCCESS_RESULT,
        RATE_LIMIT,
    )
    validator = StreamValidator(observer=lambda *event: seen.append(event))
    validator.feed(data)
    assert validator.close() is not V.VIOLATION
    assert seen == [
        ("system", "init", False),
        ("assistant", None, False),
        ("result", "success", False),
        ("rate_limit_event", None, True),
    ]
    assert secret not in repr(seen)


def test_the_observer_sees_the_event_that_is_then_rejected() -> None:
    """The live test can say which type the pinned version emitted that we reject."""
    seen: list[tuple[str, str | None, bool]] = []
    validator = StreamValidator(observer=lambda *event: seen.append(event))
    validator.feed(lines(INIT, '{"type": "brand_new"}'))
    assert validator.rejection == Rejection(Violation.MALFORMED_EVENT, 2)
    assert seen[-1] == ("brand_new", None, False)


def fixture_stream(events: list[dict[str, object]]) -> bytes:
    """Rebuild a minimal stream from a recorded type sequence."""
    rebuilt = []
    for event in events:
        body: dict[str, object] = {"type": event["type"]}
        if event["subtype"] is not None:
            body["subtype"] = event["subtype"]
        rebuilt.append(json.dumps(body))
    return lines(*rebuilt)


@pytest.mark.parametrize("path", sorted(FIXTURES.glob("*.json")), ids=lambda p: p.stem)
def test_recorded_type_sequences_are_accepted(path: Path) -> None:
    """Each fixture is a pinned version's event-type sequence. A recorded one
    comes from the live test; a synthetic one must say so."""
    fixture = json.loads(path.read_text())
    assert fixture["provenance"] in {"recorded", "synthetic"}
    synthetic = fixture["provenance"] == "synthetic"
    # A synthetic fixture says so in its name and its note, and is never
    # mistaken for evidence; a recorded one names the exact pinned version.
    assert synthetic == path.stem.endswith("-synthetic"), path.name
    if synthetic:
        assert "NOT recorded" in fixture["note"]
    else:
        assert re.fullmatch(r"\d+\.\d+\.\d+", fixture["claude_code_version"])
    seen: list[tuple[str, str | None, bool]] = []
    validator = StreamValidator(observer=lambda *event: seen.append(event))
    validator.feed(fixture_stream(fixture["events"]))
    assert validator.close() is not V.VIOLATION, validator.rejection
    assert validator.result is not None
    assert seen == [(e["type"], e["subtype"], e["after_result"]) for e in fixture["events"]]


def test_there_is_a_fixture_for_rate_limit_after_the_result() -> None:
    fixtures = [json.loads(p.read_text()) for p in FIXTURES.glob("*.json")]
    assert any(
        e["type"] == "rate_limit_event" and e["after_result"] for f in fixtures for e in f["events"]
    )
