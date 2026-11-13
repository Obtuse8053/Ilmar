# SPDX-License-Identifier: Apache-2.0
"""A suspension interrupts a running agent; the work is retried only after a
resume (ADR-0029 §3 and §4; maintainer decision on #18, 2026-11-13).

* The cancellation watcher also stops the agent when the kill-switch file
  appears or a suspension is written, by this process or another.
* The attempt ends as ``RETRY_WAIT`` with reason code
  ``suspension-interrupted``: its output is never frozen, it counts as an
  attempt, and the existing retry limits apply.
* The next attempt needs a fresh reservation, which the store refuses while
  suspended: it starts only after ``ilmar resume``.
* A human cancellation stays terminal (``test_cancel_coordinator.py``).
"""

from __future__ import annotations

import sqlite3
import threading
from collections.abc import Iterator
from pathlib import Path

import pytest

from coordinator_support import Setup, make_setup, task
from ilmar.adapters.sqlite import SQLiteExecutionStore
from ilmar.core import kill_switch
from ilmar.core.cancel_inbox import Reading
from ilmar.core.cancellation import Cancellations
from ilmar.core.coordinator import ExitCode
from ilmar.core.suspension import resume_blocker
from ilmar.domain.budget import BudgetLimits, ChargeKind, Suspension, SuspensionReason
from ilmar.domain.events import Actor, ActorKind, EventContext, EventType
from ilmar.domain.execution import ExecutionState
from ilmar.domain.ledger import Commit, OperationKind
from unit.test_cancel_coordinator import WaitingAgent

S = ExecutionState
K = OperationKind
LIMITS = BudgetLimits(daily_micros=10_000_000, monthly_micros=50_000_000, reserve_micros=1_000_000)


@pytest.fixture
def setup(tmp_path: Path) -> Iterator[Setup]:
    store = SQLiteExecutionStore(tmp_path / "executions.sqlite3")
    made = make_setup(tmp_path, tasks=(task("1"),), store=store)
    yield made
    made.ownership.release()
    store.close()


@pytest.fixture
def agent(setup: Setup) -> Iterator[WaitingAgent]:
    waiting = WaitingAgent(setup, submit_for=None)  # no human request: only a suspension
    original = setup.components.agent_runner
    setup.components.agent_runner = waiting
    yield waiting
    setup.components.agent_runner = original


def operator(setup: Setup) -> EventContext:
    fence = setup.ownership.fence
    return EventContext(at=setup.clock(), instance="inst", epoch=fence.epoch,
                        actor=Actor(ActorKind.HUMAN, "operator"))  # fmt: skip


def interrupted_retry(setup: Setup) -> None:
    execution = setup.store.get("e1")
    assert execution is not None
    assert (execution.state, execution.attempt) == (S.RETRY_WAIT, 1)
    codes = [e.reason for e in setup.store.events(execution_id="e1")
             if e.type is EventType.EXECUTION_TRANSITIONED]  # fmt: skip
    assert codes[-1] == "suspension-interrupted"
    kinds = [o.kind for o in setup.store.operations("e1")]
    assert K.FREEZE_CANDIDATE not in kinds  # an interrupted run's output is never frozen
    assert len(setup.tasks.held_claims()) == 1  # the claim is kept for the retry


