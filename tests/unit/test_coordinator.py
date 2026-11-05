# SPDX-License-Identifier: Apache-2.0
"""One coordinator execution end to end (#11b, ADR-0021), with fakes."""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

import pytest

from coordinator_support import REPO, Setup, make_setup, policy, task
from ilmar.adapters.sqlite import SQLiteExecutionStore
from ilmar.core.journal import check_candidate
from ilmar.core.verification import Verifier
from ilmar.core.worker import Outcome
from ilmar.domain.agent import AgentOutcome
from ilmar.domain.changes import CandidateRevision, ChangeRequestState
from ilmar.domain.errors import ConflictError, ContainmentError, FencingError, ProviderError
from ilmar.domain.execution import ExecutionState
from ilmar.domain.ledger import (
    OperationKind,
    OperationStatus,
    ResourceKind,
    ResourceStatus,
    changed_paths_digest,
    operation_key,
)
from ilmar.domain.verification import Verdict, VerificationCommand

S = ExecutionState
K = OperationKind
R = ResourceStatus
CODE = {"success": 0, "failure": 1, "blocked": 2}


@pytest.fixture
def setup(tmp_path: Path) -> Setup:
    return make_setup(tmp_path)


@pytest.fixture
def seen(monkeypatch: pytest.MonkeyPatch) -> list[CandidateRevision]:
    """Every candidate the Verifier was given."""
    candidates: list[CandidateRevision] = []
    original = Verifier.verify

    def spy(self: Verifier, **kwargs: Any) -> Any:
        candidates.append(kwargs["candidate"])
        return original(self, **kwargs)

    monkeypatch.setattr(Verifier, "verify", spy)
    return candidates


def states(setup: Setup, execution_id: str = "e1") -> list[S]:
    return [t.to_state for t in setup.store.history(execution_id)]


def resources(setup: Setup, kind: ResourceKind, execution_id: str = "e1") -> list[Any]:
    return [r for r in setup.store.resources(execution_id=execution_id) if r.kind is kind]


# -- the happy path ----------------------------------------------------------------------


def test_one_task_is_published_exactly_as_verified(
    setup: Setup, seen: list[CandidateRevision]
) -> None:
    report = setup.coordinator().run_once()
    assert (report.code, report.results[0].outcome) == (0, Outcome.PUBLISHED)
    execution = setup.store.get("e1")
    assert execution is not None
    assert states(setup) == [
        S.QUEUED,
        S.CLAIMED,
        S.PROVISIONING,
        S.RUNNING_AGENT,
        S.VERIFYING,
        S.PUBLISHING,
        S.SUCCEEDED,
    ]
    # Exactly one claim, push and change request.
    assert len(setup.scm.pushes) == 1
    (_, branch, pushed) = setup.scm.pushes[0]
    assert branch == execution.branch
    # The published revision is exactly the verified, frozen candidate.
    (evidence,) = setup.store.evidence("e1")
    assert pushed == execution.candidate_revision == evidence.candidate_revision
    assert evidence.base_revision == execution.base_revision
    assert evidence.verdict is Verdict.PUBLISH
    assert setup.scm.branch_head(REPO, branch) == pushed
    # The Verifier saw exactly the frozen candidate and its changed paths.
    (candidate,) = seen
    freeze = setup.store.operation(operation_key(K.FREEZE_CANDIDATE, "e1", 1))
    assert freeze is not None
    check_candidate(candidate, freeze)
    assert candidate.changed_paths == ("src/app.py",)
    assert freeze.result["changed_paths_digest"] == changed_paths_digest(["src/app.py"])
    # The journal is complete and every effect happened once.
    assert all(o.status is OperationStatus.SUCCEEDED for o in setup.store.operations("e1"))
    assert setup.store.unfinished_operations() == ()
    kinds = [o.kind for o in setup.store.operations("e1")]
    assert kinds.count(K.CLAIM_TASK) == kinds.count(K.PUBLISH_REVISION) == 1
    assert kinds.count(K.UPSERT_CHANGE_REQUEST) == 1


