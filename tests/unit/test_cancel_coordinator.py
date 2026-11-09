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
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

from coordinator_support import Setup, make_setup, task
from ilmar.core import cancel_inbox as fs
from ilmar.core.worker import Outcome
from ilmar.domain.agent import AgentOutcome, AgentRequest, AgentResult
from ilmar.domain.errors import ContainmentError
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
        assert isinstance(stop, threading.Event)
        stopped = stop.wait(self.patience)  # real time: the watcher looks every 0.05 s
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


BAD = {
    "symlink-to-a-valid-request": _symlink_to_valid,
    "fifo": _fifo,
    "wrong-execution-inside": _wrong_execution_inside,
    "mode-0644": _open_mode,
    "non-canonical": _non_canonical,
    "another-executions-request": _another_executions_request,
}


@pytest.mark.parametrize("case", sorted(BAD))
def test_the_watcher_never_interrupts_on_a_bad_entry(
    setup: Setup, waiting: WaitingAgent, tmp_path: Path, case: str
) -> None:
    waiting.submit_for = None
    waiting.patience = 1.0  # 20 looks by the watcher
    os.close(fs.open_inbox(setup.layout.coordinator_root, create=True))
    waiting.before = lambda: BAD[case](setup, tmp_path)
    report = setup.coordinator(watch_interval_seconds=0.05).run_once()
    stop = waiting.stops[0]
    assert stop is not None
    assert not stop.is_set()  # the agent ran to completion
    assert requests(setup) == []  # nothing recorded for e1
    assert report.results[0].outcome is Outcome.PUBLISHED
    if case == "symlink-to-a-valid-request":
        assert (tmp_path / "elsewhere.json").exists()  # its target is never touched
    if case != "another-executions-request":
        assert "inbox:" in report.results[0].detail  # moved aside and reported
        assert os.listdir(inbox(setup) / fs.REJECTED)
    else:
        assert (inbox(setup) / "e2.json").exists()  # not this execution's to touch
    check_i11(setup)


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
