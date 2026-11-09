# SPDX-License-Identifier: Apache-2.0
"""Cancellation in the coordinator (#16 part 3, ADR-0025 §3 to §5).

One test per row of ADR-0025 §3's table, with the events, the claim and the
ledger checked; the too-late path publishes exactly once; a cancelled task is
never picked again; the watcher stops a running agent only for a valid
request for its own execution, and never for a bad entry.
"""

from __future__ import annotations

import os
import threading
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

from coordinator_support import Setup, make_setup, task
from ilmar.core import cancel_inbox as fs
from ilmar.core.effects import DeterministicError, TransientError
from ilmar.core.ownership import Ownership
from ilmar.core.worker import CANCEL_TRIES, Outcome
from ilmar.domain.agent import AgentOutcome, AgentRequest, AgentResult
from ilmar.domain.errors import CandidateRejectedError, ContainmentError, FencingError
from ilmar.domain.event_history import verify
from ilmar.domain.events import Event, EventType
from ilmar.domain.execution import ExecutionState
from ilmar.domain.ledger import (
    Commit,
    Fence,
    OperationKind,
    OperationStatus,
    ResourceKind,
    ResourceStatus,
    operation_key,
)
from ilmar.domain.workspace import ContainerHandle, StopSignal
from ilmar.ports.container_runtime import ContainerRuntime

from helpers import at

S = ExecutionState
K = OperationKind
ST = OperationStatus
R = ResourceStatus
FIXED = {"src/app.py": "print('fixed')\n"}


@pytest.fixture
def setup(tmp_path: Path) -> Setup:
    return make_setup(tmp_path)


# -- helpers --------------------------------------------------------------------------


def inbox(setup: Setup) -> Path:
    return setup.layout.coordinator_root.joinpath(*fs.INBOX_PARTS)


def submit(setup: Setup, execution_id: str = "e1", actor: str = "alice") -> fs.CancelRequest:
    fd = fs.open_inbox(setup.layout.coordinator_root, create=True)
    try:
        return fs.submit(fd, execution_id, actor, now=at(5)).request
    finally:
        os.close(fd)


def on_commit(setup: Setup, when: Callable[[Commit], bool], action: Callable[[], object]) -> None:
    """Run ``action`` once, right after the first commit for which ``when`` holds."""
    original = setup.store.commit
    done: list[bool] = []

    def commit(fence: Fence, changes: Commit, *, context: Any = None) -> None:
        original(fence, changes, context=context)
        if not done and when(changes):
            done.append(True)
            action()

    setup.store.commit = commit  # type: ignore[method-assign]


def entering(state: ExecutionState) -> Callable[[Commit], bool]:
    def test(changes: Commit) -> bool:
        return any(t.to_state is state for _, t in (*changes.created, *changes.applied))

    return test


def operation(kind: OperationKind, status: OperationStatus) -> Callable[[Commit], bool]:
    def test(changes: Commit) -> bool:
        return any(o.kind is kind and o.status is status for o in changes.operations)

    return test


def requests(setup: Setup, execution_id: str = "e1") -> list[Event]:
    return [e for e in setup.store.events(execution_id=execution_id)
            if e.type is EventType.EXECUTION_CANCEL_REQUESTED]  # fmt: skip


def decisions(setup: Setup, execution_id: str = "e1") -> list[str]:
    return [str(e.data["decision"]) for e in requests(setup, execution_id)]


def states(setup: Setup, execution_id: str = "e1") -> list[ExecutionState]:
    return [t.to_state for t in setup.store.history(execution_id)]


def kinds(setup: Setup, execution_id: str = "e1") -> set[OperationKind]:
    return {o.kind for o in setup.store.operations(execution_id)}


def check_i11(setup: Setup) -> None:
    executions = setup.store.list_in_states(set(ExecutionState))
    from ilmar.domain.event_history import StoredState

    stored = StoredState(
        executions=executions,
        operations=[o for e in executions for o in setup.store.operations(e.id)],
        resources=setup.store.resources(),
        transitions={e.id: len(setup.store.history(e.id)) for e in executions},
        evidence={e.id: len(setup.store.evidence(e.id)) for e in executions},
    )
    report = verify(setup.store.events(), stored)
    assert report.failures == (), report.failures


def check_i12(setup: Setup, execution_id: str = "e1") -> None:
    """ADR-0025 I12: once accepted, no agent run, freeze, verification run or first
    push starts; every sandbox ends released or quarantined; the execution ends
    CANCELLED (BLOCKED with quarantine)."""
    events = setup.store.events(execution_id=execution_id)
    accepted = [e.seq or 0 for e in events if e.type is EventType.EXECUTION_CANCEL_REQUESTED
                and e.data.get("decision") == "accepted"]  # fmt: skip
    assert accepted, "no accepted cancellation"
    forbidden = (K.RUN_AGENT, K.FREEZE_CANDIDATE, K.VERIFY, K.PUBLISH_REVISION)
    started = [
        e.operation
        for e in events
        if e.type is EventType.OPERATION_INTENDED
        and (e.seq or 0) > accepted[0]
        and any(str(e.operation).startswith(k.value + ":") for k in forbidden)
    ]
    assert started == [], started
    execution = setup.store.get(execution_id)
    assert execution is not None
    assert execution.state is S.CANCELLED
    for row in setup.store.resources(execution_id=execution_id):
        if row.kind is ResourceKind.CONTAINER:
            assert row.status in (R.RELEASED, R.QUARANTINED), row
    assert setup.scm.pushes == []
    assert not setup.store.unfinished_operations()


