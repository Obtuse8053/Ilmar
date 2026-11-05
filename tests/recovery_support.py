# SPDX-License-Identifier: Apache-2.0
"""Process death for recovery tests (#11c, ADR-0022).

``Crash`` is a ``BaseException`` so that, like a killed process, no ``except``
in Ilmar handles it. Once a ``Switch`` has fired, every later store write and
external effect raises too: nothing after the crash point happens.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ilmar.adapters.sqlite import SQLiteExecutionStore
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

    def commit(self, fence: Fence, changes: Commit) -> None:
        self.switch.on_write()
        self.inner.commit(fence, changes)

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