def test_execution_completion_keeps_the_claim_and_cleans_up(setup: Setup) -> None:
    setup.coordinator().run_once()
    execution = setup.store.get("e1")
    assert execution is not None
    assert execution.change_request_url
    assert setup.tasks.list_eligible() == ()  # still claimed while the change request is open
    assert any("published" in update for update in setup.tasks.updates(task("1").ref))
    # Sandboxes are gone, the workspace is deleted, and the ledger says so.
    assert {r.status for r in resources(setup, ResourceKind.CONTAINER)} == {R.RELEASED}
    assert {r.status for r in resources(setup, ResourceKind.WORKSPACE)} == {R.RELEASED}
    assert not setup.layout.attempt_dir("e1", 1).exists()
    (request,) = resources(setup, ResourceKind.CHANGE_REQUEST)
    assert request.status is R.ACTIVE


def test_an_open_change_request_keeps_the_task_out_of_new_work(setup: Setup) -> None:
    coordinator = setup.coordinator()
    coordinator.run_once()
    setup.tasks.release(task("1").ref)  # even if the claim label went missing
    report = coordinator.run_once()
    assert (report.code, report.summary) == (0, "no eligible task")
    assert setup.store.get("e2") is None


@pytest.mark.parametrize("merged", [True, False])
def test_task_completion_follows_the_change_request(setup: Setup, merged: bool) -> None:
    coordinator = setup.coordinator()
    coordinator.run_once()
    (resource,) = resources(setup, ResourceKind.CHANGE_REQUEST)
    upsert = setup.store.operation(operation_key(K.UPSERT_CHANGE_REQUEST, "e1", 1))
    assert upsert is not None
    from ilmar.domain.changes import ChangeRequest

    request = ChangeRequest(REPO, resource.identity, upsert.result["url"], resource.name, "main")
    assert setup.scm.change_request_state(request) is ChangeRequestState.OPEN
    setup.scm.finish_change_request(request, merged=merged)
    report = coordinator.run_once()
    (after,) = resources(setup, ResourceKind.CHANGE_REQUEST)
    assert after.status is R.RELEASED
    assert after.note == ("merged: task complete" if merged else "closed without merging")
    assert report.completed_tasks == (("e1",) if merged else ())
    assert setup.tasks.list_eligible() == (task("1"),)  # the claim is released
    assert setup.store.get("e2") is None  # a published task is never started again
    execution = setup.store.get("e1")
    assert execution is not None
    assert execution.state is S.SUCCEEDED  # execution completion is unchanged


def test_a_protected_path_is_published_for_review(setup: Setup) -> None:
    setup.agent.plan = [
        (AgentOutcome.COMPLETED, {"src/app.py": "print('fixed')\n", "AGENTS.md": "new rules"})
    ]
    setup.coordinator().run_once()
    (evidence,) = setup.store.evidence("e1")
    assert evidence.verdict is Verdict.PUBLISH_FOR_REVIEW
    upsert = setup.store.operation(operation_key(K.UPSERT_CHANGE_REQUEST, "e1", 1))
    assert upsert is not None
    assert "ilmar:needs-human-review" in upsert.result["labels"]


# -- retries: only transient failures, at most three attempts ----------------------------


def test_a_timed_out_agent_gets_a_new_attempt(setup: Setup) -> None:
    setup.agent.plan = [
        (AgentOutcome.TIMED_OUT, {}),
        (AgentOutcome.COMPLETED, {"src/app.py": "print('fixed')\n"}),
    ]
    report = setup.coordinator().run_once()
    assert report.code == 0
    execution = setup.store.get("e1")
    assert execution is not None
    assert (execution.state, execution.attempt) == (S.SUCCEEDED, 2)
    assert S.RETRY_WAIT in states(setup)
    # The failed attempt's workspace is kept for inspection; the successful one is deleted.
    first = setup.layout.attempt_dir("e1", 1)
    assert first.exists()
    assert not setup.layout.attempt_dir("e1", 2).exists()
    kept = [r for r in resources(setup, ResourceKind.WORKSPACE) if r.attempt == 1]
    assert {r.status for r in kept} == {R.ACTIVE}


