# SPDX-License-Identifier: Apache-2.0
"""The coordinator's event context (#17 part 3, ADR-0024 §2 layer 1, §4 and §5).

* durations are measured with the monotonic clock around the effect alone;
* a failed or ambiguous outcome carries its error class and stable code;
* a reconciliation carries what it observed and, if it failed, why;
* every failure path of the coordinator has a stable code, ownership conflicts
  included, and every coordinator event carries its pass.

The suite-wide fixture in ``tests/conftest.py`` additionally checks I11 and the
absence of ``unclassified``, ``invalid``, ``redacted`` and ``truncated`` after
every coordinator pass in every test.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from coordinator_support import Setup, make_setup
from ilmar.adapters.sqlite import SQLiteExecutionStore
from ilmar.core.coordinator import Coordinator
from ilmar.core.effects import DeterministicError, TransientError
from ilmar.core.event_context import CodedError, PassEvents, error_class, failure_code
from ilmar.core.journal import Journal
from ilmar.core.layout import WorkspaceLayout
from ilmar.core.ownership import ExecutionLease, ExecutionLeases, Ownership
from ilmar.core.state_machine import ExecutionStateMachine
from ilmar.core.worker import Outcome
from ilmar.domain.agent import AgentOutcome
from ilmar.domain.errors import (
    ConcurrencyConflictError,
    ContainmentError,
    LedgerIntegrityError,
    OwnershipConflictError,
    ProviderError,
)
from ilmar.domain.event_history import verify_lines
from ilmar.domain.events import Event, EventType
from ilmar.domain.execution import ExecutionState
from ilmar.domain.ledger import (
    Commit,
    Fence,
    Operation,
    OperationKind,
    OperationStatus,
    Resource,
    ResourceKind,
    ResourceStatus,
)
from ilmar.domain.tasks import ClaimOutcome, TaskRef
from ilmar.domain.workspace import ResourceOwner

from helpers import at

K, ST = OperationKind, OperationStatus
MACHINE = ExecutionStateMachine(max_attempts=3)


# -- the journal: durations, error classes, codes, observations --------------------------


@dataclass
class Monotonic:
    """A monotonic clock a test moves by hand."""

    now: float = 100.0
    readings: list[float] = field(default_factory=list)

    def __call__(self) -> float:
        self.readings.append(self.now)
        return self.now


@dataclass
class Harness:
    store: SQLiteExecutionStore
    ownership: Ownership
    journal: Journal
    monotonic: Monotonic
    leases: ExecutionLeases = field(default_factory=ExecutionLeases)
    held: list[ExecutionLease] = field(default_factory=list)

    def lease(self) -> ExecutionLease:
        if not self.held:
            self.held.append(self.leases.acquire("e1"))
        return self.held[0]

    def perform(
        self,
        kind: OperationKind,
        effect: Callable[[], Mapping[str, str]],
        *,
        definitive: tuple[type[BaseException], ...] = (),
        after: Callable[[Mapping[str, str]], Commit] | None = None,
    ) -> Mapping[str, str]:
        execution = self.store.get("e1")
        assert execution is not None
        return self.journal.perform(kind, execution, effect, lease=self.lease(),
                                    definitive=definitive, after=after)  # fmt: skip

    def last(self, kind: str) -> Event:
        return [e for e in self.store.events() if e.type.value == kind][-1]


@pytest.fixture
def harness(tmp_path: Path) -> Iterator[Harness]:
    state = tmp_path.resolve() / "state"
    state.mkdir()
    layout = WorkspaceLayout.under(state)
    store = SQLiteExecutionStore(tmp_path / "executions.sqlite3")
    ownership = Ownership.take(layout=layout, store=store, instance_id="inst", now=at(0))
    monotonic = Monotonic()
    events = PassEvents(clock=lambda: at(10), new_id=lambda: "p-0a0b0c")
    journal = Journal(store=store, ownership=ownership, clock=lambda: at(10), events=events,
                      monotonic=monotonic)  # fmt: skip
    execution, transition = MACHINE.start(TaskRef("fake", "1"), execution_id="e1", now=at(1))
    store.commit(ownership.fence, Commit(created=((execution, transition),)),
                 context=events.context(ownership.fence))  # fmt: skip
    yield Harness(store, ownership, journal, monotonic)
    ownership.release()
    store.close()


def test_a_duration_is_measured_around_the_effect_alone(harness: Harness) -> None:
    def effect() -> Mapping[str, str]:
        harness.monotonic.now += 0.412  # the effect takes 412 ms
        return {"head": "x"}

    harness.monotonic.now = 50.0
    harness.perform(K.PUBLISH_REVISION, effect)
    outcome = harness.last("operation.succeeded")
    assert outcome.data["duration_ms"] == 412
    assert "duration_ms" not in harness.last("operation.intended").data
    assert outcome.pass_id == "p-0a0b0c"


@pytest.mark.parametrize(
    ("error", "definitive", "kind", "code", "error_class"),
    [
        (OwnershipConflictError("x"), True, "operation.failed", "resource-ownership-conflict",
         "OwnershipConflictError"),
        (ProviderError("x"), True, "operation.failed", "transient-provider-error", "ProviderError"),
        (ProviderError("x"), False, "operation.ambiguous", "transient-provider-error",
         "ProviderError"),
        (TimeoutError("x"), False, "operation.ambiguous", "transient-provider-error",
         "TimeoutError"),
        (KeyError("x"), False, "operation.ambiguous", "unexpected-error", "KeyError"),
    ],
)  # fmt: skip
def test_a_failed_outcome_carries_its_class_code_and_duration(
    harness: Harness, error: Exception, definitive: bool, kind: str, code: str, error_class: str
) -> None:
    def effect() -> Mapping[str, str]:
        harness.monotonic.now += 2.0
        raise error

    with pytest.raises(type(error)):
        harness.perform(K.PUBLISH_REVISION, effect,
                        definitive=(type(error),) if definitive else ())  # fmt: skip
    outcome = harness.last(kind)
    assert outcome.reason == code
    assert outcome.data["error_class"] == error_class
    assert outcome.data["duration_ms"] == 2000
    assert not outcome.flags


def test_a_refused_follow_up_keeps_the_result_with_its_code(harness: Harness) -> None:
    def follow_up(result: Mapping[str, str]) -> Commit:
        raise ConcurrencyConflictError("changed")

    with pytest.raises(LedgerIntegrityError):
        harness.perform(K.PUBLISH_REVISION, lambda: {"head": "x"}, after=follow_up)
    kept = harness.last("operation.ambiguous")
    assert (kept.reason, kept.data["error_class"]) == ("ledger-integrity",
                                                        "ConcurrencyConflictError")  # fmt: skip


def test_an_error_class_outside_the_schema_is_left_out_never_written_invalid() -> None:
    class Crash(BaseException):
        pass

    assert error_class(Crash()) is None
    assert error_class(OwnershipConflictError("x")) == "OwnershipConflictError"
    assert failure_code(Crash()) == "unexpected-error"


def test_a_reconciliation_records_what_it_observed_and_why(harness: Harness) -> None:
    def interrupted() -> Mapping[str, str]:
        raise ProviderError("lost")

    with pytest.raises(ProviderError):
        harness.perform(K.PUBLISH_REVISION, interrupted)
    harness.journal.reconcile("publish-revision:e1:1", ST.FAILED, reason="did not move",
                              observed="branch-unmoved", code="interrupted",
                              lease=harness.lease())  # fmt: skip
    settled = harness.last("operation.reconciled")
    assert (settled.reason, settled.data["observed"], settled.data["status"],
            settled.data["from"]) == ("interrupted", "branch-unmoved", "failed",
                                      "ambiguous")  # fmt: skip


def test_an_intended_operation_is_first_marked_interrupted(harness: Harness) -> None:
    fence = harness.ownership.fence
    claim = Operation.intended(K.CLAIM_TASK, "e1", 1, fence=fence, now=at(10))
    harness.store.commit(fence, Commit(operations=(claim,)),
                         context=harness.journal.events.context(fence))  # fmt: skip
    harness.journal.reconcile(claim.key, ST.SUCCEEDED, reason="claimed",
                              observed="outcome-unknown",
                              lease=harness.lease())  # fmt: skip
    ambiguous = harness.last("operation.ambiguous")
    settled = harness.last("operation.reconciled")
    assert ambiguous.reason == "interrupted"
    assert settled.reason is None
    assert settled.data["observed"] == "outcome-unknown"


@pytest.mark.parametrize(
    ("status", "observed", "code", "message"),
    [
        (ST.FAILED, "outcome-unknown", None, "needs a reason code"),
        (ST.FAILED, "outcome-unknown", "unclassified-ish", "not a reason code"),
        (ST.FAILED, "outcome-unknown", "unclassified", "not a reason code"),
        (ST.SUCCEEDED, "looked fine", None, "not a registered observation"),
    ],
)
def test_a_reconciliation_must_say_what_it_saw_and_why_it_failed(
    harness: Harness, status: OperationStatus, observed: str, code: str | None, message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        harness.journal.reconcile("claim-task:e1:1", status, reason="x", observed=observed,
                                  code=code, lease=harness.lease())  # fmt: skip


# -- the coordinator: every failure path has its code ------------------------------------


@pytest.fixture
def setup(tmp_path: Path) -> Setup:
    return make_setup(tmp_path)


def events_of(setup: Setup) -> list[Event]:
    return list(setup.store.events())


def transition_to(setup: Setup, state: str) -> Event:
    (event,) = [e for e in events_of(setup) if e.type is EventType.EXECUTION_TRANSITIONED
                and e.data["to"] == state]  # fmt: skip
    return event


def test_every_event_of_a_pass_carries_that_pass(setup: Setup) -> None:
    passes = iter(["p-000001", "p-000002", "p-000003"])
    coordinator = setup.coordinator()
    coordinator._events._new_id = lambda: next(passes)
    coordinator.run_once()
    first = len(events_of(setup))
    coordinator.run_once()
    covered = [e for e in events_of(setup) if e.actor.kind.value == "coordinator"
               and e.type is not EventType.OWNERSHIP_ACQUIRED]  # fmt: skip
    assert {e.pass_id for e in covered[: first - 2]} == {"p-000001"}
    assert {e.pass_id for e in covered[first - 2 :]} <= {"p-000002"}


@pytest.mark.parametrize(
    ("outcome", "code"),
    [(ClaimOutcome.CLAIMED_BY_OTHER, "not-claimed-other"),
     (ClaimOutcome.NOT_ELIGIBLE, "not-claimed-ineligible")],
)  # fmt: skip
def test_a_claim_that_did_not_succeed_cancels_with_its_code(
    setup: Setup, monkeypatch: pytest.MonkeyPatch, outcome: ClaimOutcome, code: str
) -> None:
    monkeypatch.setattr(setup.tasks, "claim", lambda ref: outcome)
    setup.coordinator().run_once()
    assert transition_to(setup, "cancelled").reason == code


def subdirectory(root: Path, name: str) -> Path:
    (root / name).mkdir()
    return root / name


def test_an_agent_failure_and_a_timeout_have_their_codes(tmp_path: Path) -> None:
    failing = make_setup(subdirectory(tmp_path, "failing"))
    failing.agent.plan = [(AgentOutcome.FAILED, {})]
    failing.coordinator().run_once()
    assert transition_to(failing, "failed").reason == "agent-failed"

    timing_out = make_setup(subdirectory(tmp_path, "timing-out"))
    timing_out.agent.plan = [(AgentOutcome.TIMED_OUT, {})] * 3
    timing_out.coordinator().run_once()
    retries = [e for e in events_of(timing_out) if e.type is EventType.EXECUTION_TRANSITIONED
               and e.data["to"] == "retry_wait"]  # fmt: skip
    assert retries
    assert {e.reason for e in retries} == {"agent-timed-out"}
    assert transition_to(timing_out, "failed").reason == "attempts-exhausted"


def test_an_agent_that_changed_nothing_fails_with_no_changes(setup: Setup) -> None:
    setup.agent.plan = [(AgentOutcome.COMPLETED, {})]
    setup.coordinator().run_once()
    assert transition_to(setup, "failed").reason == "no-changes"


def test_failed_checks_fail_with_verification_failed(setup: Setup) -> None:
    setup.agent.plan = [(AgentOutcome.COMPLETED, {"src/app.py": "print('still broken')\n"})]
    setup.coordinator().run_once()
    assert transition_to(setup, "failed").reason == "verification-failed"


def test_a_checkout_that_cannot_succeed_fails_with_checkout_failed(
    setup: Setup, monkeypatch: pytest.MonkeyPatch
) -> None:
    def refuse(*args: object, **kwargs: object) -> str:
        raise ProviderError("unknown base branch")

    monkeypatch.setattr(setup.scm, "checkout", refuse)
    setup.coordinator().run_once()
    assert transition_to(setup, "failed").reason == "checkout-failed"
    (failed,) = [e for e in events_of(setup) if e.type is EventType.OPERATION_FAILED]
    assert failed.data["kind"] == "checkout-base"


# -- ownership conflicts: one stable code, wherever they occur (ADR-0024 §4) -------------


def test_an_ownership_conflict_on_start_is_resource_ownership_conflict(
    setup: Setup, monkeypatch: pytest.MonkeyPatch
) -> None:
    def foreign(spec: object) -> object:
        raise OwnershipConflictError("a container with this name belongs to someone else")

    monkeypatch.setattr(setup.runtime, "start", foreign)
    setup.coordinator().run_once()
    (failed,) = [e for e in events_of(setup) if e.type is EventType.OPERATION_FAILED]
    assert (failed.data["kind"], failed.reason) == ("start-container",
                                                    "resource-ownership-conflict")  # fmt: skip
    assert transition_to(setup, "failed").reason == "resource-ownership-conflict"


def test_an_ownership_conflict_on_removal_quarantines_with_its_code(
    setup: Setup, monkeypatch: pytest.MonkeyPatch
) -> None:
    def foreign(name: str, *, owner: ResourceOwner) -> bool:
        raise OwnershipConflictError("the sandbox carries another owner's labels")

    monkeypatch.setattr(setup.runtime, "remove_owned", foreign)
    report = setup.coordinator().run_once()
    assert report.results[0].outcome is Outcome.BLOCKED
    (quarantined,) = [e for e in events_of(setup) if e.type is EventType.RESOURCE_QUARANTINED]
    assert quarantined.reason == "resource-ownership-conflict"
    # An ownership conflict is always definitive: the removal is recorded as failed.
    (failed,) = [e for e in events_of(setup) if e.type is EventType.OPERATION_FAILED]
    assert (failed.data["kind"], failed.reason) == ("stop-container",
                                                    "resource-ownership-conflict")  # fmt: skip
    assert transition_to(setup, "failed").reason == "resource-ownership-conflict"


def test_an_existing_attempt_directory_is_resource_ownership_conflict(setup: Setup) -> None:
    setup.layout.attempt_dir("e1", 1).mkdir(parents=True)  # planted before Ilmar creates it
    setup.coordinator().run_once()
    (failed,) = [e for e in events_of(setup) if e.type is EventType.OPERATION_FAILED]
    assert (failed.data["kind"], failed.reason) == ("create-workspace",
                                                    "resource-ownership-conflict")  # fmt: skip
    assert transition_to(setup, "failed").reason == "resource-ownership-conflict"


def test_an_unconfirmed_removal_is_containment_unconfirmed(
    setup: Setup, monkeypatch: pytest.MonkeyPatch
) -> None:
    def unconfirmed(name: str, *, owner: ResourceOwner) -> bool:
        raise ContainmentError("the network is still there", container_id="c", name=name)

    monkeypatch.setattr(setup.runtime, "remove_owned", unconfirmed)
    setup.coordinator().run_once()
    (quarantined,) = [e for e in events_of(setup) if e.type is EventType.RESOURCE_QUARANTINED]
    assert quarantined.reason == "containment-unconfirmed"


def test_a_clean_run_has_no_failure_codes_at_all(setup: Setup) -> None:
    report = setup.coordinator().run_once()
    assert report.code == 0
    assert {e.reason for e in events_of(setup)} == {None}
    assert all(not e.flags for e in events_of(setup))


# -- the no-unclassified invariant at the API boundary ------------------------------------


def snapshot(harness: Harness) -> tuple[object, ...]:
    """Everything a refused write must leave as it was."""
    store = harness.store
    return (store.events(), store.get("e1"), store.history("e1"), store.operations("e1"),
            store.resources())  # fmt: skip


def check_i11(harness: Harness) -> None:
    lines, stored = harness.store.history_snapshot()
    assert verify_lines(lines, stored).failures == ()


def failed_transition(harness: Harness, code: str | None) -> Commit:
    execution = harness.store.get("e1")
    assert execution is not None
    updated, step = MACHINE.advance(execution, ExecutionState.FAILED, now=at(11), reason="r",
                                    error="gave up")  # fmt: skip
    return Commit(applied=((updated, step),), reasons={"e1": code} if code else {})


@pytest.mark.parametrize("code", ["unclassified", "not-a-code", "", "RESOURCE-OWNERSHIP-CONFLICT"])
def test_reconcile_refuses_an_invalid_code_and_writes_nothing(harness: Harness, code: str) -> None:
    def interrupted() -> Mapping[str, str]:
        raise ProviderError("lost")

    with pytest.raises(ProviderError):
        harness.perform(K.PUBLISH_REVISION, interrupted)
    before = snapshot(harness)
    for status in (ST.FAILED, ST.SUCCEEDED):
        with pytest.raises(ValueError, match="not a reason code"):
            harness.journal.reconcile("publish-revision:e1:1", status, reason="x",
                                      observed="branch-unmoved", code=code,
                                      lease=harness.lease())  # fmt: skip
    assert snapshot(harness) == before
    check_i11(harness)


@pytest.mark.parametrize("code", ["unclassified", "not-a-code", ""])
def test_explicit_code_inputs_refuse_unclassified_and_unknown_codes(code: str) -> None:
    for error in (CodedError, TransientError, DeterministicError):
        with pytest.raises(ValueError, match="not a reason code"):
            error("x", code=code)
    events = PassEvents(clock=lambda: at(1))
    with pytest.raises(ValueError, match="not a reason code"):
        events.context(Fence("inst", 1), reasons={"e1": code})


@pytest.mark.parametrize("code", [None, "unclassified", "not-a-code"])
def test_a_failure_without_a_valid_code_is_refused_and_rolled_back(
    harness: Harness, code: str | None
) -> None:
    before = snapshot(harness)
    with pytest.raises(LedgerIntegrityError, match="nothing is written"):
        harness.journal.record(failed_transition(harness, code), lease=harness.lease(),
                               execution_id="e1")  # fmt: skip
    assert snapshot(harness) == before
    stored = harness.store.get("e1")
    assert stored is not None
    assert stored.state is ExecutionState.QUEUED  # never silently failed
    check_i11(harness)


def test_a_quarantine_without_a_code_is_refused(harness: Harness) -> None:
    fence = harness.ownership.fence
    box = Resource.intended(ResourceKind.CONTAINER, "e1", 1, "c1", owner_token="t", now=at(10))
    harness.journal.record(Commit(resources=(box,)), lease=harness.lease(), execution_id="e1")
    before = snapshot(harness)
    quarantined = box.moved(ResourceStatus.QUARANTINED, now=at(11))
    with pytest.raises(LedgerIntegrityError, match="no reason code"):
        harness.journal.record(Commit(resources=(quarantined,)), lease=harness.lease(),
                               execution_id="e1")  # fmt: skip
    assert snapshot(harness) == before
    assert harness.store.resource(box.key) == box
    del fence


def test_a_follow_up_without_a_code_never_commits_as_a_success(harness: Harness) -> None:
    """The effect happened, but its follow-up fails the execution without a code: the
    outcome is kept as ambiguous with its result, the transition is not written."""
    with pytest.raises(LedgerIntegrityError, match="follow-up changes were refused"):
        harness.perform(K.PUBLISH_REVISION, lambda: {"head": "x"},
                        after=lambda _r: failed_transition(harness, None))  # fmt: skip
    (operation,) = harness.store.operations("e1")
    assert operation.status is ST.AMBIGUOUS
    assert operation.result == {"head": "x"}
    assert harness.last("operation.ambiguous").reason == "ledger-integrity"
    stored = harness.store.get("e1")
    assert stored is not None
    assert stored.state is ExecutionState.QUEUED
    assert "execution.transitioned" not in [e.type.value for e in harness.store.events()]
    assert all(e.reason != "unclassified" for e in harness.store.events())
    check_i11(harness)


def test_a_worker_ending_without_its_code_blocks_instead_of_failing(
    setup: Setup, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A bug that drops a code is a refused write and a blocked execution, never a
    failed execution with an ``unclassified`` event."""
    from ilmar.core import worker

    real = worker._Run.transition_to

    def without_code(self: object, target: ExecutionState, reason: str, *, code: str | None = None,
                     **values: str) -> Commit:  # fmt: skip
        return real(self, target, reason, **values)  # type: ignore[arg-type]

    monkeypatch.setattr(worker._Run, "transition_to", without_code)
    setup.agent.plan = [(AgentOutcome.FAILED, {})]
    report = setup.coordinator().run_once()
    (result,) = report.results
    assert result.outcome is Outcome.BLOCKED
    assert "no reason code" in result.detail
    execution = setup.store.get("e1")
    assert execution is not None
    assert execution.state is ExecutionState.RUNNING_AGENT  # not silently failed
    assert all(e.reason != "unclassified" for e in events_of(setup))


