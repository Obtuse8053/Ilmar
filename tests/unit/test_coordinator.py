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


def test_an_unsettleable_execution_blocks_alone_and_other_work_continues(setup: Setup) -> None:
    """ADR-0022 decision 4: block only what cannot be reconciled."""
    original = setup.scm.publish_revision

    def pushed_elsewhere(*args: Any, **kwargs: Any) -> None:
        setup.scm._branches[(REPO, kwargs["branch"])] = setup.scm.seed(REPO, "x", {"y": "z"})
        raise ProviderError("connection reset")

    setup.scm.publish_revision = pushed_elsewhere  # type: ignore[method-assign]
    assert setup.coordinator().run_once().code == 2
    setup.scm.publish_revision = original  # type: ignore[method-assign]
    setup.tasks.add(task("2"))
    second = setup.coordinator().run_once()
    by_id = {r.execution_id: r for r in second.results}
    assert by_id["e1"].outcome is Outcome.BLOCKED  # still: never overwritten
    assert "unverified head" in by_id["e1"].detail
    others = [r for r in second.results if r.execution_id != "e1"]
    assert [r.outcome for r in others] == [Outcome.PUBLISHED]
    assert second.code == 2  # a human is still needed for e1


def test_an_unsettled_repeatable_effect_does_not_stop_other_work(setup: Setup) -> None:
    setup.runtime.stop_hook = lambda handle: (_ for _ in ()).throw(ProviderError("lost"))
    assert setup.coordinator().run_once().code == 2  # e1 blocked: its stop is unclear
    setup.runtime.stop_hook = None
    setup.tasks.add(task("2"))
    second = setup.coordinator().run_once()
    assert (second.code, second.results[0].outcome) == (0, Outcome.PUBLISHED)


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
    setup: Setup,
) -> None:
    """The reloaded candidate must match the freeze's revision, base and digest."""
    original = setup.scm.load_candidate

    def tampered(*args: Any, **kwargs: Any) -> CandidateRevision:
        real = original(*args, **kwargs)
        return CandidateRevision(real.revision, real.base_revision, ())  # paths dropped

    setup.scm.load_candidate = tampered  # type: ignore[method-assign]
    report = setup.coordinator().run_once()
    assert (report.code, report.results[0].outcome) == (2, Outcome.BLOCKED)
    assert "does not match" in report.results[0].detail
    assert setup.store.evidence("e1") == ()
    assert setup.scm.pushes == []


@pytest.mark.events_unchecked  # the test itself appends forged evidence, outside any pass
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
    assert "evidence" in report.results[0].detail
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


# -- adversarial review of 46c1662 -------------------------------------------------------


def test_a_containment_failure_while_starting_is_never_retried(setup: Setup) -> None:
    """Podman's start raises ContainmentError (a ProviderError) if cleanup is unconfirmed."""

    def unconfirmed(spec: Any) -> Any:
        raise ContainmentError("egress sidecar not removed", container_id="c", name=spec.name)

    setup.runtime.start = unconfirmed  # type: ignore[method-assign]
    report = setup.coordinator().run_once()
    execution = setup.store.get("e1")
    assert execution is not None
    assert (report.code, execution.state, execution.attempt) == (2, S.FAILED, 1)
    assert S.RETRY_WAIT not in states(setup)
    assert setup.agent.runs == 0
    (container,) = resources(setup, ResourceKind.CONTAINER)
    assert container.status is R.QUARANTINED


def test_a_policy_violation_while_publishing_is_never_retried(setup: Setup) -> None:
    from ilmar.adapters.github.client import CrossOriginRedirectError
    from ilmar.core.effects import NEVER_TRANSIENT
    from ilmar.domain.errors import PolicyViolationError

    assert issubclass(CrossOriginRedirectError, PolicyViolationError)
    assert issubclass(CrossOriginRedirectError, NEVER_TRANSIENT)
    calls = []

    def redirected(*args: Any, **kwargs: Any) -> None:
        calls.append(1)
        raise CrossOriginRedirectError("redirect to another origin refused")

    setup.scm.publish_revision = redirected  # type: ignore[method-assign]
    report = setup.coordinator().run_once()
    execution = setup.store.get("e1")
    assert execution is not None
    assert (report.code, execution.state) == (1, S.FAILED)
    assert calls == [1]  # not tried again, not under retry-1
    assert setup.agent.runs == 1


def test_a_failed_checkout_is_not_retried(setup: Setup) -> None:
    def unsafe(*args: Any, **kwargs: Any) -> str:
        raise ProviderError("unsafe path in tree")

    setup.scm.checkout = unsafe  # type: ignore[method-assign]
    report = setup.coordinator().run_once()
    execution = setup.store.get("e1")
    assert execution is not None
    assert (report.code, execution.state, execution.attempt) == (1, S.FAILED, 1)
    assert setup.agent.runs == 0


def test_a_failed_task_is_never_started_again_automatically(setup: Setup) -> None:
    setup.agent.plan = [(AgentOutcome.FAILED, {})]
    coordinator = setup.coordinator()
    assert coordinator.run_once().code == 1
    assert setup.tasks.list_eligible() == (task("1"),)  # the claim was released
    again = coordinator.run_once()
    assert (again.code, again.summary) == (0, "no eligible task")
    assert setup.store.get("e2") is None
    assert setup.agent.runs == 1