def test_the_kill_switch_stops_a_running_agent_for_a_retry_after_resume(
    setup: Setup, agent: WaitingAgent, tmp_path: Path
) -> None:
    kill = tmp_path / "KILL"

    def engage() -> None:
        kill.write_text("")  # engaged while the agent runs

    agent.before = engage
    coordinator = setup.coordinator(budget=LIMITS, kill_switch=kill_switch.checker(kill),
                                    watch_interval_seconds=0.05)  # fmt: skip
    report = coordinator.run_once()
    assert agent.stops[0] is not None
    assert agent.stops[0].is_set()
    assert report.code is ExitCode.SUSPENDED
    held = setup.store.suspension()
    assert held is not None
    assert held.reason is SuspensionReason.KILL_SWITCH
    interrupted_retry(setup)
    # Still engaged: later passes start nothing, and the retry is not even reserved.
    assert coordinator.run_once().code is ExitCode.SUSPENDED
    assert [c.attempt for c in setup.store.charges("e1")] == [1]
    # Resume: the file is gone, the suspension lifted; the retry gets a fresh reservation.
    kill.unlink()
    setup.store.commit(setup.ownership.fence, Commit(resume=setup.clock()),
                       context=operator(setup))  # fmt: skip
    agent.before = None
    agent.patience = 0.0  # this time the agent finishes
    report = coordinator.run_once()
    assert report.code is ExitCode.SUCCESS, report.summary
    execution = setup.store.get("e1")
    assert execution is not None
    assert (execution.state, execution.attempt) == (S.SUCCEEDED, 2)
    charges = setup.store.charges("e1")
    assert [(c.attempt, c.kind) for c in charges] == [(1, ChargeKind.RESERVATION),
                                                      (2, ChargeKind.RESERVATION)]  # fmt: skip


def test_a_suspension_written_elsewhere_stops_a_running_agent(
    setup: Setup, agent: WaitingAgent
) -> None:
    """``ilmar suspend`` from another process: the watcher reads the store."""
    held = Suspension(SuspensionReason.OPERATOR, setup.clock())
    agent.before = lambda: setup.store.commit(
        setup.ownership.fence, Commit(suspend=held), context=operator(setup)
    )
    report = setup.coordinator(budget=LIMITS, watch_interval_seconds=0.05).run_once()
    assert report.code is ExitCode.SUSPENDED
    interrupted_retry(setup)


def test_the_existing_retry_limits_apply(setup: Setup, agent: WaitingAgent, tmp_path: Path) -> None:
    kill = tmp_path / "KILL"

    def engage() -> None:
        kill.write_text("")

    agent.before = engage
    setup.coordinator(budget=LIMITS, kill_switch=kill_switch.checker(kill),
                      watch_interval_seconds=0.05, max_attempts=1).run_once()  # fmt: skip
    execution = setup.store.get("e1")
    assert execution is not None
    assert execution.state is S.FAILED  # no attempt left: as for any interrupted attempt


def on_watcher_thread() -> bool:
    return threading.current_thread().name.startswith("ilmar-watch-")


