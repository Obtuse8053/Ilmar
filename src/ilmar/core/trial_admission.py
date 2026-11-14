# SPDX-License-Identifier: Apache-2.0
"""Attach the trial's cap to every admission (ADR-0041 §5).

``ilmar run --loop --trial`` gives the coordinator this store instead of the
real one. Every commit that admits work goes through ``commit``, from every
path: a new claim, an attempt started after a retry wait, a recovered
execution. This wrapper adds the trial's :class:`~ilmar.domain.gates.TrialCap`
to such a commit, and the real store checks it in the same transaction
(:func:`ilmar.ports.execution_store.check_trial`). Everything else passes
through unchanged.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from ilmar.domain.events import EventContext
from ilmar.domain.execution import ExecutionState
from ilmar.domain.gates import TrialCap
from ilmar.domain.ledger import Commit, Fence

_ADMITTING = frozenset({ExecutionState.CLAIMED, ExecutionState.PROVISIONING})


def admits_work(changes: Commit) -> bool:
    """A commit that creates a live execution, claims one or starts an attempt."""
    return any(not e.state.is_terminal for e, _ in changes.created) or any(
        t.to_state in _ADMITTING for _, t in changes.applied
    )


class TrialAdmissionStore:
    """An execution store that bounds every admission by one trial."""

    def __init__(self, store: Any, cap: TrialCap) -> None:
        self._store = store
        self.cap = cap

    def commit(self, fence: Fence, changes: Commit, *, context: EventContext | None = None) -> None:
        if admits_work(changes):
            changes = replace(changes, trial=self.cap)
        self._store.commit(fence, changes, context=context)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._store, name)

    def __enter__(self) -> TrialAdmissionStore:
        return self

    def __exit__(self, *exc: object) -> None:
        """The real store's lifecycle belongs to its owner (the components)."""
