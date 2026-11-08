# SPDX-License-Identifier: Apache-2.0
"""Contract every ``ExecutionStore`` adapter must satisfy.

Subclass :class:`ExecutionStoreContract` in a ``test_*.py`` module and
implement :meth:`make_store`.
"""

from __future__ import annotations

import dataclasses
import threading
from collections.abc import Callable, Iterator, Sequence
from datetime import timedelta, timezone
from pathlib import Path

import pytest

from ilmar.core.state_machine import ExecutionStateMachine
from ilmar.domain.errors import (
    ConcurrencyConflictError,
    DuplicateExecutionError,
    ExecutionNotFoundError,
    FencingError,
    LedgerIntegrityError,
)
from ilmar.domain.event_history import StoredState, verify
from ilmar.domain.events import Event, EventContext, EventType, derive_events
from ilmar.domain.execution import Execution, ExecutionState
from ilmar.domain.ledger import (
    Commit,
    Fence,
    Operation,
    OperationKind,
    OperationStatus,
    Resource,
    ResourceKind,
    ResourceStatus,
    changed_paths_digest,
)
from ilmar.domain.tasks import TaskRef
from ilmar.domain.verification import (
    AgentReport,
    CheckClass,
    CheckEvidence,
    EvidenceRecord,
    Independence,
    Verdict,
)
from ilmar.ports import event_log
from ilmar.ports.execution_store import ExecutionStore

from helpers import at

S = ExecutionState
MACHINE = ExecutionStateMachine(max_attempts=3)
TASK_A = TaskRef("fake", "A")
TASK_B = TaskRef("fake", "B")
BASE, CANDIDATE = "a" * 40, "b" * 40


def evidence_record(execution_id: str, attempt: int = 1, seconds: float = 10) -> EvidenceRecord:
    return EvidenceRecord(
        execution_id=execution_id,
        attempt=attempt,
        recorded_at=at(seconds),
        base_revision=BASE,
        candidate_revision=CANDIDATE,
        policy_digest="sha256:" + "0" * 64,
        checks=(
            CheckEvidence("ci-config-paths", CheckClass.INDEPENDENT, True),
            CheckEvidence(
                "tests", CheckClass.CANDIDATE_EXECUTED, False, command=("pytest",), exit_code=1
            ),
        ),
        protected_paths=(),
        check_influencing_paths=(),
        ci_config_paths=(),
        independence=Independence.UNCHANGED_CHECKS,
        verdict=Verdict.REJECTED_CHECKS_FAILED,
        requires_human_review=False,
        review_reasons=(),
        agent_report=AgentReport("completed", "says it is done ünïcödé"),
    )


def _create(store: ExecutionStore, execution_id: str, task: TaskRef, seconds: float) -> Execution:
    execution, transition = MACHINE.start(task, execution_id=execution_id, now=at(seconds))
    store.create(execution, transition)
    return execution


def _advance(
    store: ExecutionStore,
    execution: Execution,
    target: ExecutionState,
    seconds: float,
    **kwargs: str,
) -> Execution:
    if target is S.RUNNING_AGENT:
        kwargs.setdefault("base_revision", BASE)
    if target is S.VERIFYING:
        kwargs.setdefault("candidate_revision", CANDIDATE)
    reason = kwargs.pop("reason", f"to {target.value}")
    updated, transition = MACHINE.advance(
        execution, target, now=at(seconds), reason=reason, **kwargs
    )
    store.apply(updated, transition)
    return updated


# -- fencing, journal and ledger (ADR-0020) ----------------------------------------------

K = OperationKind
ST = OperationStatus
R = ResourceStatus


def claimed(store: ExecutionStore) -> Fence:
    return store.claim_instance("inst", now=at(0))


def fenced_execution(store: ExecutionStore, fence: Fence, execution_id: str = "e1") -> Execution:
    execution, transition = MACHINE.start(TASK_A, execution_id=execution_id, now=at(1))
    store.create(execution, transition, fence=fence)
    return execution


def intent(
    kind: OperationKind, fence: Fence, execution_id: str = "e1", step: str = ""
) -> Operation:
    return Operation.intended(
        kind, execution_id, 1, fence=fence, now=at(2), step=step, intent={"what": "x"}
    )


def freeze_op(fence: Fence, *, revision: str = CANDIDATE) -> tuple[Operation, Operation]:
    first = intent(K.FREEZE_CANDIDATE, fence)
    done = first.moved(
        ST.SUCCEEDED,
        fence=fence,
        now=at(3),
        result={
            "revision": revision,
            "base_revision": BASE,
            "changed_paths_digest": changed_paths_digest(["src/a.py"]),
            "changed_path_count": "1",
        },
    )
    return first, done


