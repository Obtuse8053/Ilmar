# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import itertools
from datetime import timedelta

import pytest

from ilmar.core.state_machine import ALLOWED_TRANSITIONS, ExecutionStateMachine, can_transition
from ilmar.domain.errors import InvalidTransitionError, RetryLimitExceededError
from ilmar.domain.execution import Execution, ExecutionState
from ilmar.domain.tasks import TaskRef

from helpers import at

S = ExecutionState
TASK = TaskRef("fake", "1")
BASE, CANDIDATE = "a" * 40, "b" * 40

HAPPY_PATH = [
    S.CLAIMED,
    S.PROVISIONING,
    S.RUNNING_AGENT,
    S.VERIFYING,
    S.PUBLISHING,
    S.SUCCEEDED,
]


def started(machine: ExecutionStateMachine) -> Execution:
    execution, _ = machine.start(TASK, execution_id="e1", now=at(0))
    return execution


def walk(
    machine: ExecutionStateMachine, execution: Execution, *targets: ExecutionState
) -> Execution:
    for target in targets:
        error = "failure" if target in (S.FAILED, S.RETRY_WAIT) else None
        execution, _ = machine.advance(
            execution,
            target,
            now=execution.updated_at + timedelta(seconds=1),
            reason="test",
            error=error,
            base_revision=BASE if target is S.RUNNING_AGENT else None,
            candidate_revision=CANDIDATE if target is S.VERIFYING else None,
        )
    return execution


@pytest.fixture
def machine() -> ExecutionStateMachine:
    return ExecutionStateMachine(max_attempts=3)


def test_start_creates_queued_execution(machine: ExecutionStateMachine) -> None:
    execution, transition = machine.start(TASK, execution_id="e1", now=at(0))
    assert execution.state is S.QUEUED
    assert (execution.attempt, execution.version) == (1, 1)
    assert transition.from_state is None
    assert transition.to_state is S.QUEUED


def test_happy_path_reaches_succeeded(machine: ExecutionStateMachine) -> None:
    execution = walk(machine, started(machine), *HAPPY_PATH)
    assert execution.state is S.SUCCEEDED
    assert execution.version == 1 + len(HAPPY_PATH)
    assert execution.attempt == 1


def test_every_state_is_covered() -> None:
    assert set(ALLOWED_TRANSITIONS) == set(S)


def test_terminal_states_have_no_exits() -> None:
    for state in S:
        assert (not ALLOWED_TRANSITIONS[state]) == state.is_terminal


def test_every_non_terminal_state_can_fail_or_be_cancelled() -> None:
    for state in S:
        if not state.is_terminal:
            assert can_transition(state, S.FAILED)
            assert can_transition(state, S.CANCELLED)


def test_retry_only_after_work_has_started() -> None:
    assert not can_transition(S.QUEUED, S.RETRY_WAIT)
    assert not can_transition(S.CLAIMED, S.RETRY_WAIT)
    for state in (S.PROVISIONING, S.RUNNING_AGENT, S.VERIFYING, S.PUBLISHING):
        assert can_transition(state, S.RETRY_WAIT)


def test_verification_cannot_be_skipped() -> None:
    assert not can_transition(S.RUNNING_AGENT, S.PUBLISHING)
    assert not can_transition(S.RUNNING_AGENT, S.SUCCEEDED)
    assert not can_transition(S.VERIFYING, S.SUCCEEDED)


@pytest.mark.parametrize(
    ("source", "target"),
    [(s, t) for s, t in itertools.product(S, S) if not can_transition(s, t)],
)
def test_disallowed_transitions_raise(source: ExecutionState, target: ExecutionState) -> None:
    execution = Execution(
        id="e1",
        task=TASK,
        state=source,
        attempt=1,
        version=5,
        created_at=at(0),
        updated_at=at(1),
    )
    with pytest.raises(InvalidTransitionError):
        ExecutionStateMachine(max_attempts=3).advance(
            execution, target, now=at(2), reason="test", error="e"
        )


def test_failure_states_require_an_error(machine: ExecutionStateMachine) -> None:
    execution = walk(machine, started(machine), S.CLAIMED, S.PROVISIONING)
    for target in (S.FAILED, S.RETRY_WAIT):
        with pytest.raises(ValueError, match="error"):
            machine.advance(execution, target, now=at(10), reason="test")


def test_reason_is_required(machine: ExecutionStateMachine) -> None:
    with pytest.raises(ValueError, match="reason"):
        machine.advance(started(machine), S.CLAIMED, now=at(1), reason="  ")


def test_time_cannot_go_backwards(machine: ExecutionStateMachine) -> None:
    execution = walk(machine, started(machine), S.CLAIMED)
    with pytest.raises(ValueError, match="precede"):
        machine.advance(execution, S.PROVISIONING, now=at(0), reason="test")


def test_retry_increments_attempt(machine: ExecutionStateMachine) -> None:
    execution = walk(
        machine, started(machine), S.CLAIMED, S.PROVISIONING, S.RETRY_WAIT, S.PROVISIONING
    )
    assert execution.attempt == 2
    assert execution.last_error == "failure"


def test_retries_are_bounded(machine: ExecutionStateMachine) -> None:
    execution = walk(machine, started(machine), S.CLAIMED, S.PROVISIONING)
    for _ in range(2):
        execution = walk(machine, execution, S.RETRY_WAIT, S.PROVISIONING)
    assert execution.attempt == 3
    assert machine.retries_remaining(execution) == 0
    with pytest.raises(RetryLimitExceededError):
        machine.advance(execution, S.RETRY_WAIT, now=at(100), reason="test", error="again")
    assert walk(machine, execution, S.FAILED).state is S.FAILED


