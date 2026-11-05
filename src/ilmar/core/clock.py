# SPDX-License-Identifier: Apache-2.0
"""Restart-safe timestamps for persisted state (ADR-0022).

Two kinds of time, never mixed:

* **Deadlines of running work** (agent and command timeouts, proxy start-up)
  are measured by adapters with ``time.monotonic`` and never persisted.
* **Timestamps of persisted state** (transitions, journal, ledger, evidence)
  come from the wall clock, which can step backwards (NTP, a VM restored
  from a snapshot, a wrong clock fixed while Ilmar is down).

``NonDecreasingClock`` makes the timestamps one process hands out
non-decreasing. Across a restart, the worker also clamps each transition to
the execution's last persisted update (:func:`not_before`). A backward step
therefore never makes a write invalid; timestamps stand still until the wall
clock catches up.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime


class NonDecreasingClock:
    """The wall clock, never earlier than any time it returned before."""

    def __init__(self, wall: Callable[[], datetime]) -> None:
        self._wall = wall
        self._last: datetime | None = None

    def __call__(self) -> datetime:
        now = self._wall()
        if self._last is not None and now < self._last:
            now = self._last
        self._last = now
        return now


def not_before(now: datetime, floor: datetime) -> datetime:
    """``now``, or ``floor`` if the wall clock is behind a persisted timestamp."""
    return floor if now < floor else now
