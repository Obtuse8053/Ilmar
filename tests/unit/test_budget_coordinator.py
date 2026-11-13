# SPDX-License-Identifier: Apache-2.0
"""Reservations, suspension and resumption through the coordinator (ADR-0029, #18).

Every attempt is charged its reservation in the commit that starts it. When
the allowances cannot take one more, the coordinator writes a durable
suspension, claims nothing and starts nothing, and the pass exits 78. A
provider refusal (spend limit, billing or authentication) fails its execution
and suspends in the same commit. Nothing is ever refunded.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from coordinator_support import Setup, make_setup, task
from ilmar.adapters.sqlite import SQLiteExecutionStore
from ilmar.core.coordinator import ExitCode
from ilmar.core.state_machine import RetryBackoff
from ilmar.core.suspension import resume_blocker
from ilmar.core.worker import Outcome
from ilmar.domain.agent import AgentOutcome
from ilmar.domain.budget import (
    BudgetLimits,
    BudgetTotals,
    ChargeKind,
    Suspension,
    SuspensionReason,
)
from ilmar.domain.events import Actor, ActorKind, EventContext, EventType
from ilmar.domain.execution import ExecutionState
from ilmar.domain.ledger import Commit

S = ExecutionState
LIMITS = BudgetLimits(daily_micros=2_000_000, monthly_micros=10_000_000, reserve_micros=1_000_000)
NO_WAIT = RetryBackoff(1.0, 1.0)
FIXED = {"src/app.py": "print('fixed')\n"}
NOW = datetime(2026, 1, 1, 12, tzinfo=UTC)
NOTHING = BudgetTotals()


@pytest.fixture
def setup(tmp_path: Path) -> Iterator[Setup]:
    store = SQLiteExecutionStore(tmp_path / "executions.sqlite3")
    made = make_setup(tmp_path, tasks=(task("1"), task("2"), task("3")), store=store)
    yield made
    made.ownership.release()
    store.close()


def reserved(setup: Setup) -> list[tuple[str, int]]:
    return [(e.execution or "", e.attempt or 0) for e in setup.store.events()
            if e.type is EventType.BUDGET_RESERVED]  # fmt: skip


def test_each_attempt_is_charged_its_reservation_as_it_starts(setup: Setup) -> None:
    setup.agent.plan = [(AgentOutcome.COMPLETED, FIXED)]
    report = setup.coordinator(budget=LIMITS).run_once()
    assert report.code is ExitCode.SUCCESS
    assert reserved(setup) == [("e1", 1)]
    (charge,) = setup.store.charges("e1")
    assert (charge.kind, charge.amount_micros) == (ChargeKind.RESERVATION, 1_000_000)
    assert setup.store.suspension() is None


def test_without_a_budget_nothing_is_charged(setup: Setup) -> None:
    setup.agent.plan = [(AgentOutcome.COMPLETED, FIXED)]
    setup.coordinator().run_once()
    assert reserved(setup) == []


def test_the_pass_that_consumes_the_last_reservation_exits_78(setup: Setup) -> None:
    """#113 review: two reservations fill the daily allowance exactly. The second
    pass finishes its execution, suspends with ``budget-daily`` and exits 78 in
    that same pass. Later passes claim and start nothing, and also exit 78."""
    setup.agent.plan = [(AgentOutcome.COMPLETED, FIXED)]
    coordinator = setup.coordinator(budget=LIMITS)
    assert coordinator.run_once().code is ExitCode.SUCCESS
    assert setup.store.suspension() is None  # one more reservation still fits
    second = coordinator.run_once()
    assert second.code is ExitCode.SUSPENDED
    assert [r.outcome for r in second.results] == [Outcome.PUBLISHED, Outcome.SUSPENDED]
    assert "suspended (budget-daily)" in second.summary
    assert setup.store.budget_totals(setup.clock.now).day_micros == LIMITS.daily_micros
    suspension = setup.store.suspension()
    assert suspension is not None
    assert suspension.reason is SuspensionReason.BUDGET_DAILY
    third = coordinator.run_once()
    assert third.code is ExitCode.SUSPENDED
    assert len(setup.store.list_in_states(set(S))) == 2  # no third execution was created
    assert len(reserved(setup)) == 2
    # A budget suspension never lifts by itself.
    assert coordinator.run_once().code is ExitCode.SUSPENDED


def test_a_retry_the_store_refuses_suspends_mid_pass_and_refunds_nothing(setup: Setup) -> None:
    """The first attempt fills the daily allowance and stalls; its retry would
    exceed it. The store refuses the second attempt atomically, the execution
    stays waiting with its attempt-1 reservation, and the pass suspends."""
    tight = BudgetLimits(daily_micros=1_000_000, monthly_micros=10_000_000,
                         reserve_micros=1_000_000)  # fmt: skip
    setup.agent.plan = [(AgentOutcome.STALLED, {}), (AgentOutcome.COMPLETED, FIXED)]
    coordinator = setup.coordinator(budget=tight, retry_backoff=NO_WAIT)
    report = coordinator.run_once()
    assert report.code is ExitCode.SUSPENDED
    execution = setup.store.get("e1")
    assert execution is not None
    assert (execution.state, execution.attempt) == (S.RETRY_WAIT, 1)
    assert reserved(setup) == [("e1", 1)]
    assert [r.outcome for r in report.results if r.execution_id == "e1"] == [Outcome.SUSPENDED]
    suspension = setup.store.suspension()
    assert suspension is not None
    assert suspension.reason is SuspensionReason.BUDGET_DAILY


def test_a_suspended_instance_admits_nothing_and_exits_78(setup: Setup) -> None:
    setup.agent.plan = [(AgentOutcome.COMPLETED, FIXED)]
    fence = setup.ownership.fence
    when = setup.clock()
    operator = EventContext(at=when, instance="inst", epoch=fence.epoch,
                            actor=Actor(ActorKind.HUMAN, "operator"))  # fmt: skip
    setup.store.commit(fence, Commit(suspend=Suspension(SuspensionReason.OPERATOR, when)),
                       context=operator)  # fmt: skip
    report = setup.coordinator(budget=LIMITS).run_once()
    assert report.code is ExitCode.SUSPENDED
    assert "suspended (operator-suspended)" in report.summary
    assert setup.store.get("e1") is None
    assert reserved(setup) == []


def test_a_provider_refusal_fails_and_suspends_in_one_commit(setup: Setup) -> None:
    setup.agent.plan = [(AgentOutcome.PROVIDER_REFUSED, {})]
    report = setup.coordinator(budget=LIMITS, retry_backoff=NO_WAIT).run_once()
    execution = setup.store.get("e1")
    assert execution is not None
    assert (execution.state, execution.attempt) == (S.FAILED, 1)
    suspension = setup.store.suspension()
    assert suspension is not None
    assert suspension.reason is SuspensionReason.PROVIDER_CAP
    events = setup.store.events()
    failed = next(e for e in events if e.type is EventType.EXECUTION_TRANSITIONED
                  and e.data.get("to") == "failed")  # fmt: skip
    suspended = next(e for e in events if e.type is EventType.INSTANCE_SUSPENDED)
    assert failed.seq is not None
    assert suspended.seq == failed.seq + 1  # the same commit, in order
    assert suspended.reason == "provider-cap"
    assert setup.coordinator(budget=LIMITS).run_once().code is ExitCode.SUSPENDED
    assert report.code is ExitCode.SUSPENDED  # in the pass that suspended (#113 review)
    assert [r.outcome for r in report.results].count(Outcome.SUSPENDED) == 1


def blocker(
    reason: SuspensionReason,
    totals: BudgetTotals = NOTHING,
    *,
    limits: BudgetLimits | None = LIMITS,
    latest_update: datetime = NOW,
    provider_cap_raised: bool = False,
    kill_switch_present: bool = False,
) -> str | None:
    return resume_blocker(
        Suspension(reason, NOW), now=NOW, totals=totals, limits=limits,
        latest_update=latest_update, provider_cap_raised=provider_cap_raised,
        kill_switch_present=kill_switch_present,
    )  # fmt: skip


def test_a_budget_suspension_lifts_only_when_another_reservation_fits() -> None:
    daily, monthly = SuspensionReason.BUDGET_DAILY, SuspensionReason.BUDGET_MONTHLY
    assert blocker(daily, BudgetTotals(2_000_000, 2_000_000)) is not None
    assert blocker(daily, BudgetTotals(1_000_000, 2_000_000)) is None  # exactly fits
    assert blocker(daily, BudgetTotals(0, 0), limits=None) is not None  # cannot tell
    assert blocker(monthly, BudgetTotals(0, 10_000_000)) is not None
    assert blocker(monthly, BudgetTotals(0, 9_000_000)) is None


def test_other_suspensions_lift_only_when_their_cause_is_gone() -> None:
    cap = SuspensionReason.PROVIDER_CAP
    assert blocker(cap) is not None
    assert blocker(cap, provider_cap_raised=True) is None
    clock = SuspensionReason.CLOCK_ANOMALY
    assert blocker(clock, latest_update=NOW + timedelta(hours=1)) is not None
    assert blocker(clock) is None
    kill = SuspensionReason.KILL_SWITCH
    assert blocker(kill, kill_switch_present=True) is not None
    assert blocker(kill) is None
    assert blocker(SuspensionReason.OPERATOR) is None


class _CoordinatorDied(BaseException):
    pass


@pytest.mark.parametrize("step", ["create-workspace", "agent-run"])
def test_a_crash_after_the_reservation_never_refunds_it(setup: Setup, step: str) -> None:
    """The reservation commits with the attempt's start. A crash anywhere after
    it (here: while provisioning, or while the agent runs) leaves it charged.
    Recovery resumes provisioning in the same attempt, or abandons an
    interrupted agent run and charges the next attempt its own reservation."""
    original_run = setup.agent.run

    def died(*args: object, **kwargs: object) -> object:
        raise _CoordinatorDied

    if step == "agent-run":
        setup.agent.run = died  # type: ignore[method-assign,assignment]
    else:
        setup.components.layout = _Exploding(setup.components.layout)  # type: ignore[assignment]
    with pytest.raises(_CoordinatorDied):
        setup.coordinator(budget=LIMITS, retry_backoff=NO_WAIT).run_once()
    assert reserved(setup) == [("e1", 1)]
    setup.agent.run = original_run  # type: ignore[method-assign]
    if step != "agent-run":
        setup.components.layout = setup.components.layout.inner  # type: ignore[attr-defined]
    setup.agent.plan = [(AgentOutcome.COMPLETED, FIXED)]
    roomy = BudgetLimits(daily_micros=5_000_000, monthly_micros=10_000_000,
                         reserve_micros=1_000_000)  # fmt: skip
    coordinator = setup.coordinator(budget=roomy, retry_backoff=NO_WAIT)
    coordinator.run_once()
    recovered = setup.store.get("e1")
    assert recovered is not None
    if recovered.state is S.RETRY_WAIT:
        assert recovered.retry_not_before is not None
        setup.clock.now = recovered.retry_not_before
        coordinator.run_once()
    done = setup.store.get("e1")
    assert done is not None
    # Provisioning resumes the same attempt; an interrupted agent run needs a new
    # one. Either way every attempt that started is charged exactly once.
    attempts = 1 if step == "create-workspace" else 2
    assert (done.state, done.attempt) == (S.SUCCEEDED, attempts)
    assert [c.attempt for c in setup.store.charges("e1")] == list(range(1, attempts + 1))
    assert [a for e, a in reserved(setup) if e == "e1"] == list(range(1, attempts + 1))


class _Exploding:
    """A layout whose agent workspace cannot be made: the process 'dies' there."""

    def __init__(self, inner: object) -> None:
        self.inner = inner

    def __getattr__(self, name: str) -> object:
        if name == "agent_workspace":
            raise _CoordinatorDied
        return getattr(self.inner, name)