# -- #17 completion: the change-request observation (ADR-0024 §1) -------------------------


def published(setup: Setup) -> Coordinator:
    coordinator = setup.coordinator()
    assert coordinator.run_once().results[0].outcome is Outcome.PUBLISHED
    return coordinator


def finish_request(setup: Setup, *, merged: bool) -> None:
    ((_, record),) = setup.scm._change_requests.items()
    setup.scm.finish_change_request(record.request, merged=merged)


def release_events(setup: Setup, step: str) -> list[Event]:
    return [e for e in events_of(setup) if e.operation == f"release-task:e1:1:{step}"]


def request_events(setup: Setup) -> list[Event]:
    return [e for e in events_of(setup) if e.type.value.startswith("resource.")
            and e.data.get("kind") == "change-request"]  # fmt: skip


@pytest.mark.parametrize(
    ("merged", "step", "observed"),
    [(True, "merged", "change-request-merged"), (False, "closed", "change-request-closed")],
)
def test_the_change_request_state_is_recorded_with_the_release_it_decides(
    setup: Setup, merged: bool, step: str, observed: str
) -> None:
    coordinator = published(setup)
    finish_request(setup, merged=merged)
    coordinator.run_once()
    intended, succeeded = release_events(setup, step)
    assert (intended.type, succeeded.type) == (EventType.OPERATION_INTENDED,
                                               EventType.OPERATION_SUCCEEDED)  # fmt: skip
    assert intended.data["observed"] == succeeded.data["observed"] == observed
    # Same commits as the resource changes it decides: releasing with the intent,
    # released with the outcome, in adjacent seqs and nothing between them.
    releasing, released = request_events(setup)[-2:]
    assert (releasing.data["status"], released.data["status"]) == ("releasing", "released")

    def adjacent(first: Event, second: Event) -> bool:
        assert first.seq is not None
        assert second.seq is not None
        return abs(first.seq - second.seq) == 1

    assert adjacent(intended, releasing)
    assert adjacent(succeeded, released)
    assert intended.pass_id == releasing.pass_id