def cancelled_cleanly(setup: Setup, *, claimed: bool = True) -> None:
    """The common ending: one accepted request recorded, CANCELLED with the code,
    the claim released (if held) with one note, nothing left in the inbox."""
    assert decisions(setup) == ["accepted"]
    (event,) = requests(setup)
    assert (event.actor.ref, event.reason) == ("alice", "cancel-requested")
    execution = setup.store.get("e1")
    assert execution is not None
    assert execution.state is S.CANCELLED
    into = [
        e
        for e in setup.store.events(execution_id="e1")
        if e.type is EventType.EXECUTION_TRANSITIONED and e.data.get("to") == "cancelled"
    ]
    assert [e.reason for e in into] == ["cancel-requested"]
    release = setup.store.operation(operation_key(K.RELEASE_TASK, "e1", execution.attempt,
                                                  "cancelled"))  # fmt: skip
    if claimed:
        assert release is not None
        assert release.status is ST.SUCCEEDED
        assert setup.tasks.list_eligible() == (task("1"),)  # released on the tracker
        assert len(setup.tasks.updates(task("1").ref)) == 1  # one note
    else:
        assert release is None
        assert K.CLAIM_TASK not in kinds(setup)
    assert [n for n in os.listdir(inbox(setup)) if n != fs.REJECTED] == []
    assert setup.runtime._containers == {}
    check_i11(setup)
    check_i12(setup)


def never_picked_again(setup: Setup) -> None:
    report = setup.coordinator().run_once()
    assert report.summary == "no eligible task"
    assert len(setup.store.list_in_states(set(ExecutionState))) == 1


# -- one test per row of ADR-0025 §3 ----------------------------------------------------


def test_queued_without_a_claim_intent(setup: Setup) -> None:
    on_commit(setup, entering(S.QUEUED), lambda: submit(setup))
    report = setup.coordinator().run_once()
    (result,) = report.results
    assert (result.outcome, result.code, report.code) == (Outcome.CANCELLED, "cancel-requested", 0)
    assert states(setup) == [S.QUEUED, S.CANCELLED]
    cancelled_cleanly(setup, claimed=False)
    assert setup.tasks.updates(task("1").ref) == ()
    never_picked_again(setup)


def test_claimed(setup: Setup) -> None:
    on_commit(setup, entering(S.CLAIMED), lambda: submit(setup))
    setup.coordinator().run_once()
    assert states(setup) == [S.QUEUED, S.CLAIMED, S.CANCELLED]
    cancelled_cleanly(setup)
    never_picked_again(setup)


def test_retry_wait(setup: Setup) -> None:
    setup.agent.plan = [(AgentOutcome.TIMED_OUT, {}), (AgentOutcome.COMPLETED, FIXED)]
    on_commit(setup, entering(S.RETRY_WAIT), lambda: submit(setup))
    setup.coordinator().run_once()
    assert states(setup)[-2:] == [S.RETRY_WAIT, S.CANCELLED]
    assert setup.agent.runs == 1  # no new attempt
    assert setup.layout.attempt_dir("e1", 1).exists()  # earlier attempts' workspaces stay
    cancelled_cleanly(setup)


def test_provisioning(setup: Setup) -> None:
    on_commit(setup, entering(S.PROVISIONING), lambda: submit(setup))
    setup.coordinator().run_once()
    assert states(setup)[-2:] == [S.PROVISIONING, S.CANCELLED]
    assert K.START_CONTAINER not in kinds(setup)
    assert setup.runtime.started == []  # no sandbox
    cancelled_cleanly(setup)


def test_provisioning_after_the_attempt_directory(setup: Setup) -> None:
    on_commit(setup, operation(K.CREATE_WORKSPACE, ST.SUCCEEDED), lambda: submit(setup))
    setup.coordinator().run_once()
    assert K.CHECKOUT_BASE not in kinds(setup)  # the next step never starts
    assert setup.layout.attempt_dir("e1", 1).exists()  # workspaces stay
    cancelled_cleanly(setup)


def test_running_agent_before_the_sandbox(setup: Setup) -> None:
    on_commit(setup, entering(S.RUNNING_AGENT), lambda: submit(setup))
    setup.coordinator().run_once()
    assert setup.runtime.started == []
    assert setup.agent.runs == 0
    cancelled_cleanly(setup)


def test_running_agent_with_a_started_sandbox_never_runs_the_agent(setup: Setup) -> None:
    on_commit(setup, operation(K.START_CONTAINER, ST.SUCCEEDED), lambda: submit(setup))
    setup.coordinator().run_once()
    assert len(setup.runtime.started) == 1
    assert setup.agent.runs == 0
    assert K.RUN_AGENT not in kinds(setup)
    (sandbox,) = [r for r in setup.store.resources(execution_id="e1")
                  if r.kind is ResourceKind.CONTAINER]  # fmt: skip
    assert sandbox.status is R.RELEASED  # the journaled, ownership-verified stop
    stop = setup.store.operation(operation_key(K.STOP_CONTAINER, "e1", 1, sandbox.name))
    assert stop is not None
    assert stop.status is ST.SUCCEEDED
    cancelled_cleanly(setup)


