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

from collections.abc import Iterator
from pathlib import Path

import pytest

from coordinator_support import Setup, make_setup, task
from ilmar.adapters.sqlite import SQLiteExecutionStore
from ilmar.core import kill_switch
from ilmar.core.coordinator import ExitCode
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