def test_three_transient_failures_end_the_execution(setup: Setup) -> None:
    setup.agent.plan = [(AgentOutcome.TIMED_OUT, {})]
    report = setup.coordinator(max_attempts=5).run_once()  # capped at 3
    execution = setup.store.get("e1")
    assert execution is not None
    assert (report.code, execution.state, execution.attempt) == (1, S.FAILED, 3)
    assert "attempts exhausted" in (execution.last_error or "")
    assert setup.agent.runs == 3
    assert setup.tasks.list_eligible() == (task("1"),)  # released on terminal failure
    assert setup.scm.pushes == []
    for attempt in (1, 2, 3):
        assert setup.layout.attempt_dir("e1", attempt).exists()  # all kept


@pytest.mark.parametrize(
    ("plan", "reason"),
    [
        ([(AgentOutcome.FAILED, {})], "the agent failed"),
        ([(AgentOutcome.COMPLETED, {})], "the agent changed nothing"),
        (
            [(AgentOutcome.COMPLETED, {"src/app.py": "print('still a bug')\n"})],
            "verification checks failed",
        ),
        (
            [(AgentOutcome.COMPLETED, {".gitlab-ci.yml": "stages: []\n"})],
            "CI configuration changed",
        ),
    ],
)
def test_deterministic_and_policy_failures_are_never_retried(
    setup: Setup, plan: list[Any], reason: str
) -> None:
    setup.agent.plan = plan
    report = setup.coordinator().run_once()
    execution = setup.store.get("e1")
    assert execution is not None
    assert (report.code, execution.state, execution.attempt) == (1, S.FAILED, 1)
    assert reason in (execution.last_error or "")
    assert S.RETRY_WAIT not in states(setup)
    assert setup.agent.runs == 1
    assert setup.scm.pushes == []
    assert setup.tasks.list_eligible() == (task("1"),)
    assert setup.layout.attempt_dir("e1", 1).exists()  # kept for inspection


def test_a_branch_that_moved_is_never_overwritten_or_retried(setup: Setup) -> None:
    def someone_pushes() -> None:
        execution = setup.store.get("e1")
        assert execution is not None
        assert execution.branch
        moved = setup.scm.push_external(REPO, "main", {"x": "y"})
        setup.scm._branches[(REPO, execution.branch)] = moved  # someone pushed to the task branch

    setup.agent.during = someone_pushes
    report = setup.coordinator().run_once()
    execution = setup.store.get("e1")
    assert execution is not None
    assert (report.code, execution.state) == (1, S.FAILED)
    assert "LeaseConflictError" in (execution.last_error or "")
    assert setup.scm.pushes == []
    publish = setup.store.operation(operation_key(K.PUBLISH_REVISION, "e1", 1))
    assert publish is not None
    assert publish.status is OperationStatus.FAILED


def test_a_task_claimed_by_someone_else_is_cancelled_untouched(setup: Setup) -> None:
    class Racing:
        def __getattr__(self, name: str) -> Any:
            return getattr(setup.tasks, name)

        def claim(self, ref: Any) -> Any:
            setup.tasks.claim_as(ref, "human")
            return setup.tasks.claim(ref)

    setup.components.task_provider = Racing()
    report = setup.coordinator().run_once()
    assert (report.code, report.results[0].outcome) == (0, Outcome.CANCELLED)
    assert states(setup) == [S.QUEUED, S.CANCELLED]
    assert setup.runtime.started == []
    assert not setup.layout.attempt_dir("e1", 1).exists()


# -- ambiguous effects and reconciliation in the same run --------------------------------


def test_a_push_whose_answer_was_lost_is_recognised_not_repeated(setup: Setup) -> None:
    original = setup.scm.publish_revision

    def pushed_then_lost(*args: Any, **kwargs: Any) -> None:
        original(*args, **kwargs)
        raise ProviderError("connection reset")

    setup.scm.publish_revision = pushed_then_lost  # type: ignore[method-assign]
    report = setup.coordinator().run_once()
    assert report.code == 0
    assert len(setup.scm.pushes) == 1
    publish = setup.store.operation(operation_key(K.PUBLISH_REVISION, "e1", 1))
    assert publish is not None
    assert publish.status is OperationStatus.SUCCEEDED


