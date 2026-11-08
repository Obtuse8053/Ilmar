# SPDX-License-Identifier: Apache-2.0
"""Process death for recovery tests (#11c, ADR-0022).

``Crash`` is a ``BaseException`` so that, like a killed process, no ``except``
in Ilmar handles it. Once a ``Switch`` has fired, every later store write and
external effect raises too: nothing after the crash point happens.
"""

from __future__ import annotations

import sqlite3
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ilmar.adapters.sqlite import SQLiteExecutionStore
from ilmar.domain.event_history import verify_lines
from ilmar.domain.events import EventContext
from ilmar.domain.ledger import Commit, Fence


class Crash(BaseException):
    """The coordinator process died here."""


@dataclass
class Switch:
    """Decides where the process dies: at a store write or before an external effect."""

    write_at: int | None = None
    effect_at: int | None = None
    before: str | None = None
    """Die before the first call to this effect, e.g. ``"scm.publish_revision"``."""
    writes: int = 0
    effects: int = 0
    dead: bool = False

    def on_write(self) -> None:
        self.writes += 1
        if self.dead or (self.write_at is not None and self.writes >= self.write_at):
            self.dead = True
            raise Crash(f"died at write {self.writes}")

    def on_effect(self, what: str) -> None:
        if self.dead:
            raise Crash("already dead")
        self.effects += 1
        if what == self.before or (self.effect_at is not None and self.effects >= self.effect_at):
            self.dead = True
            raise Crash(f"died before effect {self.effects} ({what})")


class KillableStore:
    """A real SQLite store whose writes go through the switch."""

    def __init__(self, inner: SQLiteExecutionStore, switch: Switch) -> None:
        self.inner, self.switch = inner, switch

    def create(self, *args: Any, **kwargs: Any) -> None:
        self.switch.on_write()
        self.inner.create(*args, **kwargs)

    def apply(self, *args: Any, **kwargs: Any) -> None:
        self.switch.on_write()
        self.inner.apply(*args, **kwargs)

    def commit(self, fence: Fence, changes: Commit, *, context: EventContext | None = None) -> None:
        self.switch.on_write()
        self.inner.commit(fence, changes, context=context)

    def append_evidence(self, *args: Any, **kwargs: Any) -> None:
        self.switch.on_write()
        self.inner.append_evidence(*args, **kwargs)

    def __getattr__(self, name: str) -> Any:
        return getattr(self.inner, name)


class Killable:
    """A port whose mutating calls go through the switch first."""

    def __init__(self, target: Any, mutating: set[str], switch: Switch, label: str) -> None:
        self._target, self._mutating, self._switch, self._label = target, mutating, switch, label

    def __getattr__(self, name: str) -> Any:
        attribute = getattr(self._target, name)
        if name not in self._mutating:
            return attribute

        def guarded(*args: Any, **kwargs: Any) -> Any:
            self._switch.on_effect(f"{self._label}.{name}")
            return attribute(*args, **kwargs)

        return guarded


# -- the event log (ADR-0024) ------------------------------------------------------------

PREVIOUS_SCHEMA = 3
"""The schema before the events migration."""


def event_problems(db: Path) -> list[str]:
    """Invariant I11 and the flags that must never appear, read without writing.

    ``unclassified`` is not checked here yet: the coordinator supplies reason
    codes in #17's core wiring (part 3 of 4).
    """
    with SQLiteExecutionStore.open_read_only(db) as store:
        lines, stored = store.history_snapshot()
    if not lines:
        return []  # no coverage yet: nothing was ever claimed
    report = verify_lines(lines, stored)
    problems = [f"I11 {f.check} {f.subject}: {f.detail}" for f in report.failures]
    for line in lines:
        flags = {flag for flag in ("redacted", "invalid", "truncated") if f'"{flag}":true' in line}
        if flags:
            problems.append(f"an event was flagged {sorted(flags)}: {line[:120]}")
    return problems


def to_previous_schema(db: Path) -> None:
    """The store exactly as schema 3 would have left it: every row, no events.

    The events migration only adds the ``events`` table (its index and triggers
    go with it), so removing it and the version leaves the previous layout.
    """
    with closing(sqlite3.connect(db)) as conn:
        conn.execute("DROP TABLE events")
        conn.execute("DELETE FROM sqlite_sequence WHERE name = 'events'")
        conn.execute(f"PRAGMA user_version = {PREVIOUS_SCHEMA}")
        conn.commit()