def test_running_agent_with_a_recorded_run_is_never_frozen(setup: Setup) -> None:
    """The agent completed, but the request arrived during the run: not frozen."""

    def request() -> None:
        submit(setup)

    setup.agent.during = request  # the default watcher interval never fires
    setup.coordinator().run_once()
    run = setup.store.operation(operation_key(K.RUN_AGENT, "e1", 1))
    assert run is not None
    assert (run.status, run.result["outcome"]) == (ST.SUCCEEDED, "completed")
    assert K.FREEZE_CANDIDATE not in kinds(setup)
    cancelled_cleanly(setup)


class WaitingAgent:
    """Works until the stop signal, like a long agent run in a real sandbox."""

    def __init__(self, setup: Setup, *, submit_for: str | None = "e1") -> None:
        self.setup = setup
        self.submit_for = submit_for
        self.stops: list[StopSignal | None] = []
        self.runs = 0
        self.before: Callable[[], None] | None = None
        self.patience = 5.0  # seconds to wait for the stop signal
        self.submitted_at = 0.0
        self.stopped_at = 0.0

    @property
    def name(self) -> str:
        return "waiting"

    def run(
        self,
        runtime: ContainerRuntime,
        container: ContainerHandle,
        request: AgentRequest,
        *,
        stop: StopSignal | None = None,
    ) -> AgentResult:
        self.runs += 1
        self.stops.append(stop)
        if self.before is not None:
            self.before()
        if self.submit_for is not None:
            submit(self.setup, self.submit_for)
        self.submitted_at = time.monotonic()
        assert isinstance(stop, threading.Event)
        stopped = stop.wait(self.patience)  # real time: the watcher looks every 0.05 s
        self.stopped_at = time.monotonic()
        if not stopped:
            runtime.run(container, ["write", "src/app.py", FIXED["src/app.py"]],
                        timeout_seconds=10)  # fmt: skip
            return AgentResult(AgentOutcome.COMPLETED, "done", 0)
        result = runtime.run(container, ["sleep", "30"], timeout_seconds=60, stop=stop)
        assert result.cancelled  # the runtime removed the whole sandbox, as on a timeout
        return AgentResult(AgentOutcome.CANCELLED, "stopped", None)


@pytest.fixture
def waiting(setup: Setup) -> Iterator[WaitingAgent]:
    agent = WaitingAgent(setup)
    original = setup.components.agent_runner
    setup.components.agent_runner = agent
    yield agent
    setup.components.agent_runner = original


def test_running_agent_in_progress_is_stopped_by_the_watcher(
    setup: Setup, waiting: WaitingAgent
) -> None:
    report = setup.coordinator(watch_interval_seconds=0.05).run_once()
    assert report.results[0].outcome is Outcome.CANCELLED
    assert waiting.stops[0] is not None
    assert waiting.stops[0].is_set()
    # Positive control: stopped within the watcher interval (plus scheduling slack).
    assert waiting.stopped_at - waiting.submitted_at < 0.05 + 1.0
    run = setup.store.operation(operation_key(K.RUN_AGENT, "e1", 1))
    assert run is not None
    assert (run.status, run.result["outcome"]) == (ST.SUCCEEDED, "cancelled")
    assert K.FREEZE_CANDIDATE not in kinds(setup)
    assert setup.layout.agent_workspace("e1", 1).exists()  # kept for inspection
    cancelled_cleanly(setup)


def test_verifying_before_a_run(setup: Setup) -> None:
    on_commit(setup, entering(S.VERIFYING), lambda: submit(setup))
    setup.coordinator().run_once()
    assert K.VERIFY not in kinds(setup)
    assert setup.store.evidence("e1") == ()
    execution = setup.store.get("e1")
    assert execution is not None
    assert execution.candidate_revision is not None  # the candidate is kept
    cancelled_cleanly(setup)


def test_verifying_a_run_in_progress_finishes_and_its_evidence_is_kept(setup: Setup) -> None:
    on_commit(setup, operation(K.VERIFY, ST.INTENDED), lambda: submit(setup))
    setup.coordinator().run_once()
    (run,) = [o for o in setup.store.operations("e1") if o.kind is K.VERIFY]
    assert run.status is ST.SUCCEEDED  # bounded: it finished
    assert len(setup.store.evidence("e1")) == 1  # kept
    assert K.PUBLISH_REVISION not in kinds(setup)
    cancelled_cleanly(setup)


def test_publishing_before_the_first_push(setup: Setup) -> None:
    on_commit(setup, entering(S.PUBLISHING), lambda: submit(setup))
    setup.coordinator().run_once()
    assert setup.scm.pushes == []
    assert K.PUBLISH_REVISION not in kinds(setup)
    assert len(setup.store.evidence("e1")) == 1
    cancelled_cleanly(setup)


def test_publishing_after_a_push_intent_is_too_late_and_publishes_exactly_once(
    setup: Setup,
) -> None:
    on_commit(setup, operation(K.PUBLISH_REVISION, ST.INTENDED), lambda: submit(setup))
    report = setup.coordinator().run_once()
    assert report.results[0].outcome is Outcome.PUBLISHED
    assert decisions(setup) == ["too-late"]
    assert len(setup.scm.pushes) == 1  # exactly once (I1)
    execution = setup.store.get("e1")
    assert execution is not None
    assert execution.state is S.SUCCEEDED
    assert [n for n in os.listdir(inbox(setup)) if n != fs.REJECTED] == []
    assert setup.tasks.list_eligible() == ()  # claimed while the change request is open
    check_i11(setup)