def test_lowered_limit_is_enforced_when_resuming_a_retry() -> None:
    generous = ExecutionStateMachine(max_attempts=5)
    execution = walk(generous, started(generous), S.CLAIMED, S.PROVISIONING, S.RETRY_WAIT)
    execution = walk(generous, execution, S.PROVISIONING, S.RETRY_WAIT)
    assert execution.attempt == 2
    strict = ExecutionStateMachine(max_attempts=2)
    with pytest.raises(RetryLimitExceededError):
        strict.advance(execution, S.PROVISIONING, now=at(100), reason="resume")


def test_single_attempt_means_no_retries() -> None:
    machine = ExecutionStateMachine(max_attempts=1)
    execution = walk(machine, started(machine), S.CLAIMED, S.PROVISIONING)
    with pytest.raises(RetryLimitExceededError):
        machine.advance(execution, S.RETRY_WAIT, now=at(10), reason="test", error="x")


def test_max_attempts_must_be_positive() -> None:
    with pytest.raises(ValueError, match="max_attempts"):
        ExecutionStateMachine(max_attempts=0)


def test_branch_and_change_request_are_recorded_and_kept(
    machine: ExecutionStateMachine,
) -> None:
    execution = walk(machine, started(machine), S.CLAIMED)
    execution, _ = machine.advance(
        execution, S.PROVISIONING, now=at(10), reason="test", branch="ilmar/task-1"
    )
    execution = walk(machine, execution, S.RUNNING_AGENT, S.VERIFYING)
    execution, _ = machine.advance(
        execution,
        S.PUBLISHING,
        now=at(20),
        reason="test",
        change_request_url="https://example.invalid/pr/1",
    )
    execution = walk(machine, execution, S.SUCCEEDED)
    assert execution.branch == "ilmar/task-1"
    assert execution.change_request_url == "https://example.invalid/pr/1"


def test_advance_does_not_mutate_input(machine: ExecutionStateMachine) -> None:
    execution = started(machine)
    machine.advance(execution, S.CLAIMED, now=at(1), reason="test")
    assert execution.state is S.QUEUED
    assert execution.version == 1


# -- candidate revisions (ADR-0010, #9) -----------------------------------------------


def test_happy_path_records_base_and_candidate(machine: ExecutionStateMachine) -> None:
    execution = walk(machine, started(machine), *HAPPY_PATH)
    assert (execution.base_revision, execution.candidate_revision) == (BASE, CANDIDATE)


def test_verifying_and_publishing_need_a_candidate(machine: ExecutionStateMachine) -> None:
    running = walk(machine, started(machine), S.CLAIMED, S.PROVISIONING, S.RUNNING_AGENT)
    with pytest.raises(ValueError, match="candidate revision"):
        machine.advance(running, S.VERIFYING, now=at(100), reason="test")


def test_running_agent_needs_the_base(machine: ExecutionStateMachine) -> None:
    provisioning = walk(machine, started(machine), S.CLAIMED, S.PROVISIONING)
    with pytest.raises(ValueError, match="base revision"):
        machine.advance(provisioning, S.RUNNING_AGENT, now=at(100), reason="test")


def test_revisions_are_set_once_and_only_at_their_step(machine: ExecutionStateMachine) -> None:
    provisioning = walk(machine, started(machine), S.CLAIMED, S.PROVISIONING)
    with pytest.raises(ValueError, match="candidate_revision is set once"):
        machine.advance(
            provisioning, S.RUNNING_AGENT, now=at(100), reason="t",
            base_revision=BASE, candidate_revision=CANDIDATE,
        )  # fmt: skip
    verifying = walk(machine, provisioning, S.RUNNING_AGENT, S.VERIFYING)
    with pytest.raises(ValueError, match="candidate_revision is set once"):
        machine.advance(
            verifying, S.PUBLISHING, now=at(100), reason="t", candidate_revision="c" * 40
        )
    with pytest.raises(ValueError, match="base_revision is set once"):
        machine.advance(verifying, S.PUBLISHING, now=at(100), reason="t", base_revision="c" * 40)


def test_a_new_attempt_clears_the_revisions(machine: ExecutionStateMachine) -> None:
    verifying = walk(
        machine, started(machine), S.CLAIMED, S.PROVISIONING, S.RUNNING_AGENT, S.VERIFYING
    )
    retried = walk(machine, verifying, S.RETRY_WAIT, S.PROVISIONING)
    assert retried.attempt == 2
    assert (retried.base_revision, retried.candidate_revision) == (None, None)
    with pytest.raises(ValueError, match="new attempt"):
        machine.advance(
            walk(machine, verifying, S.RETRY_WAIT), S.PROVISIONING, now=at(500), reason="t",
            base_revision=BASE,
        )  # fmt: skip


@pytest.mark.parametrize("bad", ["", "not hex!", "../x"])
def test_revision_ids_are_validated(machine: ExecutionStateMachine, bad: str) -> None:
    provisioning = walk(machine, started(machine), S.CLAIMED, S.PROVISIONING)
    with pytest.raises(ValueError, match="revision id"):
        machine.advance(provisioning, S.RUNNING_AGENT, now=at(100), reason="t", base_revision=bad)


def test_a_candidate_needs_a_base() -> None:
    from ilmar.domain.execution import Execution

    with pytest.raises(ValueError, match="base revision"):
        Execution(
            id="e", task=TASK, state=S.VERIFYING, attempt=1, version=1,
            created_at=at(0), updated_at=at(0), candidate_revision=CANDIDATE,
        )  # fmt: skip
