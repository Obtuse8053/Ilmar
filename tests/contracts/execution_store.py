# SPDX-License-Identifier: Apache-2.0
"""Contract every ``ExecutionStore`` adapter must satisfy.

Subclass :class:`ExecutionStoreContract` in a ``test_*.py`` module and
implement :meth:`make_store`.
"""

from __future__ import annotations

import threading
from collections.abc import Iterator
from datetime import timedelta, timezone
from pathlib import Path

import pytest

from ilmar.core.state_machine import ExecutionStateMachine
from ilmar.domain.errors import (
    ConcurrencyConflictError,
    DuplicateExecutionError,
    ExecutionNotFoundError,
)
from ilmar.domain.execution import Execution, ExecutionState
from ilmar.domain.tasks import TaskRef
from ilmar.ports.execution_store import ExecutionStore

from helpers import at

S = ExecutionState
MACHINE = ExecutionStateMachine(max_attempts=3)
TASK_A = TaskRef("fake", "A")
TASK_B = TaskRef("fake", "B")


def _create(store: ExecutionStore, execution_id: str, task: TaskRef, seconds: float) -> Execution:
    execution, transition = MACHINE.start(task, execution_id=execution_id, now=at(seconds))
    store.create(execution, transition)
    return execution


def _advance(
    store: ExecutionStore,
    execution: Execution,
    target: ExecutionState,
    seconds: float,
    **kwargs: str,
) -> Execution:
    reason = kwargs.pop("reason", f"to {target.value}")
    updated, transition = MACHINE.advance(
        execution, target, now=at(seconds), reason=reason, **kwargs
    )
    store.apply(updated, transition)
    return updated


