# SPDX-License-Identifier: Apache-2.0
"""The SQLite event log beyond the shared contract (ADR-0024 §8 and §9).

* the migration from the previous schema (version 3), under the instance lock,
  with its baseline, in one transaction;
* read-only opens, which never migrate and never write;
* crashes inside the migration, as exceptions and as a killed process;
* ``verify``'s classes, each failure made by editing the database outside Ilmar.

The pre-migration store is built through the public API and then has its
events table removed. :func:`test_the_fixture_has_exactly_the_previous_schema`
shows that this leaves exactly the layout schema version 3 creates.
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import textwrap
from collections.abc import Callable
from contextlib import closing
from pathlib import Path

import pytest

from event_store_support import MACHINE, PATHS, digest, dump, populate
from ilmar.adapters import sqlite as sqlite_adapter
from ilmar.adapters.sqlite import MIGRATIONS, SCHEMA_V1, SCHEMA_VERSION, SQLiteExecutionStore
from ilmar.domain.errors import FencingError, LedgerIntegrityError
from ilmar.domain.event_history import FindingClass, Report, verify_lines
from ilmar.domain.events import CorruptEventError, EventType
from ilmar.domain.execution import ExecutionState
from ilmar.domain.ledger import Commit, OperationStatus, ResourceStatus
from ilmar.domain.tasks import TaskRef
from ilmar.ports import event_log
from recovery_support import PREVIOUS_SCHEMA, to_previous_schema, to_schema_4

from contracts.execution_store import evidence_record
from helpers import at

S, ST, R = ExecutionState, OperationStatus, ResourceStatus


@pytest.fixture
def old_store(tmp_path: Path) -> Path:
    path = tmp_path / "executions.sqlite3"
    with SQLiteExecutionStore(path) as store:
        populate(store)
    to_previous_schema(path)
    return path


def schema(path: Path) -> list[tuple[str, str, str]]:
    with closing(sqlite3.connect(path)) as conn:
        rows = conn.execute("SELECT type, name, sql FROM sqlite_master WHERE sql IS NOT NULL")
        return sorted(rows.fetchall())


def version(path: Path) -> int:
    with closing(sqlite3.connect(path)) as conn:
        (value,) = conn.execute("PRAGMA user_version").fetchone()
    return int(value)


def report(path: Path) -> Report:
    with SQLiteExecutionStore.open_read_only(path) as store:
        lines, stored = store.history_snapshot()
    return verify_lines(lines, stored)


def failures(result: Report) -> set[tuple[str, str]]:
    return {(f.check, f.subject) for f in result.failures}


# -- the fixture ----------------------------------------------------------------------------


def test_the_fixture_has_exactly_the_previous_schema(old_store: Path, tmp_path: Path) -> None:
    reference = tmp_path / "reference.sqlite3"
    with closing(sqlite3.connect(reference)) as conn:
        conn.executescript(SCHEMA_V1 + MIGRATIONS[1] + MIGRATIONS[2])
    assert schema(old_store) == schema(reference)
    assert version(old_store) == PREVIOUS_SCHEMA
    with closing(sqlite3.connect(old_store)) as conn:
        states = {s for (s,) in conn.execute("SELECT state FROM executions")}
        statuses = {s for (s,) in conn.execute("SELECT status FROM operations")}
        resources = {s for (s,) in conn.execute("SELECT status FROM resources")}
    assert {s.value for s in S if not s.is_terminal} | {"succeeded", "failed"} == states
    assert {"intended", "ambiguous", "succeeded"} <= statuses
    assert {"active", "releasing", "quarantined"} == resources


# -- opening never migrates ---------------------------------------------------------------


def test_opening_for_writing_without_the_lock_never_migrates(old_store: Path) -> None:
    before, rows = digest(old_store), dump(old_store)
    with SQLiteExecutionStore(old_store) as store:
        assert store.get("r") is not None
        assert store.events() == ()
    assert digest(old_store) == before
    assert dump(old_store) == rows
    assert version(old_store) == PREVIOUS_SCHEMA


def test_a_store_without_coverage_refuses_fenced_writes(old_store: Path) -> None:
    """Only the lock-held claim may start coverage: a fence read from the old
    owner row cannot write uncovered changes."""
    with SQLiteExecutionStore(old_store) as store:
        fence = store.fence()
        assert fence is not None
        execution = store.get("q")
        assert execution is not None
        updated, step = MACHINE.advance(execution, S.CLAIMED, now=at(9), reason="r")
        with pytest.raises(FencingError, match="no event coverage"):
            store.commit(fence, Commit(applied=((updated, step),)))
        with pytest.raises(FencingError, match="no event coverage"):
            store.append_evidence(evidence_record("q"), fence=fence)
    assert version(old_store) == PREVIOUS_SCHEMA


@pytest.mark.parametrize("migrated", [False, True])
def test_a_read_only_open_never_writes(old_store: Path, migrated: bool) -> None:
    if migrated:
        with SQLiteExecutionStore(old_store) as store:
            store.claim_instance("inst", now=at(10))
    before, rows = digest(old_store), dump(old_store)
    with SQLiteExecutionStore.open_read_only(old_store) as store:
        execution = store.get("q")
        assert execution is not None
        fence = store.fence()
        assert fence is not None
        updated, step = MACHINE.advance(execution, S.CLAIMED, now=at(9), reason="r")
        new, created = MACHINE.start(TaskRef("fake", "new"), execution_id="new", now=at(9))
        writes: tuple[Callable[[], object], ...] = (
            lambda: store.claim_instance("inst", now=at(11)),
            lambda: store.commit(fence, Commit(applied=((updated, step),))),
            lambda: store.apply(updated, step, fence=fence),
            lambda: store.create(new, created),
            lambda: store.append_evidence(evidence_record("q"), fence=fence),
        )
        for write in writes:
            with pytest.raises(FencingError, match="read-only"):
                write()
        assert bool(store.events()) is migrated
    assert digest(old_store) == before
    assert dump(old_store) == rows


def test_events_and_verify_on_an_unmigrated_store_report_no_coverage(old_store: Path) -> None:
    before = digest(old_store)
    result = report(old_store)
    assert result.covered is False
    assert result.exit_code == 2
    assert digest(old_store) == before
    assert version(old_store) == PREVIOUS_SCHEMA


def test_a_read_only_open_of_an_older_schema_reports_no_coverage(tmp_path: Path) -> None:
    path = tmp_path / "v1.sqlite3"
    with closing(sqlite3.connect(path)) as conn:
        conn.executescript(SCHEMA_V1 + "PRAGMA user_version = 1;")
    before = digest(path)
    assert report(path).exit_code == 2
    with SQLiteExecutionStore.open_read_only(path) as store, pytest.raises(FencingError):
        store.get("x")  # the rows need the migration before they can be read
    assert digest(path) == before


def test_a_read_only_open_never_creates_a_database(tmp_path: Path) -> None:
    with pytest.raises(sqlite3.OperationalError):
        SQLiteExecutionStore.open_read_only(tmp_path / "missing.sqlite3")
    assert list(tmp_path.iterdir()) == []


# -- the migration, under the lock ----------------------------------------------------------


def test_the_claim_migrates_with_exactly_one_baseline_per_row(old_store: Path) -> None:
    with SQLiteExecutionStore(old_store) as store:
        before = {
            "executions": store.list_in_states(set(S)),
            "operations": [o for e in store.list_in_states(set(S)) for o in store.operations(e.id)],
            "resources": store.resources(),
        }
        fence = store.claim_instance("inst", now=at(10))
        events = store.events()
    assert fence.epoch == 3
    assert version(old_store) == SCHEMA_VERSION
    marker, *rest = events
    assert marker.type is EventType.COVERAGE_STARTED
    assert marker.data == {"from_schema_version": PREVIOUS_SCHEMA,
                           "executions": len(before["executions"]),
                           "operations": len(before["operations"]),
                           "resources": len(before["resources"])}  # fmt: skip
    baselines = [e for e in rest if e.type.value.endswith(".baseline")]
    assert len(baselines) == len(rest) - 1 == sum(len(v) for v in before.values())
    assert all((e.actor.kind.value, e.epoch, e.instance) == ("migration", 2, "inst")
               for e in [marker, *baselines])  # fmt: skip
    assert all(e.pass_id is None and e.reason is None for e in [marker, *baselines])
    (acquired,) = [e for e in rest if e.type is EventType.OWNERSHIP_ACQUIRED]
    assert acquired is rest[-1]
    assert acquired.data == {"previous_instance": "inst", "previous_epoch": 2}
    snapshot = {e.execution: e.data for e in baselines if e.type is EventType.EXECUTION_BASELINE}
    assert snapshot["v"] == {"state": "verifying", "version": 5, "transitions_before": 5,
                             "evidence_before": 1}  # fmt: skip
    assert {e.data["status"] for e in baselines if e.type is EventType.OPERATION_BASELINE} == {
        "intended", "ambiguous", "succeeded"}  # fmt: skip


def test_after_the_migration_verify_reports_history_and_no_failure(old_store: Path) -> None:
    with SQLiteExecutionStore(old_store) as store:
        store.claim_instance("inst", now=at(10))
    result = report(old_store)
    assert result.exit_code == 0
    assert result.failures == ()
    executions = {f.subject: f.detail for f in result.historical
                  if f.subject.startswith("execution:")}  # fmt: skip
    assert set(executions) == {f"execution:{e}" for e in PATHS}
    assert executions["execution:v"] == "5 transitions and 1 evidence records before coverage"
    assert all(f.finding_class is FindingClass.HISTORICAL for f in result.historical)


def test_the_migration_runs_once(old_store: Path) -> None:
    with SQLiteExecutionStore(old_store) as store:
        store.claim_instance("inst", now=at(10))
        store.claim_instance("inst", now=at(11))
    with SQLiteExecutionStore(old_store) as store:
        store.claim_instance("inst", now=at(12))
        events = store.events()
    assert [e.type for e in events].count(EventType.COVERAGE_STARTED) == 1
    assert [e.type for e in events].count(EventType.OWNERSHIP_ACQUIRED) == 3
    assert report(old_store).exit_code == 0


def test_covered_work_after_the_migration_continues_from_the_baseline(old_store: Path) -> None:
    with SQLiteExecutionStore(old_store) as store:
        fence = store.claim_instance("inst", now=at(10))
        (ambiguous,) = [o for o in store.unfinished_operations() if o.status is ST.AMBIGUOUS]
        store.commit(fence, Commit(operations=(ambiguous.moved(ST.FAILED, fence=fence,
                                                                 now=at(11)),)))  # fmt: skip
        (active,) = store.resources(statuses={R.ACTIVE})
        store.commit(fence, Commit(resources=(active.moved(R.RELEASING, now=at(11)),)))
        execution = store.get("q")
        assert execution is not None
        updated, step = MACHINE.advance(execution, S.CLAIMED, now=at(11), reason="r")
        store.commit(fence, Commit(applied=((updated, step),)))
        tail = store.events()[-3:]
    assert [(e.type.value, e.data.get("from")) for e in tail] == [
        ("operation.failed", "ambiguous"), ("resource.changed", "active"),
        ("execution.transitioned", "queued")]  # fmt: skip
    assert report(old_store).exit_code == 0


def test_a_new_database_starts_coverage_at_its_first_claim(tmp_path: Path) -> None:
    path = tmp_path / "new.sqlite3"
    with SQLiteExecutionStore(path) as store:
        assert version(path) == PREVIOUS_SCHEMA
        store.claim_instance("inst", now=at(0))
        marker, acquired = store.events()
    assert (marker.epoch, marker.data["executions"]) == (0, 0)  # never owned
    assert acquired.data == {}
    assert report(path).exit_code == 0


# -- a crash inside the migration ----------------------------------------------------------


@pytest.mark.parametrize("where", ["after-ddl", "baseline", "half-written", "claim"])
def test_a_failure_inside_the_migration_leaves_the_store_unchanged(
    old_store: Path, monkeypatch: pytest.MonkeyPatch, where: str
) -> None:
    before = dump(old_store)

    class Boom(Exception):  # noqa: N818 - a test-only crash
        pass

    real_append = sqlite_adapter._append_events
    calls = {"n": 0}

    def append(conn: sqlite3.Connection, events: object) -> None:
        calls["n"] += 1
        if where == "half-written":
            real_append(conn, list(events)[:3])  # type: ignore[call-overload]
            raise Boom
        if where == "claim" and calls["n"] == 2:  # the ownership event, after the baseline
            raise Boom
        real_append(conn, events)  # type: ignore[arg-type]

    def fail(*args: object, **kwargs: object) -> None:
        raise Boom

    real_script = sqlite_adapter._run_script

    def script_then_fail(conn: sqlite3.Connection, text: str) -> None:
        real_script(conn, text)  # the events table and its triggers now exist, uncommitted
        raise Boom

    if where == "after-ddl":
        monkeypatch.setattr(sqlite_adapter, "_run_script", script_then_fail)
    elif where == "baseline":
        monkeypatch.setattr(event_log, "baseline_events", fail)
    else:
        monkeypatch.setattr(sqlite_adapter, "_append_events", append)
    with SQLiteExecutionStore(old_store) as store, pytest.raises((Boom, sqlite3.Error)):
        store.claim_instance("inst", now=at(10))
    monkeypatch.undo()
    assert dump(old_store) == before
    assert version(old_store) == PREVIOUS_SCHEMA
    with SQLiteExecutionStore(old_store) as store:  # the next claim migrates, once
        store.claim_instance("inst", now=at(11))
        assert [e.type for e in store.events()].count(EventType.COVERAGE_STARTED) == 1
    assert report(old_store).exit_code == 0


KILL_DURING_MIGRATION = textwrap.dedent("""
    import os, sys
    from datetime import UTC, datetime
    from ilmar.adapters import sqlite as adapter

    real = adapter._append_events
    def dying(conn, events):
        real(conn, list(events)[:4])  # some baseline rows are in the transaction
        os._exit(9)                   # the process is killed mid-migration
    adapter._append_events = dying
    with adapter.SQLiteExecutionStore(sys.argv[1]) as store:
        store.claim_instance("inst", now=datetime(2026, 11, 8, tzinfo=UTC))