def test_a_push_that_never_happened_is_retried_under_a_new_key(setup: Setup) -> None:
    original, calls = setup.scm.publish_revision, []

    def flaky(*args: Any, **kwargs: Any) -> None:
        calls.append(1)
        if len(calls) == 1:
            raise ProviderError("timeout before sending")
        original(*args, **kwargs)

    setup.scm.publish_revision = flaky  # type: ignore[method-assign]
    assert setup.coordinator().run_once().code == 0
    assert len(setup.scm.pushes) == 1
    first = setup.store.operation(operation_key(K.PUBLISH_REVISION, "e1", 1))
    retry = setup.store.operation(operation_key(K.PUBLISH_REVISION, "e1", 1, "retry-1"))
    assert first is not None
    assert retry is not None
    assert (first.status, retry.status) == (OperationStatus.FAILED, OperationStatus.SUCCEEDED)


def test_a_sandbox_that_never_started_is_a_transient_failure(setup: Setup) -> None:
    original, calls = setup.runtime.start, []

    def flaky(spec: Any) -> Any:
        calls.append(1)
        if len(calls) == 1:
            raise ProviderError("podman hiccup")
        return original(spec)

    setup.runtime.start = flaky  # type: ignore[method-assign]
    assert setup.coordinator().run_once().code == 0
    execution = setup.store.get("e1")
    assert execution is not None
    assert execution.attempt == 2
    (refused,) = [r for r in resources(setup, ResourceKind.CONTAINER) if r.attempt == 1]
    assert (refused.status, refused.note) == (R.RELEASED, "never created")


def test_an_unremovable_sandbox_blocks_and_is_never_frozen(setup: Setup) -> None:
    def refuse(handle: Any) -> None:
        raise ContainmentError("still running", container_id=handle.id, name=handle.name)

    setup.runtime.stop_hook = refuse
    report = setup.coordinator().run_once()
    assert (report.code, report.results[0].outcome) == (2, Outcome.BLOCKED)
    execution = setup.store.get("e1")
    assert execution is not None
    assert execution.state is S.FAILED
    assert setup.store.operation(operation_key(K.FREEZE_CANDIDATE, "e1", 1)) is None
    (container,) = resources(setup, ResourceKind.CONTAINER)
    assert container.status is R.QUARANTINED
    assert setup.tasks.list_eligible() == ()  # still claimed: a human must look


def test_unfinished_operations_block_new_work(setup: Setup) -> None:
    setup.runtime.stop_hook = lambda handle: (_ for _ in ()).throw(ProviderError("lost"))
    first = setup.coordinator().run_once()
    assert first.code == 2
    setup.runtime.stop_hook = None
    setup.tasks.add(task("2"))
    second = setup.coordinator().run_once()
    assert second.code == 2
    assert "unfinished operation" in second.summary
    assert setup.store.get("e2") is None


# -- fencing and concurrency -------------------------------------------------------------


def test_a_coordinator_that_loses_ownership_stops_before_its_next_effect(
    tmp_path: Path,
) -> None:
    path = tmp_path / "executions.sqlite3"
    store = SQLiteExecutionStore(path)
    setup = make_setup(tmp_path, store=store)

    def takeover() -> None:
        with SQLiteExecutionStore(path) as intruder:
            intruder.claim_instance("inst", now=setup.clock())

    setup.agent.during = takeover
    with pytest.raises(FencingError):
        setup.coordinator().run_once()
    kinds = [o.kind for o in store.operations("e1")]
    assert K.FREEZE_CANDIDATE not in kinds
    assert setup.scm.pushes == []
    store.close()