def test_verification_infrastructure_failures_re_verify_the_same_candidate(
    setup: Setup, monkeypatch: pytest.MonkeyPatch
) -> None:
    original, calls, seen = Verifier.verify, [], []

    def flaky(self: Verifier, **kwargs: Any) -> Any:
        calls.append(kwargs["run"])
        seen.append(kwargs["candidate"])
        if len(calls) == 1:
            raise ProviderError("podman went away")
        return original(self, **kwargs)

    monkeypatch.setattr(Verifier, "verify", flaky)
    report = setup.coordinator().run_once()
    execution = setup.store.get("e1")
    assert execution is not None
    assert (report.code, execution.attempt) == (0, 1)  # no new attempt, no second agent run
    assert setup.agent.runs == 1
    assert calls == [1, 2]
    assert seen[0] == seen[1]  # exactly the same frozen candidate both times
    first = setup.store.operation(operation_key(K.VERIFY, "e1", 1, "run-1"))
    assert first is not None
    assert first.status is OperationStatus.FAILED
    assert setup.scm.pushes[0][2] == execution.candidate_revision


def test_repeated_verification_failures_block_without_rerunning_the_agent(
    setup: Setup, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(self: Verifier, **kwargs: Any) -> Any:
        raise ProviderError("podman went away")

    monkeypatch.setattr(Verifier, "verify", broken)
    report = setup.coordinator().run_once()
    execution = setup.store.get("e1")
    assert execution is not None
    assert (report.code, execution.state, execution.attempt) == (2, S.VERIFYING, 1)
    assert "frozen candidate is kept" in report.results[0].detail
    assert setup.agent.runs == 1
    assert setup.scm.pushes == []


def test_repeated_push_failures_block_without_rerunning_the_agent(setup: Setup) -> None:
    def never_sent(*args: Any, **kwargs: Any) -> None:
        raise ProviderError("timeout before sending")

    setup.scm.publish_revision = never_sent  # type: ignore[method-assign]
    report = setup.coordinator().run_once()
    execution = setup.store.get("e1")
    assert execution is not None
    assert (report.code, execution.state, execution.attempt) == (2, S.PUBLISHING, 1)
    assert setup.agent.runs == 1
    steps = [o.key for o in setup.store.operations("e1") if o.kind is K.PUBLISH_REVISION]
    assert steps == [
        "publish-revision:e1:1",
        "publish-revision:e1:1:retry-1",
        "publish-revision:e1:1:retry-2",
    ]
    assert setup.store.unfinished_operations() == ()


def test_no_change_request_is_opened_when_the_branch_moved_after_the_push(setup: Setup) -> None:
    original = setup.scm.publish_revision

    def then_someone_pushes(*args: Any, **kwargs: Any) -> None:
        original(*args, **kwargs)
        setup.scm._branches[(REPO, kwargs["branch"])] = setup.scm.seed(REPO, "x", {"a": "b"})

    setup.scm.publish_revision = then_someone_pushes  # type: ignore[method-assign]
    report = setup.coordinator().run_once()
    assert report.code == 2
    detail = report.results[0].detail
    assert "not the verified candidate" in detail or "moved after the push" in detail
    assert resources(setup, ResourceKind.CHANGE_REQUEST) == []


def test_the_head_is_checked_again_right_before_the_change_request(setup: Setup) -> None:
    original, reads = setup.scm.branch_head, []

    def moving(repository: Any, branch: str) -> Any:
        reads.append(branch)
        head = original(repository, branch)
        if branch.startswith("ilmar/") and len(reads) == 3:  # the read after the push
            return "f" * 40
        return head

    setup.scm.branch_head = moving  # type: ignore[method-assign]
    report = setup.coordinator().run_once()
    assert report.code == 2
    assert resources(setup, ResourceKind.CHANGE_REQUEST) == []


def test_a_branch_that_moves_while_the_change_request_opens_is_not_complete(
    setup: Setup,
) -> None:
    original = setup.scm.upsert_change_request

    def racing(spec: Any) -> Any:
        request = original(spec)
        setup.scm._branches[(REPO, spec.source_branch)] = setup.scm.seed(REPO, "x", {"a": "b"})
        return request

    setup.scm.upsert_change_request = racing  # type: ignore[method-assign]
    report = setup.coordinator().run_once()
    execution = setup.store.get("e1")
    assert execution is not None
    assert (report.code, execution.state) == (2, S.PUBLISHING)  # not called complete
    (request,) = resources(setup, ResourceKind.CHANGE_REQUEST)
    assert request.status is R.ACTIVE  # but the change request is tracked


def test_an_interrupted_claim_release_after_a_merge_is_resumed(setup: Setup) -> None:
    from ilmar.domain.changes import ChangeRequest

    coordinator = setup.coordinator()
    coordinator.run_once()
    (resource,) = resources(setup, ResourceKind.CHANGE_REQUEST)
    upsert = setup.store.operation(operation_key(K.UPSERT_CHANGE_REQUEST, "e1", 1))
    assert upsert is not None
    request = ChangeRequest(REPO, resource.identity, upsert.result["url"], resource.name, "main")
    setup.scm.finish_change_request(request, merged=True)
    original, calls = setup.tasks.release, []

    def flaky(ref: Any) -> None:
        calls.append(1)
        if len(calls) == 1:
            raise ProviderError("tracker unavailable")
        original(ref)

    setup.tasks.release = flaky  # type: ignore[method-assign]
    interrupted = coordinator.run_once()  # reported, and it does not abort the pass
    assert interrupted.code == 2
    assert "ProviderError" in interrupted.summary
    (half,) = resources(setup, ResourceKind.CHANGE_REQUEST)
    assert half.status is R.RELEASING
    report = coordinator.run_once()  # a repeatable release does not block the pass
    (done,) = resources(setup, ResourceKind.CHANGE_REQUEST)
    assert (done.status, done.note) == (R.RELEASED, "merged: task complete")
    assert report.completed_tasks == ("e1",)
    assert setup.tasks.list_eligible() == (task("1"),)