""")


def test_a_process_killed_inside_the_migration_leaves_the_store_unchanged(
    old_store: Path,
) -> None:
    before = dump(old_store)
    source = Path(sqlite_adapter.__file__).parents[2]  # the directory holding ``ilmar``
    killed = subprocess.run(  # noqa: S603 - our own interpreter and script
        [sys.executable, "-c", KILL_DURING_MIGRATION, str(old_store)],
        check=False, capture_output=True, timeout=60,
        env={**os.environ, "PYTHONPATH": str(source)},
    )  # fmt: skip
    assert killed.returncode == 9, killed.stderr
    assert dump(old_store) == before
    assert version(old_store) == PREVIOUS_SCHEMA
    assert report(old_store).exit_code == 2
    with SQLiteExecutionStore(old_store) as store:
        store.claim_instance("inst", now=at(11))
    assert report(old_store).exit_code == 0


# -- verify's classes, from edits made outside Ilmar ----------------------------------------


@pytest.fixture
def covered(old_store: Path) -> Path:
    with SQLiteExecutionStore(old_store) as store:
        fence = store.claim_instance("inst", now=at(10))
        execution = store.get("q")
        assert execution is not None
        updated, step = MACHINE.advance(execution, S.CLAIMED, now=at(11), reason="r")
        store.commit(fence, Commit(applied=((updated, step),)))
        store.append_evidence(evidence_record("q", seconds=12), fence=fence)
    assert report(old_store).exit_code == 0
    return old_store


def edit(path: Path, *statements: str) -> None:
    with closing(sqlite3.connect(path)) as conn:
        for statement in statements:
            conn.execute(statement)
        conn.commit()


EDITS: dict[str, tuple[tuple[str, ...], tuple[str, str]]] = {
    "row changed without an event": (
        ("UPDATE executions SET state = 'provisioning', version = 3 WHERE id = 'q'",),
        ("fold-mismatch", "execution:q"),
    ),
    "pre-baseline row changed": (
        ("UPDATE executions SET version = 9 WHERE id = 'ok'",),
        ("fold-mismatch", "execution:ok"),
    ),
    "extra transition after the baseline": (
        ("INSERT INTO transitions (execution_id, from_state, to_state, at, reason) VALUES "
         "('c', 'claimed', 'claimed', '2026-11-08T00:00:00.000000+00:00', 'x')",),
        ("count-mismatch", "execution:c"),
    ),
    "extra evidence after the baseline": (
        ("INSERT INTO evidence (execution_id, attempt, recorded_at, digest, record) "
         "SELECT execution_id, attempt, recorded_at, digest, record FROM evidence "
         "WHERE execution_id = 'v'",),
        ("count-mismatch", "execution:v"),
    ),
    "execution inserted with no events": (
        ("INSERT INTO executions (id, task_provider, task_key, state, is_terminal, attempt, "
         "version, created_at, updated_at) VALUES ('ghost', 'fake', 'ghost', 'queued', 0, 1, 1, "
         "'2026-11-08T00:00:00.000000+00:00', '2026-11-08T00:00:00.000000+00:00')",),
        ("no-coverage-event", "execution:ghost"),
    ),
    "event deleted after removing its trigger": (
        ("DROP TRIGGER events_are_append_only_delete",
         "DELETE FROM events WHERE seq = (SELECT MAX(seq) - 1 FROM events)"),
        ("seq-gap", "store"),
    ),
    "event rewritten after removing its trigger": (
        ("DROP TRIGGER events_are_append_only_update",
         "UPDATE events SET line = replace(line, '\"to\":\"claimed\"', '\"to\":\"queued\"') "
         "WHERE type = 'execution.transitioned'"),
        ("fold-mismatch", "execution:q"),
    ),
}  # fmt: skip


@pytest.mark.parametrize("name", sorted(EDITS))
def test_each_integrity_failure_is_detected(covered: Path, name: str) -> None:
    statements, expected = EDITS[name]
    edit(covered, *statements)
    result = report(covered)
    assert result.exit_code == 1
    assert expected in failures(result)
    # Never mixed: what is a failure is not also reported as history, and the
    # baselined history is still reported as history.
    assert not {(f.check, f.subject) for f in result.historical} & failures(result)
    assert any(f.subject == "execution:v" for f in result.historical)


@pytest.mark.parametrize(
    "statement",
    ["UPDATE events SET line = '{}'", "DELETE FROM events", "UPDATE events SET seq = seq + 100"],
)
def test_the_database_refuses_to_change_an_event(covered: Path, statement: str) -> None:
    with pytest.raises(sqlite3.IntegrityError, match="events are append-only"):
        edit(covered, statement)
    assert report(covered).exit_code == 0


def test_a_corrupt_event_line_is_reported_and_refused_on_read(covered: Path) -> None:
    edit(covered, "DROP TRIGGER events_are_append_only_update",
         "UPDATE events SET line = substr(line, 2) WHERE seq = 3")  # fmt: skip
    assert ("corrupt-event", "store") in failures(report(covered))
    with SQLiteExecutionStore.open_read_only(covered) as store, pytest.raises(CorruptEventError):
        store.events()


def test_a_seq_is_never_reused_after_tampering(covered: Path) -> None:
    edit(covered, "DROP TRIGGER events_are_append_only_delete",
         "DELETE FROM events WHERE seq = (SELECT MAX(seq) FROM events)")  # fmt: skip
    with SQLiteExecutionStore(covered) as store:
        last = store.events()[-1].seq or 0
        store.claim_instance("inst", now=at(20))
        assert store.events()[-1].seq == last + 2  # the deleted seq stays a gap
    assert ("seq-gap", "store") in failures(report(covered))


def test_a_stored_line_is_canonical_json(covered: Path) -> None:
    with closing(sqlite3.connect(covered)) as conn:
        rows = conn.execute("SELECT seq, type, execution_id, line FROM events").fetchall()
    for seq, kind, execution_id, line in rows:
        decoded = json.loads(line)
        assert json.dumps(decoded, sort_keys=True, separators=(",", ":")) == line
        assert (decoded["seq"], decoded["type"], decoded.get("execution")) == (
            seq, kind, execution_id)  # fmt: skip


def test_the_pairing_check_runs_inside_the_transaction(
    covered: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A derivation bug is refused before COMMIT: the rows roll back with it."""
    before = dump(covered)
    monkeypatch.setattr(event_log, "derive_events", lambda changes, context: ())
    with SQLiteExecutionStore(covered) as store:
        fence = store.fence()
        assert fence is not None
        execution = store.get("c")
        assert execution is not None
        updated, step = MACHINE.advance(execution, S.PROVISIONING, now=at(13), reason="r")
        with pytest.raises(LedgerIntegrityError, match="without an event"):
            store.commit(fence, Commit(applied=((updated, step),)))
    assert dump(covered) == before