class FailingCommit:
    """The store's commit, failing the first commit that writes the release operation
    with ``status``, as a full disk would: the store writes nothing of it."""

    def __init__(self, commit: Callable[..., None], status: OperationStatus) -> None:
        self.real, self.status, self.failed = commit, status, False

    def __call__(self, fence: Fence, changes: Commit, **kwargs: Any) -> None:
        if not self.failed and any(o.kind is K.RELEASE_TASK and o.status is self.status
                                   for o in changes.operations):  # fmt: skip
            self.failed = True
            raise LedgerIntegrityError("the disk is full")
        self.real(fence, changes, **kwargs)


@pytest.mark.parametrize("status", [OperationStatus.INTENDED, OperationStatus.SUCCEEDED])
def test_the_observation_and_its_changes_are_written_together_or_not_at_all(
    setup: Setup, monkeypatch: pytest.MonkeyPatch, status: OperationStatus
) -> None:
    coordinator = published(setup)
    finish_request(setup, merged=True)
    failing = FailingCommit(setup.store.commit, status)
    monkeypatch.setattr(setup.store, "commit", failing)
    before = len(events_of(setup))
    coordinator.run_once()
    assert failing.failed
    (request,) = [r for r in setup.store.resources() if r.kind.value == "change-request"]
    if status is OperationStatus.INTENDED:
        # Refused together: no intent, no observation, and the resource unchanged.
        assert release_events(setup, "merged") == []
        assert request.status.value == "active"
        assert len(events_of(setup)) == before or all(
            e.operation != "release-task:e1:1:merged" for e in events_of(setup)[before:]
        )
    else:
        # The release happened but its outcome was refused: kept as ambiguous with
        # the observation, and the resource still releasing, never released alone.
        assert [e.type.value for e in release_events(setup, "merged")] == [
            "operation.intended", "operation.ambiguous"]  # fmt: skip
        assert {e.data["observed"] for e in release_events(setup, "merged")} == {
            "change-request-merged"}  # fmt: skip
        assert request.status.value == "releasing"
    monkeypatch.undo()
    setup.coordinator().run_once()  # the next pass reads the state again and finishes
    final = release_events(setup, "merged")
    assert final[-1].type is EventType.OPERATION_SUCCEEDED
    assert {e.data["observed"] for e in final} == {"change-request-merged"}
    assert [r.status.value for r in setup.store.resources()
            if r.kind.value == "change-request"] == ["released"]  # fmt: skip