def to_running_agent(store: ExecutionStore, fence: Fence, execution: Execution) -> Execution:
    for target, seconds in ((S.CLAIMED, 4), (S.PROVISIONING, 5), (S.RUNNING_AGENT, 6)):
        updated, transition = MACHINE.advance(
            execution,
            target,
            now=at(seconds),
            reason="t",
            base_revision=BASE if target is S.RUNNING_AGENT else None,
        )
        store.apply(updated, transition, fence=fence)
        execution = updated
    return execution


class FencingContract:
    """Mixed into :class:`ExecutionStoreContract`; see ADR-0020."""

    def test_claiming_gives_increasing_epochs(self, store: ExecutionStore) -> None:
        assert store.fence() is None
        first = claimed(store)
        second = claimed(store)
        assert (first.epoch, second.epoch) == (1, 2)
        assert store.fence() == second

    def test_another_instance_cannot_claim_the_store(self, store: ExecutionStore) -> None:
        claimed(store)
        with pytest.raises(FencingError, match="belongs to instance"):
            store.claim_instance("other", now=at(1))

    def test_once_claimed_unfenced_writes_are_refused(self, store: ExecutionStore) -> None:
        fence = claimed(store)
        execution, transition = MACHINE.start(TASK_A, execution_id="e1", now=at(1))
        with pytest.raises(FencingError):
            store.create(execution, transition)
        store.create(execution, transition, fence=fence)
        updated, step = MACHINE.advance(execution, S.CLAIMED, now=at(2), reason="t")
        with pytest.raises(FencingError):
            store.apply(updated, step)
        with pytest.raises(FencingError):
            store.append_evidence(evidence_record("e1"))
        store.apply(updated, step, fence=fence)

    def test_a_superseded_fence_writes_nothing(self, store: ExecutionStore) -> None:
        old = claimed(store)
        execution = fenced_execution(store, old)
        claimed(store)  # another coordinator took over
        updated, step = MACHINE.advance(execution, S.CLAIMED, now=at(2), reason="t")
        writes: tuple[Callable[[], None], ...] = (
            lambda: store.apply(updated, step, fence=old),
            lambda: store.commit(old, Commit(operations=(intent(K.CLAIM_TASK, old),))),
            lambda: store.append_evidence(evidence_record("e1"), fence=old),
        )
        for write in writes:
            with pytest.raises(FencingError):
                write()
        assert store.get("e1") == execution
        assert store.operations("e1") == ()
        assert store.evidence("e1") == ()

    def test_a_fence_on_an_unclaimed_store_is_refused(self, store: ExecutionStore) -> None:
        with pytest.raises(FencingError):
            store.commit(Fence("inst", 1), Commit())

    def test_a_commit_is_all_or_nothing(self, store: ExecutionStore) -> None:
        fence = claimed(store)
        execution, transition = MACHINE.start(TASK_A, execution_id="e1", now=at(1))
        bad = Resource.intended(
            ResourceKind.WORKSPACE, "e1", 2, "/w", owner_token="tok", now=at(1)
        )  # attempt 2 does not exist yet
        with pytest.raises(LedgerIntegrityError):
            store.commit(
                fence,
                Commit(
                    created=((execution, transition),),
                    operations=(intent(K.CLAIM_TASK, fence),),
                    resources=(bad,),
                ),
            )
        assert store.get("e1") is None
        assert store.operation(intent(K.CLAIM_TASK, fence).key) is None
        assert store.resources() == ()

    def test_operations_round_trip_and_follow_their_status_rules(
        self, store: ExecutionStore
    ) -> None:
        fence = claimed(store)
        fenced_execution(store, fence)
        first = intent(K.START_CONTAINER, fence, step="ilmar-inst-e1-1")
        store.commit(fence, Commit(operations=(first,)))
        assert store.operation(first.key) == first
        assert store.unfinished_operations() == (first,)
        done = first.moved(ST.SUCCEEDED, fence=fence, now=at(3), result={"id": "c1"})
        store.commit(fence, Commit(operations=(done,)))
        assert store.operation(first.key) == done
        assert store.operations("e1") == (done,)
        assert store.unfinished_operations() == ()
        again = dataclasses.replace(done, status=ST.AMBIGUOUS, version=3)
        with pytest.raises(LedgerIntegrityError, match="cannot move"):
            store.commit(fence, Commit(operations=(again,)))

    def test_operation_writes_are_versioned(self, store: ExecutionStore) -> None:
        fence = claimed(store)
        fenced_execution(store, fence)
        first = intent(K.CLAIM_TASK, fence)
        store.commit(fence, Commit(operations=(first,)))
        with pytest.raises(ConcurrencyConflictError):
            store.commit(fence, Commit(operations=(first,)))  # inserting twice
        done = first.moved(ST.SUCCEEDED, fence=fence, now=at(3))
        store.commit(fence, Commit(operations=(done,)))
        stale = first.moved(ST.FAILED, fence=fence, now=at(4))  # version 2 again
        with pytest.raises(ConcurrencyConflictError):
            store.commit(fence, Commit(operations=(stale,)))

    def test_operations_must_start_intended_with_the_current_epoch(
        self, store: ExecutionStore
    ) -> None:
        fence = claimed(store)
        fenced_execution(store, fence)
        with pytest.raises(LedgerIntegrityError, match="epoch"):
            store.commit(fence, Commit(operations=(intent(K.CLAIM_TASK, Fence("inst", 9)),)))
        skipped = dataclasses.replace(intent(K.CLAIM_TASK, fence), status=ST.SUCCEEDED)
        with pytest.raises(LedgerIntegrityError, match="must start as intended"):
            store.commit(fence, Commit(operations=(skipped,)))

    def test_intent_and_identity_are_immutable(self, store: ExecutionStore) -> None:
        fence = claimed(store)
        fenced_execution(store, fence)
        first = intent(K.PUBLISH_REVISION, fence)
        store.commit(fence, Commit(operations=(first,)))
        changed = dataclasses.replace(first, intent={"what": "else"}, version=2, status=ST.FAILED)
        with pytest.raises(LedgerIntegrityError, match="immutable"):
            store.commit(fence, Commit(operations=(changed,)))

    def test_operations_belong_to_the_current_attempt(self, store: ExecutionStore) -> None:
        fence = claimed(store)
        fenced_execution(store, fence)
        later = Operation.intended(K.CLAIM_TASK, "e1", 2, fence=fence, now=at(2))
        with pytest.raises(LedgerIntegrityError, match="attempt"):
            store.commit(fence, Commit(operations=(later,)))

    def test_resources_follow_their_lifecycle(self, store: ExecutionStore) -> None:
        fence = claimed(store)
        fenced_execution(store, fence)
        planned = Resource.intended(
            ResourceKind.CONTAINER, "e1", 1, "ilmar-inst-e1-1", owner_token="tok", now=at(2)
        )
        store.commit(fence, Commit(resources=(planned,)))
        active = planned.moved(R.ACTIVE, now=at(3), identity="c0ffee")
        store.commit(fence, Commit(resources=(active,)))
        assert store.resources(statuses={R.ACTIVE}) == (active,)
        assert store.resources(execution_id="other") == ()
        swapped = dataclasses.replace(active, identity="other", version=3, status=R.RELEASING)
        with pytest.raises(LedgerIntegrityError, match="identity"):
            store.commit(fence, Commit(resources=(swapped,)))
        quarantined = active.moved(R.QUARANTINED, now=at(4), note="removal unconfirmed")
        store.commit(fence, Commit(resources=(quarantined,)))
        assert store.resource(planned.key) == quarantined
        reused = dataclasses.replace(quarantined, status=R.RELEASED, version=4)
        with pytest.raises(LedgerIntegrityError, match="cannot move"):
            store.commit(fence, Commit(resources=(reused,)))

    def test_a_candidate_is_recorded_only_with_its_freeze(self, store: ExecutionStore) -> None:
        fence = claimed(store)
        execution = to_running_agent(store, fence, fenced_execution(store, fence))
        verifying, step = MACHINE.advance(
            execution, S.VERIFYING, now=at(7), reason="t", candidate_revision=CANDIDATE
        )
        with pytest.raises(LedgerIntegrityError, match="no matching"):
            store.apply(verifying, step, fence=fence)
        first, other = freeze_op(fence, revision="c" * 40)
        store.commit(fence, Commit(operations=(first,)))
        store.commit(fence, Commit(operations=(other,)))
        with pytest.raises(LedgerIntegrityError, match="no matching"):
            store.apply(verifying, step, fence=fence)
        assert store.get("e1") == execution

    def test_a_candidate_and_its_freeze_can_be_committed_together(
        self, store: ExecutionStore
    ) -> None:
        fence = claimed(store)
        execution = to_running_agent(store, fence, fenced_execution(store, fence))
        first, done = freeze_op(fence)
        store.commit(fence, Commit(operations=(first,)))
        verifying, step = MACHINE.advance(
            execution, S.VERIFYING, now=at(7), reason="t", candidate_revision=CANDIDATE
        )
        store.commit(fence, Commit(applied=((verifying, step),), operations=(done,)))
        assert store.get("e1") == verifying

    def test_a_new_execution_cannot_carry_a_candidate(self, store: ExecutionStore) -> None:
        fence = claimed(store)
        execution, transition = MACHINE.start(TASK_A, execution_id="e1", now=at(1))
        smuggled = dataclasses.replace(execution, base_revision=BASE, candidate_revision=CANDIDATE)
        with pytest.raises(LedgerIntegrityError, match="cannot already carry a candidate"):
            store.create(smuggled, transition, fence=fence)
        assert store.get("e1") is None

    def test_a_recorded_candidate_cannot_be_swapped_or_rebased_later(
        self, store: ExecutionStore
    ) -> None:
        fence = claimed(store)
        execution = to_running_agent(store, fence, fenced_execution(store, fence))
        first, done = freeze_op(fence)
        store.commit(fence, Commit(operations=(first,)))
        verifying, step = MACHINE.advance(
            execution, S.VERIFYING, now=at(7), reason="t", candidate_revision=CANDIDATE
        )
        store.commit(fence, Commit(applied=((verifying, step),), operations=(done,)))
        publishing, to_publish = MACHINE.advance(verifying, S.PUBLISHING, now=at(8), reason="t")
        for tampered in (
            dataclasses.replace(publishing, candidate_revision="e" * 40),
            dataclasses.replace(publishing, base_revision="d" * 40),
        ):
            with pytest.raises(LedgerIntegrityError, match="no matching"):
                store.apply(tampered, to_publish, fence=fence)
        store.apply(publishing, to_publish, fence=fence)  # unchanged: fine
        assert store.get("e1") == publishing

    def test_an_intent_needs_the_version_its_writer_saw(self, store: ExecutionStore) -> None:
        fence = claimed(store)
        execution = fenced_execution(store, fence)
        claimed_execution, step = MACHINE.advance(execution, S.CLAIMED, now=at(2), reason="t")
        store.apply(claimed_execution, step, fence=fence)
        stale = Commit(operations=(intent(K.CLAIM_TASK, fence),), expected=(("e1", 1),))
        with pytest.raises(ConcurrencyConflictError, match="stale"):
            store.commit(fence, stale)
        assert store.operations("e1") == ()
        store.commit(
            fence, Commit(operations=(intent(K.CLAIM_TASK, fence),), expected=(("e1", 2),))
        )

    def test_one_commit_cannot_create_two_active_executions_for_a_task(
        self, store: ExecutionStore
    ) -> None:
        fence = claimed(store)
        first = MACHINE.start(TASK_A, execution_id="e1", now=at(1))
        second = MACHINE.start(TASK_A, execution_id="e2", now=at(1))
        with pytest.raises(DuplicateExecutionError):
            store.commit(fence, Commit(created=(first, second)))
        assert store.get("e1") is None

    def test_concurrent_intents_for_one_key_admit_exactly_one(self, store: ExecutionStore) -> None:
        fence = claimed(store)
        fenced_execution(store, fence)
        results: list[str] = []
        barrier = threading.Barrier(8)

        def attempt() -> None:
            barrier.wait()
            try:
                store.commit(fence, Commit(operations=(intent(K.PUBLISH_REVISION, fence),)))
                results.append("ok")
            except ConcurrencyConflictError:
                results.append("conflict")

        threads = [threading.Thread(target=attempt) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        assert sorted(results) == ["conflict"] * 7 + ["ok"]


# -- the event log (ADR-0024) -------------------------------------------------------------


def stored_state(store: ExecutionStore) -> StoredState:
    """The rows I11 is checked against, read through the port only."""
    executions = store.list_in_states(set(ExecutionState))
    return StoredState(
        executions=executions,
        operations=[o for e in executions for o in store.operations(e.id)],
        resources=store.resources(),
        transitions={e.id: len(store.history(e.id)) for e in executions},
        evidence={e.id: len(store.evidence(e.id)) for e in executions},
    )


def check_i11(store: ExecutionStore) -> None:
    """Invariant I11: once coverage started, the events rebuild every stored row."""
    events = store.events()
    if events:
        report = verify(events, stored_state(store))
        assert report.failures == (), report.failures


def types(events: Sequence[Event]) -> list[str]:
    return [e.type.value for e in events]


class EventLogContract:
    """Mixed into :class:`ExecutionStoreContract`; see ADR-0024 §2, §6 and §8."""

    def test_an_unclaimed_store_has_no_events(self, store: ExecutionStore) -> None:
        _create(store, "e1", TASK_A, 0)
        assert store.events() == ()

    def test_the_first_claim_starts_coverage_with_a_baseline(self, store: ExecutionStore) -> None:
        execution = _create(store, "e1", TASK_A, 0)
        _advance(store, execution, S.CLAIMED, 1)
        store.append_evidence(evidence_record("e1"))
        fence = claimed(store)
        marker, baseline, acquired = store.events()
        assert marker.type is EventType.COVERAGE_STARTED
        assert (marker.seq, marker.epoch, marker.actor.kind.value) == (1, 0, "migration")
        assert {k: marker.data[k] for k in ("executions", "operations", "resources")} == {
            "executions": 1, "operations": 0, "resources": 0}  # fmt: skip
        assert baseline.type is EventType.EXECUTION_BASELINE
        assert baseline.data == {"state": "claimed", "version": 2, "transitions_before": 2,
                                 "evidence_before": 1}  # fmt: skip
        assert acquired.type is EventType.OWNERSHIP_ACQUIRED
        assert (acquired.seq, acquired.epoch, acquired.data) == (3, fence.epoch, {})
        report = verify(store.events(), stored_state(store))
        assert report.failures == ()
        assert {f.subject for f in report.historical} == {"execution:e1"}

    def test_a_later_claim_records_only_the_takeover(self, store: ExecutionStore) -> None:
        first = claimed(store)
        second = claimed(store)
        events = store.events()
        assert types(events) == ["events.coverage_started", "ownership.acquired",
                                 "ownership.acquired"]  # fmt: skip
        assert events[-1].epoch == second.epoch
        assert events[-1].data == {"previous_instance": "inst", "previous_epoch": first.epoch}

    def test_every_write_appends_exactly_its_paired_events(self, store: ExecutionStore) -> None:
        fence = claimed(store)
        start = len(store.events())
        execution = fenced_execution(store, fence)
        claim = intent(K.CLAIM_TASK, fence)
        box = Resource.intended(ResourceKind.CONTAINER, "e1", 1, "c1", owner_token="t", now=at(2))
        store.commit(fence, Commit(operations=(claim,), resources=(box,)))
        updated, step = MACHINE.advance(execution, S.CLAIMED, now=at(3), reason="claimed")
        done = claim.moved(ST.SUCCEEDED, fence=fence, now=at(3))
        active = box.moved(R.ACTIVE, now=at(3), identity="c0ffee")
        store.commit(fence, Commit(applied=((updated, step),), operations=(done,),
                                   resources=(active,)))  # fmt: skip
        store.append_evidence(evidence_record("e1", seconds=4), fence=fence)
        new = store.events(after_seq=start)
        assert types(new) == [
            "execution.created",
            "operation.intended", "resource.recorded",
            "execution.transitioned", "operation.succeeded", "resource.changed",
            "evidence.recorded",
        ]  # fmt: skip
        assert [e.seq for e in new] == list(range(start + 1, start + 1 + len(new)))
        assert new[3].data == {"from": "queued", "to": "claimed", "version": 2}
        assert new[4].data["from"] == "intended"  # the stored status, filled by the store
        assert new[5].data["from"] == "intended"
        assert all(e.execution == "e1" and e.epoch == fence.epoch for e in new)
        assert all(not e.flags for e in store.events())

    def test_events_read_back_exactly_as_derived(self, store: ExecutionStore) -> None:
        fence = claimed(store)
        execution, transition = MACHINE.start(TASK_A, execution_id="e1", now=at(1))
        claim = intent(K.CLAIM_TASK, fence)
        changes = Commit(created=((execution, transition),))
        context = EventContext(at=at(1), instance="inst", epoch=fence.epoch,
                               pass_id="p-0a1b2c", tasks={"e1": "fake:A"})  # fmt: skip
        store.commit(fence, changes, context=context)
        failed = claim.moved(ST.FAILED, fence=fence, now=at(3))
        store.commit(fence, Commit(operations=(claim,)))
        later = EventContext(at=at(3), instance="inst", epoch=fence.epoch, pass_id="p-0a1b2c",
                             reasons={claim.key: "not-claimed-other"},
                             durations_ms={claim.key: 412},
                             error_classes={claim.key: "ProviderError"})  # fmt: skip
        store.commit(fence, Commit(operations=(failed,)), context=later)
        created, intended, outcome = store.events()[-3:]
        (expected_created,) = derive_events(changes, context)
        assert created.to_json() == expected_created.with_seq(created.seq or 0).to_json()
        assert intended.type is EventType.OPERATION_INTENDED
        assert outcome.to_json() == (
            derive_events(Commit(operations=(failed,)), later)[0]
            .with_previous("intended")
            .with_seq(outcome.seq or 0)
            .to_json()
        )
        assert (outcome.pass_id, outcome.reason) == ("p-0a1b2c", "not-claimed-other")
        assert outcome.data["duration_ms"] == 412

    def test_a_refused_write_appends_no_event(self, store: ExecutionStore) -> None:
        old = claimed(store)
        fenced_execution(store, old)
        fence = claimed(store)
        before = store.events()
        stale = intent(K.CLAIM_TASK, old)
        with pytest.raises(FencingError):
            store.commit(old, Commit(operations=(stale,)))
        with pytest.raises(FencingError):
            store.append_evidence(evidence_record("e1"), fence=old)
        bad = Resource.intended(ResourceKind.WORKSPACE, "e1", 2, "/w", owner_token="t", now=at(1))
        with pytest.raises(LedgerIntegrityError):  # attempt 2 does not exist
            store.commit(fence, Commit(operations=(intent(K.CLAIM_TASK, fence),),
                                       resources=(bad,)))  # fmt: skip
        with pytest.raises(ConcurrencyConflictError):
            store.commit(fence, Commit(operations=(intent(K.CLAIM_TASK, fence),),
                                       expected=(("e1", 9),)))  # fmt: skip
        assert store.events() == before
        assert store.operations("e1") == ()

    def test_a_context_from_another_owner_is_refused(self, store: ExecutionStore) -> None:
        old = claimed(store)
        fence = claimed(store)
        execution, transition = MACHINE.start(TASK_A, execution_id="e1", now=at(1))
        before = store.events()
        for context in (
            EventContext(at=at(1), instance="inst", epoch=old.epoch),
            EventContext(at=at(1), instance="other", epoch=fence.epoch),
        ):
            with pytest.raises(LedgerIntegrityError, match="another owner"):
                store.commit(fence, Commit(created=((execution, transition),)), context=context)
        assert store.get("e1") is None
        assert store.events() == before

    @pytest.mark.parametrize("break_it", ["drop", "duplicate", "foreign", "wrong-version"])
    def test_an_unpaired_row_or_event_writes_nothing(
        self, store: ExecutionStore, monkeypatch: pytest.MonkeyPatch, break_it: str
    ) -> None:
        fence = claimed(store)
        fenced_execution(store, fence)
        other = intent(K.START_CONTAINER, fence, step="c1")
        real = derive_events  # what the store adapters call, through event_log

        def broken(changes: Commit, context: EventContext) -> tuple[Event, ...]:
            events = list(real(changes, context))
            if break_it == "drop":
                events.pop()
            elif break_it == "duplicate":
                events.append(events[0])
            elif break_it == "foreign":
                events += real(Commit(operations=(other,)), context)
            else:
                events[0] = real(Commit(operations=(other,)), context)[0]
            return tuple(events)

        monkeypatch.setattr(event_log, "derive_events", broken)
        claim = intent(K.CLAIM_TASK, fence)
        box = Resource.intended(ResourceKind.CONTAINER, "e1", 1, "c1", owner_token="t", now=at(2))
        before = store.events()
        with pytest.raises(LedgerIntegrityError):
            store.commit(fence, Commit(operations=(claim,), resources=(box,)))
        assert store.events() == before
        assert store.operations("e1") == ()
        assert store.resources() == ()

    @pytest.mark.parametrize("code", ["unclassified", "not-a-code"])
    @pytest.mark.parametrize("where", ["commit", "context"])
    def test_an_explicit_invalid_code_rolls_back_rows_and_events(
        self, store: ExecutionStore, code: str, where: str
    ) -> None:
        fence = claimed(store)
        execution = fenced_execution(store, fence)
        before = (store.events(), store.history("e1"))
        updated, step = MACHINE.advance(execution, S.FAILED, now=at(2), reason="r", error="x")
        failed = intent(K.CLAIM_TASK, fence)
        reasons = {"e1": code}
        changes = Commit(applied=((updated, step),), operations=(failed,),
                         reasons=reasons if where == "commit" else {})  # fmt: skip
        context = EventContext(at=at(2), instance="inst", epoch=fence.epoch,
                               reasons=reasons if where == "context" else {})  # fmt: skip
        with pytest.raises(LedgerIntegrityError, match="not a reason code"):
            store.commit(fence, changes, context=context)
        assert (store.events(), store.history("e1")) == before
        assert store.get("e1") == execution
        assert store.operations("e1") == ()

    def test_a_missing_code_is_the_documented_fallback_and_reads_back(
        self, store: ExecutionStore
    ) -> None:
        """The store's own fallback (ADR-0024 §2): a caller outside the coordinator that
        gives no code is recorded as ``unclassified``, and that history reads back. The
        coordinator never reaches it: its boundary refuses such a commit first."""
        fence = claimed(store)
        execution = fenced_execution(store, fence)
        updated, step = MACHINE.advance(execution, S.FAILED, now=at(2), reason="r", error="x")
        store.commit(fence, Commit(applied=((updated, step),)))
        last = store.events()[-1]
        assert (last.type, last.reason) == (EventType.EXECUTION_TRANSITIONED, "unclassified")
        assert Event.from_json(last.to_json()) == last  # historical reading is strict and works

    def test_events_are_read_after_a_seq_and_up_to_a_limit(self, store: ExecutionStore) -> None:
        for _ in range(4):
            claimed(store)
        assert [e.seq for e in store.events()] == [1, 2, 3, 4, 5]
        assert [e.seq for e in store.events(after_seq=2)] == [3, 4, 5]
        assert [e.seq for e in store.events(after_seq=1, limit=2)] == [2, 3]
        assert store.events(after_seq=5) == ()


class ExecutionStoreContract(FencingContract, EventLogContract):
    def make_store(self, tmp_path: Path) -> Iterator[ExecutionStore]:
        raise NotImplementedError

    @pytest.fixture
    def store(self, tmp_path: Path) -> Iterator[ExecutionStore]:
        # I11 is checked after every test, so after every commit the suite makes.
        for store in self.make_store(tmp_path):
            yield store
            check_i11(store)

    # -- create / get ----------------------------------------------------

    def test_created_execution_round_trips(self, store: ExecutionStore) -> None:
        execution = _create(store, "e1", TASK_A, 0)
        assert store.get("e1") == execution

    def test_get_unknown_returns_none(self, store: ExecutionStore) -> None:
        assert store.get("missing") is None

    def test_creation_is_recorded_in_history(self, store: ExecutionStore) -> None:
        _create(store, "e1", TASK_A, 0)
        (entry,) = store.history("e1")
        assert entry.from_state is None
        assert entry.to_state is S.QUEUED
        assert entry.at == at(0)

    def test_history_of_unknown_execution_raises(self, store: ExecutionStore) -> None:
        with pytest.raises(ExecutionNotFoundError):
            store.history("missing")

    def test_duplicate_id_is_rejected(self, store: ExecutionStore) -> None:
        _create(store, "e1", TASK_A, 0)
        with pytest.raises(DuplicateExecutionError):
            _create(store, "e1", TASK_B, 1)

    def test_second_active_execution_for_task_is_rejected(self, store: ExecutionStore) -> None:
        _create(store, "e1", TASK_A, 0)
        with pytest.raises(DuplicateExecutionError):
            _create(store, "e2", TASK_A, 1)
        assert store.get("e2") is None

    def test_new_execution_allowed_after_previous_one_ends(self, store: ExecutionStore) -> None:
        first = _create(store, "e1", TASK_A, 0)
        _advance(store, first, S.CANCELLED, 1)
        second = _create(store, "e2", TASK_A, 2)
        assert store.active_for_task(TASK_A) == second

    def test_inconsistent_create_is_rejected(self, store: ExecutionStore) -> None:
        _, transition_for_e1 = MACHINE.start(TASK_A, execution_id="e1", now=at(0))
        e2, _ = MACHINE.start(TASK_A, execution_id="e2", now=at(0))
        with pytest.raises(ValueError, match="different execution"):
            store.create(e2, transition_for_e1)
        assert store.get("e1") is None
        assert store.get("e2") is None

    # -- active_for_task / list_in_states --------------------------------

    def test_active_for_task(self, store: ExecutionStore) -> None:
        assert store.active_for_task(TASK_A) is None
        execution = _create(store, "e1", TASK_A, 0)
        assert store.active_for_task(TASK_A) == execution
        assert store.active_for_task(TASK_B) is None
        _advance(store, execution, S.FAILED, 1, error="boom")
        assert store.active_for_task(TASK_A) is None

    def test_list_in_states_filters_and_orders_oldest_first(self, store: ExecutionStore) -> None:
        late = _create(store, "z-late", TASK_A, 10)
        early = _create(store, "a-early", TASK_B, 5)
        same_time = _create(store, "b-same", TaskRef("fake", "C"), 5)
        done = _create(store, "done", TaskRef("fake", "D"), 0)
        _advance(store, done, S.CANCELLED, 20)

        queued = store.list_in_states({S.QUEUED})
        assert [e.id for e in queued] == [early.id, same_time.id, late.id]
        assert [e.id for e in store.list_in_states({S.CANCELLED})] == ["done"]
        assert len(store.list_in_states({S.QUEUED, S.CANCELLED})) == 4
        assert list(store.list_in_states(())) == []

    # -- apply -----------------------------------------------------------

    def test_apply_updates_record_and_appends_history(self, store: ExecutionStore) -> None:
        execution = _create(store, "e1", TASK_A, 0)
        execution = _advance(store, execution, S.CLAIMED, 1)
        execution = _advance(store, execution, S.PROVISIONING, 2, branch="ilmar/a")
        assert store.get("e1") == execution
        assert execution.branch == "ilmar/a"
        assert [t.to_state for t in store.history("e1")] == [S.QUEUED, S.CLAIMED, S.PROVISIONING]
        assert [t.from_state for t in store.history("e1")] == [None, S.QUEUED, S.CLAIMED]

    def test_optional_fields_round_trip(self, store: ExecutionStore) -> None:
        execution = _create(store, "e1", TASK_A, 0)
        execution = _advance(store, execution, S.CLAIMED, 1)
        execution = _advance(store, execution, S.PROVISIONING, 2, branch="b")
        execution = _advance(
            store,
            execution,
            S.RETRY_WAIT,
            3,
            error="workspace failed: ünïcödé",
            change_request_url="https://example.invalid/cr/1",
        )
        stored = store.get("e1")
        assert stored == execution
        assert stored is not None
        assert stored.last_error == "workspace failed: ünïcödé"

    def test_timestamps_preserve_the_instant(self, store: ExecutionStore) -> None:
        offset = timezone(timedelta(hours=5, minutes=30))
        when = at(0.123456).astimezone(offset)
        execution, transition = MACHINE.start(TASK_A, execution_id="e1", now=when)
        store.create(execution, transition)
        stored = store.get("e1")
        assert stored is not None
        assert stored.created_at == when
        assert stored.created_at.utcoffset() is not None

    def test_stale_update_is_rejected_and_nothing_changes(self, store: ExecutionStore) -> None:
        base = _create(store, "e1", TASK_A, 0)
        winner = _advance(store, base, S.CLAIMED, 1)
        loser, transition = MACHINE.advance(base, S.CANCELLED, now=at(2), reason="race")
        with pytest.raises(ConcurrencyConflictError):
            store.apply(loser, transition)
        assert store.get("e1") == winner
        assert len(store.history("e1")) == 2

    def test_apply_to_unknown_execution_raises(self, store: ExecutionStore) -> None:
        execution, _ = MACHINE.start(TASK_A, execution_id="ghost", now=at(0))
        updated, transition = MACHINE.advance(execution, S.CLAIMED, now=at(1), reason="x")
        with pytest.raises(ExecutionNotFoundError):
            store.apply(updated, transition)

    def test_concurrent_creates_for_one_task_admit_exactly_one(self, store: ExecutionStore) -> None:
        results: list[str] = []
        barrier = threading.Barrier(8)

        def attempt(n: int) -> None:
            execution, transition = MACHINE.start(TASK_A, execution_id=f"e{n}", now=at(n))
            barrier.wait()
            try:
                store.create(execution, transition)
            except DuplicateExecutionError:
                results.append("duplicate")
            else:
                results.append("created")

        threads = [threading.Thread(target=attempt, args=(n,)) for n in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        assert sorted(results) == ["created"] + ["duplicate"] * 7

    # -- candidate revisions and evidence (#9) -------------------------------------

    def test_revisions_round_trip_and_reset_on_retry(self, store: ExecutionStore) -> None:
        execution = _create(store, "e1", TASK_A, 0)
        for seconds, target in enumerate(
            (S.CLAIMED, S.PROVISIONING, S.RUNNING_AGENT, S.VERIFYING), start=1
        ):
            execution = _advance(store, execution, target, seconds)
        stored = store.get("e1")
        assert stored is not None
        assert (stored.base_revision, stored.candidate_revision) == (BASE, CANDIDATE)
        execution = _advance(store, execution, S.RETRY_WAIT, 10, error="checks failed")
        execution = _advance(store, execution, S.PROVISIONING, 11)
        stored = store.get("e1")
        assert stored is not None
        assert (stored.base_revision, stored.candidate_revision) == (None, None)
        assert stored.attempt == 2

    def test_evidence_is_appended_in_order_and_round_trips(self, store: ExecutionStore) -> None:
        _create(store, "e1", TASK_A, 0)
        _create(store, "e2", TASK_B, 0)
        first, second = evidence_record("e1", 1, 10), evidence_record("e1", 2, 20)
        store.append_evidence(first)
        store.append_evidence(evidence_record("e2"))
        store.append_evidence(second)
        assert store.evidence("e1") == (first, second)
        assert len(store.evidence("e2")) == 1

    def test_evidence_of_an_execution_without_any_is_empty(self, store: ExecutionStore) -> None:
        _create(store, "e1", TASK_A, 0)
        assert tuple(store.evidence("e1")) == ()

    def test_evidence_needs_a_known_execution(self, store: ExecutionStore) -> None:
        with pytest.raises(ExecutionNotFoundError):
            store.append_evidence(evidence_record("nope"))
        with pytest.raises(ExecutionNotFoundError):
            store.evidence("nope")

    def test_returned_evidence_cannot_change_what_is_stored(self, store: ExecutionStore) -> None:
        _create(store, "e1", TASK_A, 0)
        store.append_evidence(evidence_record("e1"))
        returned = store.evidence("e1")
        assert isinstance(returned, tuple)
        assert store.evidence("e1") == returned