def test_a_failing_suspension_check_suspends_the_instance_and_exits_78(
    setup: Setup, agent: WaitingAgent, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#122 review: a suspension check that fails while the agent runs stops it
    and durably suspends the whole instance; the pass exits 78. The task and its
    charged reservation are kept; nothing is retried before an operator resumes
    and the check succeeds again."""
    real = setup.store.suspension
    failing = [True]

    def suspension() -> Suspension | None:
        if failing[0] and on_watcher_thread():
            raise sqlite3.OperationalError("database is locked")
        return real()

    monkeypatch.setattr(setup.store, "suspension", suspension)
    coordinator = setup.coordinator(budget=LIMITS, watch_interval_seconds=0.05)
    report = coordinator.run_once()
    assert report.code is ExitCode.SUSPENDED
    assert agent.stops[0] is not None
    assert agent.stops[0].is_set()
    assert agent.stopped_at - agent.submitted_at < agent.patience  # stopped, not finished
    held = setup.store.suspension()
    assert held is not None
    assert held.reason is SuspensionReason.SUSPENSION_CHECK_FAILED
    assert "OperationalError" in held.detail
    interrupted_retry(setup)  # RETRY_WAIT at attempt 1, nothing frozen, claim kept
    assert agent.runs == 1  # no automatic retry
    assert [c.attempt for c in setup.store.charges("e1")] == [1]  # kept, never refunded
    suspended = [e for e in setup.store.events() if e.type is EventType.INSTANCE_SUSPENDED]
    assert [e.reason for e in suspended] == ["suspension-check-failed"]

    # Later passes start nothing and reserve nothing while it is in force.
    assert coordinator.run_once().code is ExitCode.SUSPENDED
    assert agent.runs == 1
    assert [c.attempt for c in setup.store.charges("e1")] == [1]

    # Only an operator lifts it, once the check succeeds; then the task is
    # retried with a fresh reservation under the usual limits.
    blocked = resume_blocker(held, now=setup.clock(), totals=setup.store.budget_totals(
        setup.clock()), limits=LIMITS, latest_update=None, provider_cap_raised=False,
        kill_switch_present=False)  # fmt: skip
    assert blocked is not None
    assert "--check-fault-resolved" in blocked
    failing[0] = False
    setup.store.commit(setup.ownership.fence, Commit(resume=setup.clock()),
                       context=operator(setup))  # fmt: skip
    agent.patience = 0.0
    report = coordinator.run_once()
    assert report.code is ExitCode.SUCCESS, report.summary
    execution = setup.store.get("e1")
    assert execution is not None
    assert (execution.state, execution.attempt) == (S.SUCCEEDED, 2)
    assert [c.attempt for c in setup.store.charges("e1")] == [1, 2]


def test_a_failing_check_suspends_even_with_one_attempt_left(
    setup: Setup, agent: WaitingAgent, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The check failure suspends; it never consumes retries until the task fails."""
    real = setup.store.suspension

    def suspension() -> Suspension | None:
        if on_watcher_thread():
            raise RuntimeError("unexpected")
        return real()

    monkeypatch.setattr(setup.store, "suspension", suspension)
    report = setup.coordinator(budget=LIMITS, watch_interval_seconds=0.05,
                               max_attempts=3).run_once()  # fmt: skip
    assert report.code is ExitCode.SUSPENDED
    assert agent.runs == 1
    execution = setup.store.get("e1")
    assert execution is not None
    assert execution.state is S.RETRY_WAIT


def test_a_failing_inbox_read_never_hides_the_kill_switch(
    setup: Setup, agent: WaitingAgent, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#122 review: the watcher's inbox read fails on every tick; the kill
    switch still stops the agent."""
    kill = tmp_path / "KILL"
    real = Cancellations.read

    def read(self: Cancellations, execution_id: str) -> Reading:
        if on_watcher_thread():
            raise RuntimeError("the inbox is unreadable")
        return real(self, execution_id)

    monkeypatch.setattr(Cancellations, "read", read)

    def engage() -> None:
        kill.write_text("")

    agent.before = engage
    report = setup.coordinator(budget=LIMITS, kill_switch=kill_switch.checker(kill),
                               watch_interval_seconds=0.05).run_once()  # fmt: skip
    assert agent.stops[0] is not None
    assert agent.stops[0].is_set()
    assert report.code is ExitCode.SUSPENDED
    interrupted_retry(setup)


def test_a_suspension_that_cannot_be_written_stops_the_pass_and_ends_nothing(
    setup: Setup, agent: WaitingAgent, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fail closed when even the suspension fails: the pass stops and the
    attempt is not ended, so nothing is retried, frozen or published."""
    real_suspension, real_commit = setup.store.suspension, setup.store.commit

    def suspension() -> Suspension | None:
        if on_watcher_thread():
            raise sqlite3.OperationalError("database is locked")
        return real_suspension()

    def commit(*args: object, **kwargs: object) -> object:
        change = args[1] if len(args) > 1 else kwargs.get("commit")
        if isinstance(change, Commit) and change.suspend is not None:
            raise sqlite3.OperationalError("disk I/O error")
        return real_commit(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(setup.store, "suspension", suspension)
    monkeypatch.setattr(setup.store, "commit", commit)
    report = setup.coordinator(budget=LIMITS, watch_interval_seconds=0.05).run_once()
    assert report.code is ExitCode.BLOCKED
    assert agent.runs == 1
    execution = setup.store.get("e1")
    assert execution is not None
    assert execution.state is S.RUNNING_AGENT  # not ended: the journal shows where it stopped
    assert K.FREEZE_CANDIDATE not in [o.kind for o in setup.store.operations("e1")]