def test_a_failure_recorded_first_stays_a_failure(setup: Setup) -> None:
    """Precedence: the first transition committed wins (ADR-0025 §3)."""
    setup.agent.plan = [(AgentOutcome.FAILED, {})]
    on_commit(setup, entering(S.FAILED), lambda: submit(setup))
    setup.coordinator().run_once()
    execution = setup.store.get("e1")
    assert execution is not None
    assert execution.state is S.FAILED
    assert decisions(setup) == ["already-terminal"]
    assert setup.store.operation(operation_key(K.RELEASE_TASK, "e1", 1, "failed")) is not None
    assert [n for n in os.listdir(inbox(setup)) if n != fs.REJECTED] == []
    check_i11(setup)


def test_an_unremovable_sandbox_keeps_the_claim_and_blocks(setup: Setup) -> None:
    def refuse(handle: Any) -> None:
        raise ContainmentError("still running", container_id=handle.id, name=handle.name)

    on_commit(setup, operation(K.START_CONTAINER, ST.SUCCEEDED), lambda: submit(setup))
    setup.runtime.stop_hook = refuse
    report = setup.coordinator().run_once()
    (result,) = report.results
    assert (result.outcome, report.code) == (Outcome.BLOCKED, 2)
    execution = setup.store.get("e1")
    assert execution is not None
    assert execution.state is S.CANCELLED  # a cancellation never turns into a failure
    (sandbox,) = [r for r in setup.store.resources(execution_id="e1")
                  if r.kind is ResourceKind.CONTAINER]  # fmt: skip
    assert sandbox.status is R.QUARANTINED
    assert setup.tasks.list_eligible() == ()  # the claim is kept for a human
    assert setup.store.operation(operation_key(K.RELEASE_TASK, "e1", 1, "cancelled")) is None
    assert setup.agent.runs == 0
    check_i11(setup)
    again = setup.coordinator().run_once()  # still blocked, never re-run
    assert [r.outcome for r in again.results] == [Outcome.BLOCKED]
    assert setup.agent.runs == 0


def test_a_transient_failure_after_acceptance_ends_cancelled_not_retried(setup: Setup) -> None:
    """ADR-0025 §5: a pending cancellation ends the execution instead of a new attempt."""
    setup.agent.plan = [(AgentOutcome.TIMED_OUT, {}), (AgentOutcome.COMPLETED, FIXED)]

    def accept_then_time_out() -> None:
        submit(setup)

    setup.agent.during = accept_then_time_out  # the scripted run then reports a timeout
    setup.coordinator().run_once()
    assert setup.agent.runs == 1
    assert S.RETRY_WAIT not in states(setup)
    cancelled_cleanly(setup)


# -- the watcher: valid requests only -----------------------------------------------------


def _symlink_to_valid(setup: Setup, tmp: Path) -> None:
    target = tmp / "elsewhere.json"
    target.write_text(fs.encode({"schema": fs.SCHEMA, "request": "q-" + "a" * 32,
                                 "execution": "e1", "actor": "alice",
                                 "written_at": "2026-11-09T10:00:00Z"}))  # fmt: skip
    target.chmod(0o600)
    (inbox(setup) / "e1.json").symlink_to(target)


def _fifo(setup: Setup, tmp: Path) -> None:
    os.mkfifo(inbox(setup) / "e1.json", 0o600)


def _wrong_execution_inside(setup: Setup, tmp: Path) -> None:
    path = inbox(setup) / "e1.json"
    path.write_text(fs.encode({"schema": fs.SCHEMA, "request": "q-" + "a" * 32,
                               "execution": "e2", "actor": "alice",
                               "written_at": "2026-11-09T10:00:00Z"}))  # fmt: skip
    path.chmod(0o600)


def _open_mode(setup: Setup, tmp: Path) -> None:
    path = inbox(setup) / "e1.json"
    path.write_text(fs.encode({"schema": fs.SCHEMA, "request": "q-" + "a" * 32,
                               "execution": "e1", "actor": "alice",
                               "written_at": "2026-11-09T10:00:00Z"}))  # fmt: skip
    path.chmod(0o644)


def _non_canonical(setup: Setup, tmp: Path) -> None:
    path = inbox(setup) / "e1.json"
    path.write_text('{ "schema": "ilmar.cancel-request/1" }')
    path.chmod(0o600)


def _another_executions_request(setup: Setup, tmp: Path) -> None:
    submit(setup, "e2")


VALID_FIELDS = {"schema": fs.SCHEMA, "request": "q-" + "a" * 32, "execution": "e1",
                "actor": "alice", "written_at": "2026-11-09T10:00:00Z"}  # fmt: skip


def _raw(content: bytes) -> Callable[[Setup, Path], None]:
    def place(setup: Setup, tmp: Path) -> None:
        path = inbox(setup) / "e1.json"
        path.write_bytes(content)
        path.chmod(0o600)

    return place


def _directory(setup: Setup, tmp: Path) -> None:
    (inbox(setup) / "e1.json").mkdir(mode=0o700)


def _hard_link_without_temporary(setup: Setup, tmp: Path) -> None:
    elsewhere = setup.layout.coordinator_root / "kept-elsewhere.json"
    elsewhere.write_text(fs.encode(VALID_FIELDS))
    elsewhere.chmod(0o600)
    os.link(elsewhere, inbox(setup) / "e1.json")