def test_an_interrupted_release_keeps_its_observation_when_repeated(
    setup: Setup, monkeypatch: pytest.MonkeyPatch
) -> None:
    coordinator = published(setup)
    finish_request(setup, merged=False)

    def lost(ref: object) -> None:
        raise ProviderError("the connection dropped")

    monkeypatch.setattr(setup.tasks, "release", lost)
    coordinator.run_once()
    monkeypatch.undo()
    setup.coordinator().run_once()
    kinds = [(e.type.value, e.data.get("observed")) for e in release_events(setup, "closed")]
    assert kinds == [
        ("operation.intended", "change-request-closed"),
        ("operation.ambiguous", "change-request-closed"),
        ("operation.intended", "change-request-closed"),  # repeated: safe for a release
        ("operation.succeeded", "change-request-closed"),
    ]


def test_an_unregistered_observation_is_refused_before_anything_is_written(
    setup: Setup,
) -> None:
    coordinator = published(setup)
    finish_request(setup, merged=True)
    execution = setup.store.get("e1")
    assert execution is not None
    (request,) = [r for r in setup.store.resources() if r.kind.value == "change-request"]
    before = events_of(setup)
    with coordinator.leases.acquire("e1") as lease, pytest.raises(ValueError, match="observation"):
        coordinator._effects.stop_tracking_change_request(
            execution, lease, resource=request, note="x", step="merged", observed="it-looked-done"
        )
    assert events_of(setup) == before