class ExecutionStoreContract:
    def make_store(self, tmp_path: Path) -> Iterator[ExecutionStore]:
        raise NotImplementedError

    @pytest.fixture
    def store(self, tmp_path: Path) -> Iterator[ExecutionStore]:
        yield from self.make_store(tmp_path)

    # -- create / get ----------------------------------------------------

    def test_created_execution_round_trips(self, store: ExecutionStore) -> None:
        execution = _create(store, "e1", TASK_A, 0)
        assert store.get("e1") == execution

    def test_get_unknown_returns_none(self, store: ExecutionStore) -> None:
        assert store.get("missing") is None

    def test_creation_is_recorded_in_history(self, store: ExecutionStore) -> None:
        _create(store, "e1", TASK_A, 0)
        (entry,) = store.history("e1")
        assert entry.from_state is None
        assert entry.to_state is S.QUEUED
        assert entry.at == at(0)

    def test_history_of_unknown_execution_raises(self, store: ExecutionStore) -> None:
        with pytest.raises(ExecutionNotFoundError):
            store.history("missing")

    def test_duplicate_id_is_rejected(self, store: ExecutionStore) -> None:
        _create(store, "e1", TASK_A, 0)
        with pytest.raises(DuplicateExecutionError):
            _create(store, "e1", TASK_B, 1)

    def test_second_active_execution_for_task_is_rejected(self, store: ExecutionStore) -> None:
        _create(store, "e1", TASK_A, 0)
        with pytest.raises(DuplicateExecutionError):
            _create(store, "e2", TASK_A, 1)
        assert store.get("e2") is None

    def test_new_execution_allowed_after_previous_one_ends(self, store: ExecutionStore) -> None:
        first = _create(store, "e1", TASK_A, 0)
        _advance(store, first, S.CANCELLED, 1)
        second = _create(store, "e2", TASK_A, 2)
        assert store.active_for_task(TASK_A) == second

    def test_inconsistent_create_is_rejected(self, store: ExecutionStore) -> None:
        _, transition_for_e1 = MACHINE.start(TASK_A, execution_id="e1", now=at(0))
        e2, _ = MACHINE.start(TASK_A, execution_id="e2", now=at(0))
        with pytest.raises(ValueError, match="different execution"):
            store.create(e2, transition_for_e1)
        assert store.get("e1") is None
        assert store.get("e2") is None

    # -- active_for_task / list_in_states --------------------------------

    def test_active_for_task(self, store: ExecutionStore) -> None:
        assert store.active_for_task(TASK_A) is None
        execution = _create(store, "e1", TASK_A, 0)
        assert store.active_for_task(TASK_A) == execution
        assert store.active_for_task(TASK_B) is None
        _advance(store, execution, S.FAILED, 1, error="boom")
        assert store.active_for_task(TASK_A) is None

    def test_list_in_states_filters_and_orders_oldest_first(self, store: ExecutionStore) -> None:
        late = _create(store, "z-late", TASK_A, 10)
        early = _create(store, "a-early", TASK_B, 5)
        same_time = _create(store, "b-same", TaskRef("fake", "C"), 5)
        done = _create(store, "done", TaskRef("fake", "D"), 0)
        _advance(store, done, S.CANCELLED, 20)

        queued = store.list_in_states({S.QUEUED})
        assert [e.id for e in queued] == [early.id, same_time.id, late.id]
        assert [e.id for e in store.list_in_states({S.CANCELLED})] == ["done"]
        assert len(store.list_in_states({S.QUEUED, S.CANCELLED})) == 4
        assert list(store.list_in_states(())) == []

    # -- apply -----------------------------------------------------------

    def test_apply_updates_record_and_appends_history(self, store: ExecutionStore) -> None:
        execution = _create(store, "e1", TASK_A, 0)
        execution = _advance(store, execution, S.CLAIMED, 1)
        execution = _advance(store, execution, S.PROVISIONING, 2, branch="ilmar/a")
        assert store.get("e1") == execution
        assert execution.branch == "ilmar/a"
        assert [t.to_state for t in store.history("e1")] == [S.QUEUED, S.CLAIMED, S.PROVISIONING]
        assert [t.from_state for t in store.history("e1")] == [None, S.QUEUED, S.CLAIMED]

    def test_optional_fields_round_trip(self, store: ExecutionStore) -> None:
        execution = _create(store, "e1", TASK_A, 0)
        execution = _advance(store, execution, S.CLAIMED, 1)
        execution = _advance(store, execution, S.PROVISIONING, 2, branch="b")
        execution = _advance(
            store,
            execution,
            S.RETRY_WAIT,
            3,
            error="workspace failed: ünïcödé",
            change_request_url="https://example.invalid/cr/1",
        )
        stored = store.get("e1")
        assert stored == execution
        assert stored is not None
        assert stored.last_error == "workspace failed: ünïcödé"

    def test_timestamps_preserve_the_instant(self, store: ExecutionStore) -> None:
        offset = timezone(timedelta(hours=5, minutes=30))
        when = at(0.123456).astimezone(offset)
        execution, transition = MACHINE.start(TASK_A, execution_id="e1", now=when)
        store.create(execution, transition)
        stored = store.get("e1")
        assert stored is not None
        assert stored.created_at == when
        assert stored.created_at.utcoffset() is not None

    def test_stale_update_is_rejected_and_nothing_changes(self, store: ExecutionStore) -> None:
        base = _create(store, "e1", TASK_A, 0)
        winner = _advance(store, base, S.CLAIMED, 1)
        loser, transition = MACHINE.advance(base, S.CANCELLED, now=at(2), reason="race")
        with pytest.raises(ConcurrencyConflictError):
            store.apply(loser, transition)
        assert store.get("e1") == winner
        assert len(store.history("e1")) == 2

    def test_apply_to_unknown_execution_raises(self, store: ExecutionStore) -> None:
        execution, _ = MACHINE.start(TASK_A, execution_id="ghost", now=at(0))
        updated, transition = MACHINE.advance(execution, S.CLAIMED, now=at(1), reason="x")
        with pytest.raises(ExecutionNotFoundError):
            store.apply(updated, transition)

    def test_concurrent_creates_for_one_task_admit_exactly_one(self, store: ExecutionStore) -> None:
        results: list[str] = []
        barrier = threading.Barrier(8)

        def attempt(n: int) -> None:
            execution, transition = MACHINE.start(TASK_A, execution_id=f"e{n}", now=at(n))
            barrier.wait()
            try:
                store.create(execution, transition)
            except DuplicateExecutionError:
                results.append("duplicate")
            else:
                results.append("created")

        threads = [threading.Thread(target=attempt, args=(n,)) for n in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        assert sorted(results) == ["created"] + ["duplicate"] * 7