def test_an_unclassified_event_in_history_still_verifies(old_store: Path) -> None:
    """History written before the boundary check (or by another caller through the
    store's fallback) stays readable: ``unclassified`` is refused as an input, never
    as a stored record."""
    with SQLiteExecutionStore(old_store) as store:
        fence = store.claim_instance("inst", now=at(10))
        execution = store.get("q")
        assert execution is not None
        updated, step = MACHINE.advance(execution, S.FAILED, now=at(11), reason="r", error="x")
        store.commit(fence, Commit(applied=((updated, step),)))  # no code: the fallback
    with SQLiteExecutionStore.open_read_only(old_store) as store:
        assert store.events()[-1].reason == "unclassified"
    assert report(old_store).exit_code == 0


# -- schema 5: instance-scoped operations (ADR-0026 §5) -------------------------------------


@pytest.fixture
def schema_4(tmp_path: Path) -> Path:
    """A covered store as schema 4 left it: every row and event."""
    path = tmp_path / "executions.sqlite3"
    with SQLiteExecutionStore(path) as store:
        populate(store)
    to_schema_4(path)
    return path


def operation_rows(path: Path) -> list[tuple[object, ...]]:
    with closing(sqlite3.connect(path)) as conn:
        return conn.execute("SELECT * FROM operations ORDER BY rowid").fetchall()


