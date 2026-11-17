# SPDX-License-Identifier: Apache-2.0
"""ADR-0045: a narrow event read decodes only the events it returns, so the
per-pass reads cost the same however long the log grows (#147)."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from ilmar.adapters.memory import InMemoryExecutionStore
from ilmar.adapters.sqlite import SQLiteExecutionStore
from ilmar.cli_loop import trial_use
from ilmar.core.state_machine import ExecutionStateMachine
from ilmar.domain.events import Event, EventType
from ilmar.domain.execution import ExecutionState
from ilmar.domain.ledger import Commit
from ilmar.domain.tasks import TaskRef
from ilmar.ports.execution_store import ExecutionStore

T0 = datetime(2026, 1, 1, tzinfo=UTC)
MACHINE = ExecutionStateMachine(max_attempts=3)


@pytest.fixture(params=["memory", "sqlite"])
def store(request: pytest.FixtureRequest, tmp_path: Path) -> Iterator[ExecutionStore]:
    if request.param == "memory":
        yield InMemoryExecutionStore()
        return
    sqlite = SQLiteExecutionStore(tmp_path / "ilmar.db")
    yield sqlite
    sqlite.close()


@pytest.fixture
def decodes(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    count = [0]
    real: Callable[[str], Event] = Event.from_json

    def counting(line: str) -> Event:
        count[0] += 1
        return real(line)

    monkeypatch.setattr(Event, "from_json", staticmethod(counting))
    return count


def grow(store: ExecutionStore, executions: int, start: int = 0) -> None:
    """History: executions created and claimed, as many passes would leave."""
    fence = store.claim_instance("inst", now=T0)
    for n in range(start, start + executions):
        at = T0 + timedelta(minutes=n)
        execution, transition = MACHINE.start(TaskRef("fake", str(n)), execution_id=f"e{n}",
                                              now=at)  # fmt: skip
        store.create(execution, transition, fence=fence)
        updated, step = MACHINE.advance(execution, ExecutionState.CLAIMED, now=at, reason="c")
        store.commit(fence, Commit(applied=((updated, step),)))


def narrow_read_cost(store: ExecutionStore, decodes: list[int], probe: Callable[[], Any]) -> int:
    before = decodes[0]
    probe()
    return decodes[0] - before


@pytest.mark.parametrize(
    "probe",
    [
        lambda s: s.events(types=(EventType.OWNERSHIP_ACQUIRED,)),
        lambda s: s.events(instance_scoped=True, types=(EventType.OPERATION_INTENDED,)),
        lambda s: s.events(execution_id="e3"),
        lambda s: s.events(after_seq=10_000),
    ],
    ids=["by-type", "instance-intents", "one-execution", "after-the-end"],
)
def test_a_narrow_read_costs_the_same_as_history_grows_tenfold(
    store: ExecutionStore, decodes: list[int], probe: Callable[[ExecutionStore], Any]
) -> None:
    grow(store, 20)
    small = narrow_read_cost(store, decodes, lambda: probe(store))
    grow(store, 180, start=20)  # ten times the history
    large = narrow_read_cost(store, decodes, lambda: probe(store))
    assert large <= small + 1  # one more ownership event per growth step at most
    assert len(store.events()) > 400  # the log really did grow, tenfold


def test_a_full_read_still_decodes_everything(store: ExecutionStore, decodes: list[int]) -> None:
    """The hash-chain verification reads the whole log; that is unchanged."""
    grow(store, 30)
    before = decodes[0]
    everything = store.events()
    assert decodes[0] - before >= len(everything)


def test_trial_usage_reads_only_what_the_trial_counts(
    store: ExecutionStore, decodes: list[int], monkeypatch: pytest.MonkeyPatch
) -> None:
    """``trial_use`` asks the store for the trial's own usage events only."""
    seen: list[dict[str, Any]] = []
    real = store.events

    def recording(**kwargs: Any) -> Any:
        seen.append(kwargs)
        return real(**kwargs)

    record = type("R", (), {"seq": 7, "terms": type("T", (), {"starts_at": T0})()})()
    monkeypatch.setattr(store, "events", recording)
    monkeypatch.setattr(store, "trial_records", lambda: (record,))
    monkeypatch.setattr("ilmar.cli_loop.trial_in_force", lambda records, at: record)
    config = type("C", (), {"budget": None})()
    trial_use(store, config, T0)
    assert seen == [{
        "after_seq": 7,
        "types": (EventType.EXECUTION_CREATED, EventType.BUDGET_RESERVED,
                  EventType.BUDGET_CHARGED),
    }]  # fmt: skip


# -- SQLite: a regression guard on the real file store (#147 review) ------------------


def sqlite_store(tmp_path: Path) -> SQLiteExecutionStore:
    return SQLiteExecutionStore(tmp_path / "ilmar.db")


def per_pass_reads(store: ExecutionStore, trial_seq: int) -> None:
    """What a loop check and a pass read from the event log after #147: the trial's
    usage since its own record, and intents (instance-scoped and one execution's).
    Spend review is read by ``ilmar resume``, not by a pass."""
    usage = (EventType.EXECUTION_CREATED, EventType.BUDGET_RESERVED, EventType.BUDGET_CHARGED)
    store.events(after_seq=trial_seq, types=usage)
    store.events(instance_scoped=True, types=(EventType.OPERATION_INTENDED,))
    store.events(execution_id="e5", types=(EventType.OPERATION_INTENDED,))


def test_sqlite_per_pass_reads_decode_nothing_more_with_fifty_times_the_history(
    tmp_path: Path, decodes: list[int]
) -> None:
    store = sqlite_store(tmp_path)
    try:
        grow(store, 20)
        trial = len(store.events())  # a trial authorized now
        small = narrow_read_cost(store, decodes, lambda: per_pass_reads(store, trial))
        grow(store, 980, start=20)
        trial = len(store.events())  # the same moment in a much longer history
        large = narrow_read_cost(store, decodes, lambda: per_pass_reads(store, trial))
        assert trial > 2000
        assert large <= small + 2, (small, large)
    finally:
        store.close()


@pytest.mark.parametrize(
    ("query", "values"),
    [
        ("EXPLAIN QUERY PLAN SELECT seq, line FROM events WHERE seq > ? AND execution_id = ? "
         "AND type IN (?) ORDER BY seq", [0, "e5", "operation.intended"]),
        ("EXPLAIN QUERY PLAN SELECT seq, line FROM events WHERE seq > ? AND execution_id IS NULL "
         "AND type IN (?) ORDER BY seq", [0, "operation.intended"]),
    ],
    ids=["one-execution", "instance-scoped"],
)  # fmt: skip
def test_sqlite_narrow_reads_use_the_execution_index(
    tmp_path: Path, query: str, values: list[object]
) -> None:
    """The per-execution and instance-scoped reads are index searches, never a
    scan of the whole table (the type filter alone scans rows without decoding
    them; ADR-0045, Consequences)."""
    store = sqlite_store(tmp_path)
    try:
        grow(store, 50)
        conn = sqlite3.connect(tmp_path / "ilmar.db")
        try:
            plan = conn.execute(query, values).fetchall()
        finally:
            conn.close()
        detail = " ".join(str(row[-1]) for row in plan)
        assert "events_by_execution" in detail, detail
    finally:
        store.close()
