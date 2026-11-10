# SPDX-License-Identifier: Apache-2.0
"""Retry backoff and durable scheduling (#13, ADR-0028, ADR-0029 §1).

A transient failure records ``retry_not_before`` in the same commit as the
move to RETRY_WAIT. No new attempt starts before it, in this process or any
later one, and a wall clock behind the store's latest persisted time only
waits longer.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from coordinator_support import Setup, make_setup, task
from ilmar.core.state_machine import (
    NO_BACKOFF,
    ExecutionStateMachine,
    RetryBackoff,
    attempt_wait_reason,
)
from ilmar.core.worker import Outcome
from ilmar.domain.agent import AgentOutcome
from ilmar.domain.errors import InvalidTransitionError
from ilmar.domain.execution import Execution, ExecutionState
from ilmar.domain.ledger import Commit
from ilmar.domain.tasks import TaskRef

S = ExecutionState
T0 = datetime(2026, 11, 10, 12, 0, tzinfo=UTC)
BACKOFF = RetryBackoff(300.0, 1200.0)
FIXED = {"src/app.py": "print('fixed')\n"}


# -- the policy -------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("failed_attempt", "seconds"), [(1, 300), (2, 600), (3, 1200), (4, 1200), (80, 1200)]
)
def test_the_wait_doubles_per_attempt_up_to_the_cap(failed_attempt: int, seconds: int) -> None:
    assert BACKOFF.delay(failed_attempt) == timedelta(seconds=seconds)
    assert BACKOFF.not_before(T0, failed_attempt) == T0 + timedelta(seconds=seconds)


@pytest.mark.parametrize(("base", "cap"), [(-1.0, 10.0), (10.0, 5.0)])
def test_an_inconsistent_backoff_is_refused(base: float, cap: float) -> None:
    with pytest.raises(ValueError, match="must not be"):
        RetryBackoff(base, cap)


def test_attempts_start_at_one() -> None:
    with pytest.raises(ValueError, match="attempts start at 1"):
        BACKOFF.delay(0)


# -- the state machine enforces the wait -------------------------------------------------


def waiting(not_before: datetime | None = T0 + timedelta(seconds=300)) -> Execution:
    machine = ExecutionStateMachine(max_attempts=3)
    execution, _ = machine.start(TaskRef("fake", "1"), execution_id="e1", now=T0)
    for target in (S.CLAIMED, S.PROVISIONING):
        execution, _ = machine.advance(execution, target, now=T0, reason="r")
    execution, _ = machine.advance(
        execution, S.RETRY_WAIT, now=T0, reason="r", error="boom", retry_not_before=not_before
    )
    return execution


def test_entering_retry_wait_records_the_time_and_leaving_clears_it() -> None:
    execution = waiting()
    assert execution.retry_not_before == T0 + timedelta(seconds=300)
    machine = ExecutionStateMachine(max_attempts=3)
    later = T0 + timedelta(seconds=300)
    retried, _ = machine.advance(execution, S.PROVISIONING, now=later, reason="r")
    assert (retried.attempt, retried.retry_not_before) == (2, None)
    failed, _ = machine.advance(execution, S.FAILED, now=T0, reason="r", error="gave up")
    assert failed.retry_not_before is None


def test_a_new_attempt_before_its_time_is_refused() -> None:
    machine = ExecutionStateMachine(max_attempts=3)
    with pytest.raises(InvalidTransitionError, match="may not retry before"):
        machine.advance(waiting(), S.PROVISIONING, now=T0 + timedelta(seconds=299), reason="r")


def test_retry_not_before_belongs_only_to_retry_wait() -> None:
    machine = ExecutionStateMachine(max_attempts=3)
    execution, _ = machine.start(TaskRef("fake", "1"), execution_id="e1", now=T0)
    with pytest.raises(ValueError, match="only on entering retry_wait"):
        machine.advance(execution, S.CLAIMED, now=T0, reason="r", retry_not_before=T0)
    with pytest.raises(ValueError, match="only an execution in retry_wait"):
        Execution(
            id="e1", task=TaskRef("fake", "1"), state=S.CLAIMED, attempt=1, version=1,
            created_at=T0, updated_at=T0, retry_not_before=T0,
        )  # fmt: skip


def test_retry_not_before_never_lies_in_the_past() -> None:
    with pytest.raises(ValueError, match="must not precede now"):
        waiting(not_before=T0 - timedelta(seconds=1))


def test_due_needs_both_the_backoff_and_the_high_water_mark_behind_now() -> None:
    execution = waiting()
    due_at = T0 + timedelta(seconds=300)
    early = attempt_wait_reason(execution, due_at - timedelta(seconds=1), latest_update=T0)
    assert early == f"retry not before {due_at.isoformat()}"
    assert attempt_wait_reason(execution, due_at, latest_update=T0) is None
    # The backoff is over, but the clock is behind a persisted time: wait.
    later = due_at + timedelta(seconds=5)
    behind = attempt_wait_reason(execution, due_at, latest_update=later)
    assert behind is not None
    assert "behind the latest persisted time" in behind
    # A wait recorded before schema 7 has no time: due at once.
    assert attempt_wait_reason(waiting(not_before=None), T0, latest_update=None) is None


def test_only_an_execution_about_to_start_an_attempt_is_asked() -> None:
    machine = ExecutionStateMachine(max_attempts=3)
    execution, _ = machine.start(TaskRef("fake", "1"), execution_id="e1", now=T0)
    with pytest.raises(ValueError, match="not about to start an attempt"):
        attempt_wait_reason(execution, T0, latest_update=None)
    claimed, _ = machine.advance(execution, S.CLAIMED, now=T0, reason="r")
    assert attempt_wait_reason(claimed, T0, latest_update=T0) is None
    assert attempt_wait_reason(claimed, T0, latest_update=T0 + timedelta(seconds=1)) is not None


# -- the coordinator ---------------------------------------------------------------------


@pytest.fixture
def setup(tmp_path: Path) -> Setup:
    return make_setup(tmp_path)


def test_a_transient_failure_waits_instead_of_retrying_in_the_same_pass(setup: Setup) -> None:
    setup.agent.plan = [(AgentOutcome.TIMED_OUT, {}), (AgentOutcome.COMPLETED, FIXED)]
    report = setup.coordinator(retry_backoff=BACKOFF).run_once()
    execution = setup.store.get("e1")
    assert execution is not None
    assert (execution.state, execution.attempt, setup.agent.runs) == (S.RETRY_WAIT, 1, 1)
    assert execution.retry_not_before == execution.updated_at + timedelta(seconds=300)
    assert report.code == 0

    # Passes before the time change nothing and report the wait.
    report = setup.coordinator(retry_backoff=BACKOFF).run_once()
    assert [r.outcome for r in report.results] == [Outcome.WAITING]
    assert "retry not before" in report.results[0].detail
    assert setup.agent.runs == 1
    assert setup.store.get("e1") == execution

    setup.clock.now = execution.retry_not_before
    setup.coordinator(retry_backoff=BACKOFF).run_once()
    done = setup.store.get("e1")
    assert done is not None
    assert (done.state, done.attempt, setup.agent.runs) == (S.SUCCEEDED, 2, 2)


def test_each_failed_attempt_waits_twice_as_long(setup: Setup) -> None:
    setup.agent.plan = [(AgentOutcome.TIMED_OUT, {})]
    waits = []
    for _ in range(3):
        setup.coordinator(retry_backoff=BACKOFF).run_once()
        execution = setup.store.get("e1")
        assert execution is not None
        if execution.state is not S.RETRY_WAIT:
            break
        assert execution.retry_not_before is not None
        waits.append((execution.retry_not_before - execution.updated_at).total_seconds())
        setup.clock.now = execution.retry_not_before
    assert waits == [300.0, 600.0]
    final = setup.store.get("e1")
    assert final is not None
    assert (final.state, final.attempt, setup.agent.runs) == (S.FAILED, 3, 3)


def test_a_restart_does_not_shorten_the_wait(setup: Setup) -> None:
    """A new coordinator (a new process) reads the time from the store."""
    setup.agent.plan = [(AgentOutcome.TIMED_OUT, {}), (AgentOutcome.COMPLETED, FIXED)]
    setup.coordinator(retry_backoff=BACKOFF).run_once()
    # Even a restarted coordinator configured without any backoff waits for the
    # time already recorded.
    report = setup.coordinator(retry_backoff=NO_BACKOFF).run_once()
    assert [r.outcome for r in report.results] == [Outcome.WAITING]
    assert setup.agent.runs == 1


def test_a_clock_behind_the_latest_persisted_time_waits(setup: Setup) -> None:
    """A restarted process whose wall clock stepped back never retries early."""
    setup.agent.plan = [(AgentOutcome.TIMED_OUT, {}), (AgentOutcome.COMPLETED, FIXED)]
    setup.coordinator(retry_backoff=BACKOFF).run_once()
    execution = setup.store.get("e1")
    assert execution is not None
    setup.clock.now = execution.updated_at - timedelta(hours=1)  # a new process, clock behind
    report = setup.coordinator(retry_backoff=NO_BACKOFF).run_once()
    assert [r.outcome for r in report.results] == [Outcome.WAITING]
    assert "behind the latest persisted time" in report.results[0].detail
    assert setup.agent.runs == 1
    assert setup.store.get("e1") == execution


def test_a_waiting_execution_does_not_hold_up_other_tasks(tmp_path: Path) -> None:
    setup = make_setup(tmp_path, tasks=(task("1"), task("2")))
    setup.agent.plan = [(AgentOutcome.TIMED_OUT, {}), (AgentOutcome.COMPLETED, FIXED)]
    setup.coordinator(retry_backoff=BACKOFF).run_once()  # e1 fails once and waits
    report = setup.coordinator(retry_backoff=BACKOFF).run_once()
    assert [r.outcome for r in report.results] == [Outcome.WAITING, Outcome.PUBLISHED]
    waiting, published = report.results
    first, second = setup.store.get(waiting.execution_id), setup.store.get(published.execution_id)
    assert first is not None
    assert second is not None
    assert (first.task.key, first.state) == ("1", S.RETRY_WAIT)
    assert (second.task.key, second.state) == ("2", S.SUCCEEDED)


def test_a_cancellation_ends_a_waiting_execution_without_a_new_attempt(setup: Setup) -> None:
    setup.agent.plan = [(AgentOutcome.TIMED_OUT, {})]
    setup.coordinator(retry_backoff=BACKOFF).run_once()
    setup.tasks.close(task("1").ref)  # the tracker no longer lists it
    setup.coordinator(retry_backoff=BACKOFF).run_once()
    execution = setup.store.get("e1")
    assert execution is not None
    assert (execution.state, execution.retry_not_before) == (S.CANCELLED, None)
    assert setup.agent.runs == 1


# -- ADR-0028 §3 for every admission of new work (review of #103) ------------------------


def test_a_clock_behind_after_a_restart_starts_no_new_task(tmp_path: Path) -> None:
    """Regression (maintainer review of #103): the clock rule held for retries but
    not for new tasks. After a restart with the clock stepped back, an eligible
    task must not start until the clock catches up."""
    setup = make_setup(tmp_path, tasks=(task("1"), task("2")))
    setup.agent.plan = [(AgentOutcome.COMPLETED, FIXED)]
    setup.coordinator(retry_backoff=BACKOFF).run_once()  # task 1 runs and publishes
    latest = setup.store.latest_update()
    assert latest is not None
    executions = setup.store.list_in_states(set(S))
    assert [e.task.key for e in executions] == ["1"]

    setup.clock.now = latest - timedelta(hours=2)  # a new process, clock stepped back
    report = setup.coordinator(retry_backoff=BACKOFF).run_once()
    assert [(r.execution_id, r.outcome) for r in report.results] == [("admission", Outcome.WAITING)]
    assert "behind the latest persisted time" in report.results[0].detail
    assert report.code == 0
    assert setup.store.list_in_states(set(S)) == executions  # task 2 not started
    assert setup.agent.runs == 1

    setup.clock.now = latest  # the clock catches up
    report = setup.coordinator(retry_backoff=BACKOFF).run_once()
    assert [r.outcome for r in report.results] == [Outcome.PUBLISHED]
    assert {e.task.key for e in setup.store.list_in_states({S.SUCCEEDED})} == {"1", "2"}


class _CrashError(Exception):
    """Ends a pass right after a commit, as a process crash would."""


def test_a_claimed_execution_does_not_start_its_first_attempt_while_behind(
    setup: Setup,
) -> None:
    """A crash right after the claim, then a restart with the clock stepped back:
    the first attempt waits for the clock, as a retry would."""
    from tracker_support import on_commit

    def claimed(changes: Commit) -> bool:
        return any(t.to_state is S.CLAIMED for _, t in changes.applied)

    def crash() -> None:
        raise _CrashError

    on_commit(setup.store, claimed, crash)
    # The pass isolates the unexpected error to this execution, as at a crash.
    setup.coordinator(retry_backoff=BACKOFF).run_once()
    execution = setup.store.get("e1")
    assert execution is not None
    assert execution.state is S.CLAIMED

    setup.clock.now = execution.updated_at - timedelta(minutes=10)
    report = setup.coordinator(retry_backoff=BACKOFF).run_once()
    assert [(r.execution_id, r.outcome) for r in report.results] == [("e1", Outcome.WAITING)]
    assert setup.store.get("e1") == execution
    assert setup.agent.runs == 0

    setup.clock.now = execution.updated_at
    setup.coordinator(retry_backoff=BACKOFF).run_once()
    done = setup.store.get("e1")
    assert done is not None
    assert (done.state, done.attempt) == (S.SUCCEEDED, 1)
