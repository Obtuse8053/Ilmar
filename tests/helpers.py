# SPDX-License-Identifier: Apache-2.0
"""Small builders shared by tests."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from ilmar.domain.tasks import Task, TaskRef

T0 = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)


def at(seconds: float) -> datetime:
    """A fixed point in time, ``seconds`` after ``T0``."""
    return T0 + timedelta(seconds=seconds)


def task(key: str, *, provider: str = "fake", title: str | None = None) -> Task:
    return Task(
        ref=TaskRef(provider=provider, key=key),
        title=title or f"Task {key}",
        description=f"Description of task {key}",
    )