def event_rows(path: Path) -> list[tuple[object, ...]]:
    with closing(sqlite3.connect(path)) as conn:
        return conn.execute("SELECT * FROM events ORDER BY seq").fetchall()


def test_the_schema_4_fixture_requires_an_execution(schema_4: Path) -> None:
    assert version(schema_4) == 4
    with closing(sqlite3.connect(schema_4)) as conn:
        (sql,) = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'operations'"
        ).fetchone()
    assert "execution_id TEXT NOT NULL REFERENCES" in sql
    assert "release-orphan-claim" not in sql


def test_a_covered_store_migrates_to_5_without_a_new_baseline(schema_4: Path) -> None:
    rows, events = operation_rows(schema_4), event_rows(schema_4)
    with SQLiteExecutionStore(schema_4) as store:
        assert store.instance_operations() == ()  # readable before the migration
        fence = store.claim_instance("inst", now=at(10))
        added = store.events(after_seq=len(events))
    assert version(schema_4) == SCHEMA_VERSION == 5
    assert operation_rows(schema_4) == rows  # every row, byte for byte, in order
    assert event_rows(schema_4)[: len(events)] == events
    assert [e.type for e in added] == [EventType.OWNERSHIP_ACQUIRED]  # no baseline
    assert fence.epoch == 3
    assert report(schema_4).exit_code == 0  # I11 holds over the rebuilt table


