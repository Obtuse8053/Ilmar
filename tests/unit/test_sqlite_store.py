# SPDX-License-Identifier: Apache-2.0
"""SQLite-specific behaviour beyond the shared ExecutionStore contract."""

from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

from ilmar.adapters.sqlite import SCHEMA_V1, SCHEMA_VERSION, SQLiteExecutionStore
from ilmar.core.state_machine import ExecutionStateMachine
from ilmar.domain.errors import DuplicateExecutionError, EvidenceIntegrityError
from ilmar.domain.execution import ExecutionState
from ilmar.domain.tasks import TaskRef

from contracts.execution_store import evidence_record
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


# -- schema v2 (#9) --------------------------------------------------------------------


def make_v1(path: Path) -> None:
    """A database as schema version 1 left it, with one execution and history."""
    with closing(sqlite3.connect(path)) as conn:
        conn.executescript(SCHEMA_V1)
        conn.execute(
            "INSERT INTO executions (id, task_provider, task_key, state, is_terminal, attempt, "
            "version, created_at, updated_at, branch, change_request_url, last_error) "
            "VALUES ('old', 'fake', '1', 'claimed', 0, 1, 2, "
            "'2026-11-01T00:00:00.000000+00:00', '2026-11-01T00:00:01.000000+00:00', "
            "NULL, NULL, NULL)"
        )
        conn.execute(
            "INSERT INTO transitions (execution_id, from_state, to_state, at, reason) "
            "VALUES ('old', NULL, 'queued', '2026-11-01T00:00:00.000000+00:00', 'created')"
        )
        conn.execute("PRAGMA user_version = 1")
        conn.commit()


def test_version_1_databases_are_migrated_in_place(tmp_path: Path) -> None:
    path = tmp_path / "executions.sqlite3"
    make_v1(path)
    with SQLiteExecutionStore(path) as store:
        old = store.get("old")
        assert old is not None
        assert old.state is ExecutionState.CLAIMED
        assert (old.base_revision, old.candidate_revision) == (None, None)
        assert len(store.history("old")) == 1
        store.append_evidence(evidence_record("old"))
        assert len(store.evidence("old")) == 1
    with closing(sqlite3.connect(path)) as conn:
        assert conn.execute("PRAGMA user_version").fetchone() == (SCHEMA_VERSION,)


def test_a_failed_migration_leaves_the_database_unchanged(tmp_path: Path) -> None:
    path = tmp_path / "executions.sqlite3"
    make_v1(path)
    with closing(sqlite3.connect(path)) as conn:
        conn.execute("CREATE TABLE evidence (x INTEGER)")  # makes the migration fail
        conn.commit()
    with pytest.raises(sqlite3.OperationalError):
        SQLiteExecutionStore(path)
    with closing(sqlite3.connect(path)) as conn:
        assert conn.execute("PRAGMA user_version").fetchone() == (1,)
        columns = {row[1] for row in conn.execute("PRAGMA table_info(executions)")}
        assert "candidate_revision" not in columns


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE evidence SET record = '{}'",
        "DELETE FROM evidence",
        "UPDATE transitions SET reason = 'rewritten'",
        "DELETE FROM transitions",
    ],
)
def test_history_and_evidence_are_append_only_in_the_database(
    tmp_path: Path, statement: str
) -> None:
    path = tmp_path / "executions.sqlite3"
    with SQLiteExecutionStore(path) as store:
        execution, transition = MACHINE.start(TaskRef("fake", "1"), execution_id="e1", now=at(0))
        store.create(execution, transition)
        store.append_evidence(evidence_record("e1"))
    with closing(sqlite3.connect(path)) as conn, pytest.raises(sqlite3.IntegrityError):
        conn.execute(statement)


def test_evidence_altered_behind_ilmars_back_is_detected(tmp_path: Path) -> None:
    path = tmp_path / "executions.sqlite3"
    with SQLiteExecutionStore(path) as store:
        execution, transition = MACHINE.start(TaskRef("fake", "1"), execution_id="e1", now=at(0))
        store.create(execution, transition)
        store.append_evidence(evidence_record("e1"))
    with closing(sqlite3.connect(path)) as conn:
        conn.execute("DROP TRIGGER evidence_is_append_only_update")
        conn.execute(
            "UPDATE evidence SET record = replace(record, 'rejected-checks-failed', 'publish')"
        )
        conn.commit()
    with SQLiteExecutionStore(path) as store, pytest.raises(EvidenceIntegrityError):
        store.evidence("e1")
