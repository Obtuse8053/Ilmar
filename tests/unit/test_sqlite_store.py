# SPDX-License-Identifier: Apache-2.0
"""SQLite-specific behaviour beyond the shared ExecutionStore contract."""

from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

from ilmar.adapters.sqlite import SCHEMA_VERSION, SQLiteExecutionStore
from ilmar.core.state_machine import ExecutionStateMachine
from ilmar.domain.errors import DuplicateExecutionError
from ilmar.domain.execution import ExecutionState
from ilmar.domain.tasks import TaskRef

from helpers import at

MACHINE = ExecutionStateMachine(max_attempts=3)


def test_state_survives_reopening(tmp_path: Path) -> None:
    path = tmp_path / "executions.sqlite3"
    with SQLiteExecutionStore(path) as store:
        execution, transition = MACHINE.start(TaskRef("fake", "1"), execution_id="e1", now=at(0))
        store.create(execution, transition)
        execution, transition = MACHINE.advance(
            execution, ExecutionState.CLAIMED, now=at(1), reason="claimed"
        )
        store.apply(execution, transition)

    with SQLiteExecutionStore(path) as reopened:
        assert reopened.get("e1") == execution
        assert len(reopened.history("e1")) == 2
        active = reopened.list_in_states({ExecutionState.CLAIMED})
        assert [e.id for e in active] == ["e1"]


def test_two_connections_cannot_both_hold_a_task(tmp_path: Path) -> None:
    path = tmp_path / "executions.sqlite3"
    task = TaskRef("fake", "1")
    with SQLiteExecutionStore(path) as first, SQLiteExecutionStore(path) as second:
        execution, transition = MACHINE.start(task, execution_id="e1", now=at(0))
        first.create(execution, transition)
        other, other_transition = MACHINE.start(task, execution_id="e2", now=at(1))
        with pytest.raises(DuplicateExecutionError):
            second.create(other, other_transition)


def test_schema_version_is_recorded(tmp_path: Path) -> None:
    path = tmp_path / "executions.sqlite3"
    SQLiteExecutionStore(path).close()
    with closing(sqlite3.connect(path)) as conn:
        assert conn.execute("PRAGMA user_version").fetchone() == (SCHEMA_VERSION,)


def test_newer_schema_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "executions.sqlite3"
    conn = sqlite3.connect(path)
    conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION + 1}")
    conn.close()
    with pytest.raises(RuntimeError, match="newer"):
        SQLiteExecutionStore(path)