CANONICAL = fs.encode(VALID_FIELDS).encode()
BAD: dict[str, Callable[[Setup, Path], None]] = {
    "symlink-to-a-valid-request": _symlink_to_valid,
    "fifo": _fifo,
    "directory": _directory,
    "hard-link-without-temporary": _hard_link_without_temporary,
    "mode-0644": _open_mode,
    "oversized": _raw(fs.encode({**VALID_FIELDS, "actor": "a" * 2000}).encode()),
    "truncated": _raw(CANONICAL[: len(CANONICAL) // 2]),
    "non-utf8": _raw(b"\xff\xfe" + CANONICAL),
    "non-canonical": _non_canonical,
    "duplicate-key": _raw(CANONICAL[:-1] + b',"actor":"mallory"}'),
    "extra-key": _raw(fs.encode({**VALID_FIELDS, "force": "yes"}).encode()),
    "wrong-schema": _raw(fs.encode({**VALID_FIELDS, "schema": "ilmar.cancel-request/9"}).encode()),
    "malformed-request-id": _raw(fs.encode({**VALID_FIELDS, "request": "q-xyz"}).encode()),
    "invalid-actor": _raw(fs.encode({**VALID_FIELDS, "actor": "Mallory <m@x>"}).encode()),
    "deep-nesting": _raw(b"[" * 500 + b"]" * 500),
    "huge-number": _raw(CANONICAL[:-1] + b',"n":' + b"9" * 900 + b"}"),
    "nan": _raw(CANONICAL[:-1] + b',"n":NaN}'),
    "wrong-execution-inside": _wrong_execution_inside,
    "another-executions-request": _another_executions_request,
}


@pytest.mark.parametrize("case", sorted(BAD))
def test_the_watcher_never_interrupts_on_a_bad_entry(
    setup: Setup, waiting: WaitingAgent, tmp_path: Path, case: str
) -> None:
    waiting.submit_for = None
    waiting.patience = 0.6  # a dozen looks by the watcher
    os.close(fs.open_inbox(setup.layout.coordinator_root, create=True))
    waiting.before = lambda: BAD[case](setup, tmp_path)
    report = setup.coordinator(watch_interval_seconds=0.05).run_once()
    stop = waiting.stops[0]
    assert stop is not None
    assert not stop.is_set()  # the agent ran to completion
    assert requests(setup) == []  # nothing recorded for e1
    assert report.results[0].outcome is Outcome.PUBLISHED
    if case == "symlink-to-a-valid-request":
        assert fs.SCHEMA in (tmp_path / "elsewhere.json").read_text()  # target untouched
    if case == "hard-link-without-temporary":
        kept = setup.layout.coordinator_root / "kept-elsewhere.json"
        assert kept.read_text() == fs.encode(VALID_FIELDS)  # the other link is untouched
    if case != "another-executions-request":
        assert "inbox:" in report.results[0].detail  # moved aside and reported
        assert os.listdir(inbox(setup) / fs.REJECTED)
        assert not os.path.lexists(inbox(setup) / "e1.json")
    else:
        assert (inbox(setup) / "e2.json").exists()  # not this execution's to touch
    check_i11(setup)


def test_the_watcher_never_interrupts_through_a_symlinked_inbox(
    setup: Setup, waiting: WaitingAgent, tmp_path: Path
) -> None:
    """``requests/cancel`` replaced by a symlink to a directory holding a valid
    request: never followed, nothing recorded, the target untouched, reported."""
    waiting.submit_for = None
    waiting.patience = 0.6
    os.close(fs.open_inbox(setup.layout.coordinator_root, create=True))
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir(mode=0o700)
    planted = elsewhere / "e1.json"
    planted.write_text(fs.encode(VALID_FIELDS))
    planted.chmod(0o600)

    def swap_directory() -> None:
        os.rmdir(inbox(setup))
        inbox(setup).symlink_to(elsewhere)

    waiting.before = swap_directory
    report = setup.coordinator(watch_interval_seconds=0.05).run_once()
    stop = waiting.stops[0]
    assert stop is not None
    assert not stop.is_set()
    assert requests(setup) == []
    assert report.results[0].outcome is Outcome.PUBLISHED
    assert "unsafe" in report.results[0].detail
    assert planted.read_text() == fs.encode(VALID_FIELDS)
    assert sorted(os.listdir(elsewhere)) == ["e1.json"]
    check_i11(setup)


def test_the_watcher_never_interrupts_on_a_foreign_owner(
    setup: Setup, waiting: WaitingAgent
) -> None:
    """Simulated by the injected owner check: the inbox belongs to someone else."""
    waiting.submit_for = None
    waiting.patience = 0.6

    def plant() -> None:
        submit(setup)  # a valid request, but in an inbox that is not ours to trust

    waiting.before = plant
    coordinator = setup.coordinator(watch_interval_seconds=0.05)
    coordinator._worker.cancellations._uid = os.geteuid() + 1
    report = coordinator.run_once()
    stop = waiting.stops[0]
    assert stop is not None
    assert not stop.is_set()
    assert requests(setup) == []
    assert report.results[0].outcome is Outcome.PUBLISHED
    assert "unsafe" in report.results[0].detail
    assert (inbox(setup) / "e1.json").exists()  # nothing touched in a foreign inbox
    check_i11(setup)


def test_queued_with_a_recorded_claim_intent_settles_the_claim_then_releases(
    setup: Setup,
) -> None:
    """ADR-0025 §3, QUEUED: a claim intent recorded (a crash before its effect) is
    settled first, since claiming is repeatable, and then the claim is released."""
    on_commit(setup, operation(K.CLAIM_TASK, ST.INTENDED),
              lambda: (_ for _ in ()).throw(Crash()))  # fmt: skip
    with pytest.raises(Crash):
        setup.coordinator().run_once()
    execution = setup.store.get("e1")
    assert execution is not None
    assert execution.state is S.QUEUED
    submit(setup)
    report = setup.coordinator().run_once()
    assert report.results[0].outcome is Outcome.CANCELLED
    claims = [o for o in setup.store.operations("e1") if o.kind is K.CLAIM_TASK]
    assert [o.status for o in claims] == [ST.SUCCEEDED]  # settled
    assert states(setup) == [S.QUEUED, S.CANCELLED]  # never CLAIMED, no other step
    cancelled_cleanly(setup)  # and released, with one note


def test_a_request_swapped_after_the_watcher_validated_it(
    setup: Setup, waiting: WaitingAgent
) -> None:
    """The worker records the request that stopped the run and deletes nothing it
    did not verify at its private name (ADR-0025 §2, §1a)."""
    original = setup.store.commit
    swapped: list[str] = []

    def commit(fence: Fence, changes: Commit, *, context: Any = None) -> None:
        stopped = any(o.kind is K.RUN_AGENT and o.status is ST.SUCCEEDED
                      for o in changes.operations)  # fmt: skip
        original(fence, changes, context=context)
        if stopped and not swapped:  # after the run, before the worker records the request
            staged = inbox(setup) / "staged"
            staged.write_text(fs.encode({"schema": fs.SCHEMA, "request": "q-" + "b" * 32,
                                         "execution": "e1", "actor": "mallory",
                                         "written_at": "2026-11-09T10:00:00Z"}))  # fmt: skip
            staged.chmod(0o600)
            os.replace(staged, inbox(setup) / "e1.json")
            swapped.append("q-" + "b" * 32)

    setup.store.commit = commit  # type: ignore[method-assign]
    report = setup.coordinator(watch_interval_seconds=0.05).run_once()
    (event,) = requests(setup)
    assert (event.data["decision"], event.actor.ref) == ("accepted", "alice")
    (kept,) = os.listdir(inbox(setup) / fs.REJECTED)
    assert swapped[0] in (inbox(setup) / fs.REJECTED / kept).read_text()  # preserved
    assert "resubmit" in report.results[0].detail
    assert report.results[0].outcome is Outcome.CANCELLED
    check_i11(setup)


# -- exception paths while a cancellation is pending (review of PR #78) -------------------


class Crash(BaseException):
    """A process death: nothing below the coordinator handles it."""


def failing(target: object, name: str, errors: list[BaseException]) -> list[str]:
    """Make ``target.name`` raise the next of ``errors`` before acting, then work."""
    original = getattr(target, name)
    calls: list[str] = []

    def wrapper(*args: Any, **kwargs: Any) -> Any:
        calls.append(name)
        if errors:
            raise errors.pop(0)
        return original(*args, **kwargs)

    setattr(target, name, wrapper)
    return calls


def sandbox_rows(setup: Setup) -> list[Any]:
    return [r for r in setup.store.resources(execution_id="e1") if r.kind is ResourceKind.CONTAINER]


def event_index(setup: Setup, predicate: Callable[[Event], bool]) -> int:
    events = setup.store.events(execution_id="e1")
    return next(i for i, e in enumerate(events) if predicate(e))


ENDING_ERRORS: dict[str, Callable[[], BaseException]] = {
    "transient": lambda: TransientError("tracker hiccup", code="transient-provider-error"),
    "deterministic": lambda: DeterministicError("refused", code="agent-failed"),
    "candidate-rejected": lambda: CandidateRejectedError("rejected"),
}


@pytest.mark.parametrize("kind", sorted(ENDING_ERRORS))
def test_an_error_in_the_ending_reruns_the_whole_ending_before_cancelled(
    setup: Setup, kind: str
) -> None:
    """Never FAILED, never a retry, and never CANCELLED before the sandbox is removed
    through the journaled, ownership-verified stop."""
    on_commit(setup, operation(K.START_CONTAINER, ST.SUCCEEDED), lambda: submit(setup))
    coordinator = setup.coordinator()
    calls = failing(coordinator._worker.effects, "remove_sandbox", [ENDING_ERRORS[kind]()])
    report = coordinator.run_once()
    assert report.results[0].outcome is Outcome.CANCELLED
    assert len(calls) == 2  # failed once, then the whole ending ran again
    assert S.FAILED not in states(setup)
    assert S.RETRY_WAIT not in states(setup)
    released = event_index(setup, lambda e: e.type is EventType.RESOURCE_CHANGED
                           and e.data.get("status") == "released")  # fmt: skip
    cancelled = event_index(setup, lambda e: e.type is EventType.EXECUTION_TRANSITIONED
                            and e.data.get("to") == "cancelled")  # fmt: skip
    assert released < cancelled  # cleanup first, then the transition
    cancelled_cleanly(setup)


@pytest.mark.parametrize("kind", sorted(ENDING_ERRORS))
def test_an_ending_that_keeps_failing_blocks_without_any_transition(
    setup: Setup, kind: str
) -> None:
    on_commit(setup, operation(K.START_CONTAINER, ST.SUCCEEDED), lambda: submit(setup))
    coordinator = setup.coordinator()
    errors = [ENDING_ERRORS[kind]() for _ in range(CANCEL_TRIES)]
    calls = failing(coordinator._worker.effects, "remove_sandbox", errors)
    report = coordinator.run_once()
    (result,) = report.results
    assert (result.outcome, report.code) == (Outcome.BLOCKED, 2)
    assert "cancellation could not complete yet" in result.detail
    assert len(calls) == CANCEL_TRIES
    execution = setup.store.get("e1")
    assert execution is not None
    assert execution.state is S.RUNNING_AGENT  # no FAILED, no RETRY_WAIT, no bare CANCELLED
    assert decisions(setup) == ["accepted"]
    (sandbox,) = sandbox_rows(setup)
    assert sandbox.status is R.ACTIVE  # not released, so not assumed gone
    assert setup.tasks.list_eligible() == ()  # the claim is held
    check_i11(setup)
    # The next pass completes the ending from durable state.
    assert setup.coordinator().run_once().results[0].outcome is Outcome.CANCELLED
    cancelled_cleanly(setup)


def test_a_crash_in_the_ending_is_completed_by_the_next_pass(setup: Setup) -> None:
    """Atomicity: the sandbox removal is journaled and the transition is one fenced
    commit; a death between them leaves a non-terminal execution that the next
    pass finishes without repeating the removal."""
    on_commit(setup, operation(K.START_CONTAINER, ST.SUCCEEDED), lambda: submit(setup))
    on_commit(setup, lambda c: any(r.status is R.RELEASED for r in c.resources),
              lambda: (_ for _ in ()).throw(Crash()))  # fmt: skip
    with pytest.raises(Crash):
        setup.coordinator().run_once()
    execution = setup.store.get("e1")
    assert execution is not None
    assert execution.state is S.RUNNING_AGENT
    stops = [o for o in setup.store.operations("e1") if o.kind is K.STOP_CONTAINER]
    assert [o.status for o in stops] == [ST.SUCCEEDED]
    check_i11(setup)
    setup.coordinator().run_once()
    assert [o.status for o in setup.store.operations("e1") if o.kind is K.STOP_CONTAINER] == [
        ST.SUCCEEDED
    ]
    cancelled_cleanly(setup)


def test_unconfirmed_removal_while_cancelling_keeps_the_claim_and_settles_honestly(
    setup: Setup,
) -> None:
    """A crash left the agent sandbox started (start-container unfinished). The
    removal cannot be confirmed: quarantined, CANCELLED, BLOCKED, the claim kept;
    the start is never settled as 'sandbox removed' and the run never starts."""
    original_start = setup.runtime.start

    def start_then_die(spec: Any) -> Any:
        original_start(spec)
        raise Crash

    setup.runtime.start = start_then_die  # type: ignore[method-assign]
    with pytest.raises(Crash):
        setup.coordinator().run_once()
    setup.runtime.start = original_start  # type: ignore[method-assign]
    submit(setup)

    def refuse(handle: Any) -> None:
        raise ContainmentError("still running", container_id=handle.id, name=handle.name)

    setup.runtime.stop_hook = refuse
    report = setup.coordinator().run_once()
    (result,) = report.results
    assert (result.outcome, report.code) == (Outcome.BLOCKED, 2)
    execution = setup.store.get("e1")
    assert execution is not None
    assert execution.state is S.CANCELLED
    (sandbox,) = sandbox_rows(setup)
    assert sandbox.status is R.QUARANTINED
    start = [o for o in setup.store.operations("e1") if o.kind is K.START_CONTAINER]
    assert [o.status in (ST.INTENDED, ST.AMBIGUOUS) for o in start] == [True]  # for the human
    observed = {e.data.get("observed") for e in setup.store.events(execution_id="e1")}
    assert "sandbox-removed" not in observed
    assert setup.agent.runs == 0
    assert setup.tasks.list_eligible() == ()  # the claim is kept
    assert setup.store.operation(operation_key(K.RELEASE_TASK, "e1", 1, "cancelled")) is None
    check_i11(setup)
    again = setup.coordinator().run_once()
    assert [r.outcome for r in again.results] == [Outcome.BLOCKED]
    assert setup.agent.runs == 0
    assert setup.tasks.list_eligible() == ()


# -- requests stranded after an execution became terminal (review of PR #78) ------------


def finished(setup: Setup, plan: list[Any] | None = None) -> ExecutionState:
    if plan is not None:
        setup.agent.plan = plan
    setup.coordinator().run_once()
    execution = setup.store.get("e1")
    assert execution is not None
    assert execution.state.is_terminal
    return execution.state


def snapshot(setup: Setup) -> tuple[Any, ...]:
    """Everything a stranded request may not change: rows, operations, transitions."""
    return (
        setup.store.get("e1"),
        tuple(setup.store.operations("e1")),
        tuple(setup.store.resources(execution_id="e1")),
        tuple(setup.store.history("e1")),
        tuple(setup.scm.pushes),
        setup.tasks.list_eligible(),
    )


@pytest.mark.parametrize(
    "plan",
    [None, [(AgentOutcome.FAILED, {})]],
    ids=["succeeded", "failed"],
)
def test_a_stranded_request_is_recorded_already_terminal_once_and_cleaned(
    setup: Setup, plan: list[Any] | None
) -> None:
    finished(setup, plan)
    before = snapshot(setup)
    events_before = len(setup.store.events(execution_id="e1"))
    submit(setup)  # it raced with the finish: the CLI saw a live execution
    report = setup.coordinator().run_once()
    assert (report.code, report.summary) == (0, "no eligible task")  # no result line
    assert decisions(setup) == ["already-terminal"]
    (event,) = requests(setup)
    assert (event.actor.ref, event.reason, event.epoch) == (
        "alice", "cancel-requested", setup.ownership.fence.epoch)  # fmt: skip
    assert len(setup.store.events(execution_id="e1")) == events_before + 1  # only that
    assert snapshot(setup) == before  # nothing else changed
    assert os.listdir(inbox(setup)) == []
    check_i11(setup)
    setup.coordinator().run_once()  # idempotent
    assert decisions(setup) == ["already-terminal"]


def test_a_second_request_after_a_cancellation_is_already_terminal(setup: Setup) -> None:
    on_commit(setup, entering(S.CLAIMED), lambda: submit(setup))
    setup.coordinator().run_once()
    before = snapshot(setup)
    submit(setup, actor="bob")
    setup.coordinator().run_once()
    assert decisions(setup) == ["accepted", "already-terminal"]
    assert snapshot(setup) == before
    assert len(setup.tasks.updates(task("1").ref)) == 1  # no second note
    assert os.listdir(inbox(setup)) == []
    check_i11(setup)


def test_a_stranded_invalid_entry_is_set_aside_and_nothing_recorded(setup: Setup) -> None:
    finished(setup)
    os.close(fs.open_inbox(setup.layout.coordinator_root, create=True))
    junk = inbox(setup) / "e1.json"
    junk.write_text('{"force":true}')
    junk.chmod(0o600)
    setup.coordinator().run_once()
    assert requests(setup) == []
    (kept,) = os.listdir(inbox(setup) / fs.REJECTED)
    assert (inbox(setup) / fs.REJECTED / kept).read_text() == '{"force":true}'
    assert [n for n in os.listdir(inbox(setup)) if n != fs.REJECTED] == []
    check_i11(setup)


def test_a_request_for_an_unknown_execution_is_left_alone(setup: Setup) -> None:
    finished(setup)
    submit(setup, "e404")
    setup.coordinator().run_once()
    assert (inbox(setup) / "e404.json").exists()
    assert setup.store.events(execution_id="e404") == ()


def test_a_stranded_request_waits_while_another_worker_holds_the_lease(setup: Setup) -> None:
    finished(setup)
    submit(setup)
    coordinator = setup.coordinator()
    with coordinator._worker.leases.acquire("e1"):
        report = coordinator.run_once()
    assert report.code == 0
    assert requests(setup) == []
    assert (inbox(setup) / "e1.json").exists()  # untouched without the lease
    setup.coordinator().run_once()
    assert decisions(setup) == ["already-terminal"]


def test_a_superseded_coordinator_records_no_stranded_request(setup: Setup) -> None:
    finished(setup)
    submit(setup)
    stale = setup.coordinator()
    setup.ownership.release()
    successor = Ownership.take(layout=setup.layout, store=setup.store, instance_id="inst",
                               now=at(99))  # fmt: skip
    try:
        with pytest.raises(FencingError):
            stale.run_once()
        assert requests(setup) == []
        assert (inbox(setup) / "e1.json").exists()
    finally:
        successor.release()


@pytest.mark.parametrize("window", ["validated", "recorded", "taken", "verified"])
def test_a_crash_while_settling_a_stranded_request_is_completed_once(
    setup: Setup, window: str
) -> None:
    """Atomicity: the record is one fenced commit and cleanup is rename-first, so a
    death in any window leaves a state the next pass completes exactly once."""
    finished(setup)
    request = submit(setup)
    coordinator = setup.coordinator()

    def die(name: str) -> None:
        if name == window:
            raise Crash

    coordinator._worker.cancellations._window = die
    with pytest.raises(Crash):
        coordinator.run_once()
    recorded = window != "validated"
    assert len(requests(setup)) == int(recorded)
    names = os.listdir(inbox(setup))
    assert names  # the entry (or its private name) is still there: never lost
    setup.coordinator().run_once()
    (event,) = requests(setup)
    assert (event.data["request"], event.data["decision"]) == (
        request.request_id, "already-terminal")  # fmt: skip
    assert os.listdir(inbox(setup)) == []
    check_i11(setup)


def test_a_cancellation_after_a_crash_at_start_removes_the_sandbox_before_cancelled(
    setup: Setup,
) -> None:
    """The fake-runtime twin of the real-Podman test: a crash right after the
    sandbox started; the ending removes it (journaled, ownership-verified),
    settles the start, then CANCELLED and the release."""
    original_start = setup.runtime.start

    def start_then_die(spec: Any) -> Any:
        original_start(spec)
        raise Crash

    setup.runtime.start = start_then_die  # type: ignore[method-assign]
    with pytest.raises(Crash):
        setup.coordinator().run_once()
    setup.runtime.start = original_start  # type: ignore[method-assign]
    assert len(setup.runtime._containers) == 1  # really started
    submit(setup)
    report = setup.coordinator().run_once()
    assert report.results[0].outcome is Outcome.CANCELLED
    stop_done = event_index(setup, lambda e: e.type is EventType.OPERATION_SUCCEEDED
                            and str(e.operation).startswith("stop-container:"))  # fmt: skip
    cancelled = event_index(setup, lambda e: e.type is EventType.EXECUTION_TRANSITIONED
                            and e.data.get("to") == "cancelled")  # fmt: skip
    assert stop_done < cancelled
    start = [o for o in setup.store.operations("e1") if o.kind is K.START_CONTAINER]
    assert [o.status for o in start] == [ST.FAILED]
    assert setup.agent.runs == 0
    cancelled_cleanly(setup)
