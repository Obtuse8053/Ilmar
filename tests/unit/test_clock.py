# SPDX-License-Identifier: Apache-2.0
"""Restart-safe timestamps (ADR-0022)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from ilmar.core.clock import NonDecreasingClock, not_before

T0 = datetime(2026, 11, 5, 12, tzinfo=UTC)


def test_a_wall_clock_step_back_never_makes_time_run_backwards() -> None:
    readings = iter([T0, T0 + timedelta(seconds=5), T0 - timedelta(days=1), T0 + timedelta(1)])
    clock = NonDecreasingClock(lambda: next(readings))
    assert [clock() for _ in range(4)] == [
        T0,
        T0 + timedelta(seconds=5),
        T0 + timedelta(seconds=5),  # stands still until the wall clock catches up
        T0 + timedelta(days=1),
    ]


def test_persisted_time_is_a_floor_across_restarts() -> None:
    stored = T0 + timedelta(hours=1)
    assert not_before(T0, stored) == stored
    assert not_before(stored + timedelta(seconds=1), stored) == stored + timedelta(seconds=1)