def test_two_executions_run_at_the_same_time_without_interfering(tmp_path: Path) -> None:
    setup = make_setup(tmp_path, tasks=(task("1"), task("2")))
    coordinator = setup.coordinator()
    barrier = threading.Barrier(2)
    original = setup.agent.run

    def in_parallel(*args: Any, **kwargs: Any) -> Any:
        barrier.wait(timeout=10)  # both agents are running at once
        return original(*args, **kwargs)

    setup.agent.run = in_parallel  # type: ignore[method-assign]
    executions = []
    for key in ("1", "2"):
        execution, transition = coordinator._machine.start(
            task(key).ref, execution_id=f"p{key}", now=setup.clock()
        )
        setup.store.create(execution, transition, fence=setup.ownership.fence)
        executions.append(execution)
    results: dict[str, Any] = {}
    threads = [
        threading.Thread(
            target=lambda k=key: results.__setitem__(k, coordinator.dispatch(f"p{k}", task(k)))
        )
        for key in ("1", "2")
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert {r.outcome for r in results.values()} == {Outcome.PUBLISHED}
    branches = {branch for _, branch, _ in setup.scm.pushes}
    assert len(branches) == 2
    assert len(set(setup.runtime.started)) == len(setup.runtime.started)


def test_one_execution_never_has_two_workers(setup: Setup) -> None:
    coordinator = setup.coordinator()
    execution, transition = coordinator._machine.start(
        task("1").ref, execution_id="p1", now=setup.clock()
    )
    setup.store.create(execution, transition, fence=setup.ownership.fence)
    with coordinator.leases.acquire("p1"), pytest.raises(ConflictError):
        coordinator.dispatch("p1", task("1"))


def test_verification_runs_in_its_own_sandbox_per_job(tmp_path: Path) -> None:
    setup = make_setup(
        tmp_path,
        verification=policy(
            VerificationCommand("fixed", ("check", "src/app.py", "fixed"), job="tests"),
            VerificationCommand("readme", ("check", "README.md", "readme"), job="docs"),
        ),
    )
    assert setup.coordinator().run_once().code == 0
    verify_names = [n for n in setup.runtime.started if "-verify-" in n]
    assert verify_names == ["ilmar-inst-e1-1-verify-1-tests", "ilmar-inst-e1-1-verify-1-docs"]
    names = {r.name for r in resources(setup, ResourceKind.CONTAINER)}
    assert set(verify_names) <= names


# -- the published revision is exactly the verified frozen candidate ---------------------


def test_a_candidate_that_differs_from_its_freeze_is_never_verified_or_published(
    setup: Setup, monkeypatch: pytest.MonkeyPatch
) -> None:
    from ilmar.core.effects import Effects

    original = Effects.freeze

    def swapped(self: Effects, *args: Any, **kwargs: Any) -> CandidateRevision:
        real = original(self, *args, **kwargs)
        return CandidateRevision(real.revision, real.base_revision, ())  # paths dropped

    monkeypatch.setattr(Effects, "freeze", swapped)
    report = setup.coordinator().run_once()
    assert (report.code, report.results[0].outcome) == (2, Outcome.BLOCKED)
    assert "does not match" in report.results[0].detail
    assert setup.store.evidence("e1") == ()
    assert setup.scm.pushes == []


def test_publishing_needs_the_latest_evidence_to_be_for_this_candidate(
    setup: Setup, monkeypatch: pytest.MonkeyPatch
) -> None:
    import dataclasses

    original = Verifier.verify

    def then_tampered(self: Verifier, **kwargs: Any) -> Any:
        record = original(self, **kwargs)
        other = dataclasses.replace(record, candidate_revision="f" * 40)
        setup.store.append_evidence(other, fence=setup.ownership.fence)
        return record

    monkeypatch.setattr(Verifier, "verify", then_tampered)
    report = setup.coordinator().run_once()
    assert report.code == 2
    assert "latest evidence" in report.results[0].detail
    assert setup.scm.pushes == []


def test_attempts_beyond_three_are_refused_by_construction(setup: Setup) -> None:
    from ilmar.core.state_machine import ExecutionStateMachine
    from ilmar.core.worker import ExecutionWorker

    coordinator = setup.coordinator(max_attempts=9)
    assert coordinator._machine.max_attempts == 3
    with pytest.raises(ValueError, match="at most 3 attempts"):
        ExecutionWorker(
            components=setup.components,
            effects=coordinator._effects,
            leases=coordinator.leases,
            machine=ExecutionStateMachine(max_attempts=4),
            clock=setup.clock,
            agent_timeout_seconds=1,
        )