def test_the_migrated_table_keeps_its_triggers_and_indexes(schema_4: Path) -> None:
    with SQLiteExecutionStore(schema_4) as store:
        store.claim_instance("inst", now=at(10))
    names = {name for _, name, _ in schema(schema_4)}
    assert {
        "operations_by_execution",
        "operations_unfinished",
        "operations_cannot_be_removed",
        "operations_final_are_immutable",
        "operations_identity_is_immutable",
    } <= names
    assert "operations_before" not in names
    with closing(sqlite3.connect(schema_4)) as conn, pytest.raises(sqlite3.IntegrityError):
        conn.execute("DELETE FROM operations")


@pytest.mark.parametrize(("kind", "attempt"), [("claim-task", 1), ("release-orphan-claim", 2)])
def test_the_database_refuses_an_unscoped_row_of_another_kind(
    schema_4: Path, kind: str, attempt: int
) -> None:
    with SQLiteExecutionStore(schema_4) as store:
        store.claim_instance("inst", now=at(10))
    with closing(sqlite3.connect(schema_4)) as conn, pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO operations VALUES (?, NULL, ?, ?, 'intended', 3, 1, 'x', 'x', '{}', "
            "'{}', '')",
            (f"{kind}:inst:{attempt}:s", attempt, kind),
        )


def test_a_failed_migration_to_5_changes_nothing(
    schema_4: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    before = dump(schema_4)
    monkeypatch.setitem(MIGRATIONS, 4, MIGRATIONS[4] + "\nSELECT no_such_function();\n")
    with SQLiteExecutionStore(schema_4) as store, pytest.raises(sqlite3.OperationalError):
        store.claim_instance("inst", now=at(10))
    assert dump(schema_4) == before
    assert version(schema_4) == 4


def test_a_schema_4_store_refuses_fenced_writes_until_claimed(schema_4: Path) -> None:
    with SQLiteExecutionStore(schema_4) as store:
        fence = store.fence()
        assert fence is not None
        with pytest.raises(FencingError, match="not migrated"):
            store.commit(fence, Commit())
    assert version(schema_4) == 4