# -- #17 completion: stable codes in the `ilmar run --once` output --------------------------


@pytest.mark.parametrize(
    ("arrange", "line"),
    [
        ("agent-failed", "e1: failed [agent-failed] the agent failed: "),
        ("not-claimed", "e1: cancelled [not-claimed-other]"),
        ("ownership", "e1: blocked [resource-ownership-conflict] quarantined resources remain"),
        ("published", "e1: published https://"),
    ],
)
def test_the_run_summary_shows_each_code_beside_the_readable_detail(
    setup: Setup, monkeypatch: pytest.MonkeyPatch, arrange: str, line: str
) -> None:
    if arrange == "agent-failed":
        setup.agent.plan = [(AgentOutcome.FAILED, {})]
    elif arrange == "not-claimed":
        monkeypatch.setattr(setup.tasks, "claim", lambda ref: ClaimOutcome.CLAIMED_BY_OTHER)
    elif arrange == "ownership":

        def foreign(name: str, *, owner: ResourceOwner) -> bool:
            raise OwnershipConflictError("the sandbox carries another owner's labels")

        monkeypatch.setattr(setup.runtime, "remove_owned", foreign)
    report = setup.coordinator().run_once()
    assert report.summary.startswith(line), report.summary
    if arrange == "published":
        assert "[" not in report.summary


def test_an_unexpected_error_is_reported_with_its_code(
    setup: Setup, monkeypatch: pytest.MonkeyPatch
) -> None:
    from ilmar.core import worker

    def bug(self: object) -> object:
        raise KeyError("a bug")

    monkeypatch.setattr(worker._Run, "drive", bug)
    report = setup.coordinator().run_once()
    assert report.summary.startswith("e1: blocked [unexpected-error] unexpected KeyError")
