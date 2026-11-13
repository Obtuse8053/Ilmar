# SPDX-License-Identifier: Apache-2.0
"""The watcher's two checks are independent, and a failed suspension check
stops the agent (ADR-0029 §4; #122 review, 2026-11-13).

* A failing cancellation-inbox read never skips the kill-switch and
  suspension check, whatever it raises.
* A suspension check that raises stops the agent: whether it may go on is
  unknown, so the watcher fails closed.
* A valid human request still wins when both are present.
"""

from __future__ import annotations

import sqlite3
import threading
from collections.abc import Callable

import pytest

from ilmar.core.cancel_inbox import CancelRequest, Reading
from ilmar.core.cancel_watcher import SUSPENSION_CHECK_FAILED, Watcher

INTERVAL = 0.01
REQUEST = CancelRequest("r1", "e1", "human", "2026-11-13T00:00:00Z", "d", 1, 2)


def failing(error: BaseException) -> Callable[[str], Reading]:
    def read(execution_id: str) -> Reading:
        raise error

    return read


def after(calls: int, value: str | None) -> Callable[[], str | None]:
    """A suspension check that reports ``value`` from its ``calls``-th call."""
    seen = [0]

    def check() -> str | None:
        seen[0] += 1
        return value if seen[0] >= calls else None

    return check


def stopped(watcher: Watcher) -> bool:
    with watcher:
        return watcher.signal.wait(2.0)


@pytest.mark.parametrize(
    "error",
    [
        OSError("unreadable"),
        ValueError("bad"),
        RuntimeError("unexpected"),
        sqlite3.OperationalError("locked"),
        KeyError("x"),
    ],
)
def test_a_failing_inbox_read_never_skips_the_suspension_check(error: BaseException) -> None:
    watcher = Watcher(failing(error), "e1", interval=INTERVAL, suspended=after(3, "kill-switch"))
    assert stopped(watcher)
    assert watcher.suspended_by == "kill-switch"
    assert watcher.request is None
    assert watcher.read_error is error


def test_a_failing_inbox_read_alone_never_stops_the_agent() -> None:
    watcher = Watcher(failing(OSError("unreadable")), "e1", interval=INTERVAL)
    with watcher:
        assert not watcher.signal.wait(0.2)  # many ticks, all failing
    assert watcher.suspended_by is None


@pytest.mark.parametrize(
    "error",
    [
        OSError("io"),
        ValueError("bad row"),
        RuntimeError("unexpected"),
        sqlite3.DatabaseError("corrupt"),
    ],
)
def test_a_failing_suspension_check_stops_the_agent(error: BaseException) -> None:
    def check() -> str | None:
        raise error

    watcher = Watcher(lambda _: Reading(), "e1", interval=INTERVAL, suspended=check)
    assert stopped(watcher)
    assert watcher.suspended_by == SUSPENSION_CHECK_FAILED
    assert watcher.check_error is error
    assert watcher.request is None


def test_the_check_still_runs_after_many_failed_reads() -> None:
    watcher = Watcher(failing(RuntimeError("x")), "e1", interval=INTERVAL,
                      suspended=after(20, "operator-suspended"))  # fmt: skip
    assert stopped(watcher)
    assert watcher.suspended_by == "operator-suspended"


def test_a_valid_request_wins_over_a_suspension() -> None:
    watcher = Watcher(lambda _: Reading(request=REQUEST), "e1", interval=INTERVAL,
                      suspended=lambda: "kill-switch")  # fmt: skip
    assert stopped(watcher)
    assert watcher.request is REQUEST
    assert watcher.suspended_by is None


def test_the_watcher_thread_survives_every_failure() -> None:
    """The thread ends only by stopping the agent or by ``__exit__``."""
    calls = [0]

    def check() -> str | None:
        calls[0] += 1
        return None

    watcher = Watcher(failing(RuntimeError("x")), "e1", interval=INTERVAL, suspended=check)
    with watcher:
        threading.Event().wait(0.2)
        assert watcher._thread.is_alive()
    assert calls[0] > 3
