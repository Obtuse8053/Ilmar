# SPDX-License-Identifier: Apache-2.0
"""One coordinator execution on real Podman (#11b, ADR-0021).

Real: rootless Podman sandboxes, the egress proxy for the agent sandbox
(ADR-0015), per-job verification sandboxes (ADR-0018) and the SQLite store
with fencing (ADR-0020). Fakes: the task tracker and source control (#11d
runs live GitHub and Claude Code). The agent is a shell command inside the
real agent sandbox.

Needs ``ILMAR_TEST_PODMAN_IMAGE`` and ``ILMAR_TEST_PODMAN_PROXY_IMAGE``;
skips without them unless ``ILMAR_TEST_PODMAN_REQUIRED=1``.
"""

from __future__ import annotations

import dataclasses
import os
import time
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from coordinator_support import BASE_FILES, REPO, Clock, task
from ilmar.adapters.podman import PodmanRuntime
from ilmar.adapters.podman.runtime import EgressSettings
from ilmar.adapters.sqlite import SQLiteExecutionStore
from ilmar.core import cancel_inbox
from ilmar.core.components import AgentSandbox, Components, Target
from ilmar.core.coordinator import Coordinator
from ilmar.core.layout import WorkspaceLayout
from ilmar.core.naming import container_name, verification_container_name
from ilmar.core.ownership import Ownership
from ilmar.core.worker import Outcome
from ilmar.domain.agent import AgentOutcome, AgentRequest, AgentResult
from ilmar.domain.events import EventType
from ilmar.domain.execution import ExecutionState
from ilmar.domain.ledger import (
    OperationKind,
    OperationStatus,
    ResourceKind,
    ResourceStatus,
    operation_key,
)
from ilmar.domain.verification import Verdict, VerificationCommand, VerificationPolicy
from ilmar.domain.workspace import ContainerHandle, ContainerSpec, NetworkPolicy, StopSignal
from ilmar.ports.agent_runner import AgentRunner
from ilmar.ports.container_runtime import ContainerRuntime

from fakes import FakeSourceControl, FakeTaskProvider
from integration.podman_support import (
    IMAGE,
    PODMAN,
    PROXY_IMAGE,
    podman,
    remove_everything,
    require,
)

pytestmark = pytest.mark.integration

INSTANCE = "it-coord"
NAMES = (
    container_name(INSTANCE, "e1", 1),
    *(verification_container_name(INSTANCE, "e1", 1, 1, job) for job in ("tests", "sandbox")),
)


class ShellAgent:
    """An agent that edits the workspace with a shell command inside its real sandbox."""

    script = 'printf "print(\'fixed\')\\n" > src/app.py && [ -n "$HTTPS_PROXY" ]'

    @property
    def name(self) -> str:
        return "shell"

    def run(
        self,
        runtime: ContainerRuntime,
        container: ContainerHandle,
        request: AgentRequest,
        *,
        stop: StopSignal | None = None,
    ) -> AgentResult:
        done = runtime.run(container, ["sh", "-c", self.script], timeout_seconds=60)
        if done.exit_code != 0:
            return AgentResult(AgentOutcome.FAILED, done.stderr[-200:], done.exit_code)
        return AgentResult(AgentOutcome.COMPLETED, "fixed it", 0)


@pytest.fixture(autouse=True)
def _require() -> Iterator[None]:
    require(("podman", PODMAN), ("ILMAR_TEST_PODMAN_IMAGE", IMAGE), ("proxy image", PROXY_IMAGE))
    remove_everything(*NAMES)
    yield
    remove_everything(*NAMES)


def test_one_execution_end_to_end_on_real_sandboxes(tmp_path: Path) -> None:
    state = tmp_path.resolve() / "state"
    state.mkdir()
    layout = WorkspaceLayout.under(state)
    layout.workspaces_root.mkdir()
    scm = FakeSourceControl()
    scm.seed(REPO, "main", BASE_FILES)
    tasks = FakeTaskProvider((task("1"),))
    store = SQLiteExecutionStore(tmp_path / "executions.sqlite3")
    policy = VerificationPolicy(
        image=IMAGE,
        commands=(
            VerificationCommand(
                "fixed", ("sh", "-c", "grep -q fixed src/app.py"), timeout_seconds=60, job="tests"
            ),
            VerificationCommand(
                "no-network",
                ("sh", "-c", '[ "$(ls /sys/class/net)" = lo ] && [ -z "$HTTPS_PROXY" ]'),
                timeout_seconds=60,
                job="sandbox",
            ),
        ),
    )
    components = Components(
        instance_id=INSTANCE,
        layout=layout,
        task_provider=tasks,
        source_control=scm,
        container_runtime=PodmanRuntime(
            layout=layout, egress=EgressSettings(proxy_image=PROXY_IMAGE)
        ),
        agent_runner=ShellAgent(),
        execution_store=store,
        agent_sandbox=AgentSandbox(IMAGE, NetworkPolicy.ALLOWLIST, ("api.anthropic.com",)),
        target=Target(REPO, "main"),
        verification=policy,
    )
    clock = Clock()
    ids = iter(["e1"])
    with Ownership.take(layout=layout, store=store, instance_id=INSTANCE, now=clock()) as owner:
        coordinator = Coordinator.create(
            components,
            owner,
            max_attempts=3,
            agent_timeout_seconds=120,
            clock=clock,
            new_id=lambda: next(ids),
        )
        report = coordinator.run_once()
    assert (report.code, report.results[0].outcome) == (0, Outcome.PUBLISHED), report.summary
    execution = store.get("e1")
    assert execution is not None
    assert execution.state is ExecutionState.SUCCEEDED
    (evidence,) = store.evidence("e1")
    assert evidence.verdict is Verdict.PUBLISH
    assert [c.name for c in evidence.checks if not c.passed] == []
    (_, _branch, pushed) = scm.pushes[0]
    assert len(scm.pushes) == 1
    assert pushed == execution.candidate_revision == evidence.candidate_revision
    containers = [r for r in store.resources(execution_id="e1") if r.kind is ResourceKind.CONTAINER]
    assert {r.name for r in containers} == set(NAMES)
    assert {r.status for r in containers} == {ResourceStatus.RELEASED}
    leftovers = podman("ps", "-a", "--format", "{{.Names}}").stdout.split()
    assert not [name for name in leftovers if name.startswith(f"ilmar-{INSTANCE}-")]
    assert not layout.attempt_dir("e1", 1).exists()
    assert store.unfinished_operations() == ()
    store.close()


# -- cancellation on real Podman (#16 part 3, ADR-0025) ---------------------------------


class LongShellAgent:
    """A long agent run in the real sandbox that leaves a background writer behind.

    It asks for its own cancellation first, as ``ilmar cancel`` would from
    another terminal: the watcher must stop it through the runtime.
    """

    script = "(sleep 6; printf late > late.txt) & sleep 300"

    def __init__(self, layout: WorkspaceLayout) -> None:
        self.layout = layout
        self.result: object = None

    @property
    def name(self) -> str:
        return "long-shell"

    def run(
        self,
        runtime: ContainerRuntime,
        container: ContainerHandle,
        request: AgentRequest,
        *,
        stop: StopSignal | None = None,
    ) -> AgentResult:
        fd = cancel_inbox.open_inbox(self.layout.coordinator_root, create=True)
        try:
            cancel_inbox.submit(fd, "e1", "alice", now=datetime.now(UTC))
        finally:
            os.close(fd)
        done = runtime.run(container, ["sh", "-c", self.script], timeout_seconds=600, stop=stop)
        self.result = done
        if done.cancelled:
            return AgentResult(AgentOutcome.CANCELLED, "stopped on request", None)
        return AgentResult(AgentOutcome.FAILED, "was not stopped", done.exit_code)


@dataclasses.dataclass
class CancelWorld:
    layout: WorkspaceLayout
    scm: FakeSourceControl
    tasks: FakeTaskProvider
    store: SQLiteExecutionStore
    components: Components
    clock: Clock

    def coordinator(self, owner: Ownership) -> Coordinator:
        ids = iter(["e1"])
        return Coordinator.create(
            self.components, owner, max_attempts=3, agent_timeout_seconds=600, clock=self.clock,
            new_id=lambda: next(ids), watch_interval_seconds=0.2,
        )  # fmt: skip

    def owner(self) -> Ownership:
        return Ownership.take(layout=self.layout, store=self.store, instance_id=INSTANCE,
                              now=self.clock())  # fmt: skip

    def sandbox_gone(self) -> None:
        sandbox = container_name(INSTANCE, "e1", 1)
        for kind, ref in (("container", sandbox), ("container", f"{sandbox}-egress"),
                          ("network", f"{sandbox}-net")):  # fmt: skip
            assert podman(kind, "exists", ref, check=False).returncode != 0, ref

    def claim_released_and_never_picked_again(self) -> None:
        assert self.tasks.list_eligible() == (task("1"),)
        assert self.store.unfinished_operations() == ()
        box = self.layout.coordinator_root.joinpath(*cancel_inbox.INBOX_PARTS)
        assert not [n for n in os.listdir(box) if n != cancel_inbox.REJECTED]
        with self.owner() as owner:
            again = self.coordinator(owner).run_once()
        assert again.summary == "no eligible task"


def cancel_world(tmp_path: Path, agent: AgentRunner) -> CancelWorld:
    state = tmp_path.resolve() / "state"
    state.mkdir()
    layout = WorkspaceLayout.under(state)
    layout.workspaces_root.mkdir()
    scm = FakeSourceControl()
    scm.seed(REPO, "main", BASE_FILES)
    tasks = FakeTaskProvider((task("1"),))
    store = SQLiteExecutionStore(tmp_path / "executions.sqlite3")
    components = Components(
        instance_id=INSTANCE,
        layout=layout,
        task_provider=tasks,
        source_control=scm,
        container_runtime=PodmanRuntime(
            layout=layout, egress=EgressSettings(proxy_image=PROXY_IMAGE)
        ),
        agent_runner=agent,
        execution_store=store,
        agent_sandbox=AgentSandbox(IMAGE, NetworkPolicy.ALLOWLIST, ("api.anthropic.com",)),
        target=Target(REPO, "main"),
        verification=VerificationPolicy(
            image=IMAGE,
            commands=(VerificationCommand("never", ("true",), timeout_seconds=60, job="tests"),),
        ),
    )
    return CancelWorld(layout, scm, tasks, store, components, Clock())


def test_a_running_agent_is_cancelled_and_its_whole_sandbox_removed(tmp_path: Path) -> None:
    world = cancel_world(tmp_path, ShellAgent())
    world.components.agent_runner = LongShellAgent(world.layout)
    began = time.monotonic()
    with world.owner() as owner:
        report = world.coordinator(owner).run_once()
    assert time.monotonic() - began < 120
    assert (report.code, report.results[0].outcome) == (0, Outcome.CANCELLED), report.summary
    store = world.store
    execution = store.get("e1")
    assert execution is not None
    assert execution.state is ExecutionState.CANCELLED
    decisions = [e.data["decision"] for e in store.events(execution_id="e1")
                 if e.type is EventType.EXECUTION_CANCEL_REQUESTED]  # fmt: skip
    assert decisions == ["accepted"]
    run = store.operation(operation_key(OperationKind.RUN_AGENT, "e1", 1))
    assert run is not None
    assert (run.status, run.result["outcome"]) == (OperationStatus.SUCCEEDED, "cancelled")
    assert store.operation(operation_key(OperationKind.FREEZE_CANDIDATE, "e1", 1)) is None
    assert store.evidence("e1") == ()
    assert world.scm.pushes == []
    world.sandbox_gone()  # container, egress proxy and network, on the host
    containers = [r for r in store.resources(execution_id="e1") if r.kind is ResourceKind.CONTAINER]
    assert {r.status for r in containers} == {ResourceStatus.RELEASED}
    # Nothing it left in the background writes afterwards; the workspace is kept.
    workspace = world.layout.agent_workspace("e1", 1)
    time.sleep(8)
    assert workspace.exists()
    assert not (workspace / "late.txt").exists()
    world.claim_released_and_never_picked_again()
    store.close()


class Crash(BaseException):
    """A process death between the real sandbox start and its record."""


class StartThenDie:
    """The real runtime, except that the process dies right after the agent
    sandbox (container, egress proxy, network) really started."""

    def __init__(self, inner: PodmanRuntime) -> None:
        self.inner = inner
        self.armed = True

    def __getattr__(self, name: str) -> Any:
        return getattr(self.inner, name)

    def start(self, spec: ContainerSpec) -> ContainerHandle:
        handle = self.inner.start(spec)
        if self.armed:
            self.armed = False
            raise Crash
        return handle


def test_a_cancellation_after_a_crash_removes_the_real_sandbox_before_cancelled(
    tmp_path: Path,
) -> None:
    """Review of PR #78: the ending removes a live sandbox the ledger does not show
    as gone through the journaled, ownership-verified stop, settles the start,
    and only then moves to CANCELLED and releases the claim."""
    world = cancel_world(tmp_path, ShellAgent())
    real = PodmanRuntime(layout=world.layout, egress=EgressSettings(proxy_image=PROXY_IMAGE))
    world.components.container_runtime = StartThenDie(real)
    with world.owner() as owner, pytest.raises(Crash):
        world.coordinator(owner).run_once()
    sandbox = container_name(INSTANCE, "e1", 1)
    assert podman("container", "exists", sandbox, check=False).returncode == 0  # really running
    fd = cancel_inbox.open_inbox(world.layout.coordinator_root, create=True)
    try:
        cancel_inbox.submit(fd, "e1", "alice", now=datetime.now(UTC))
    finally:
        os.close(fd)
    with world.owner() as owner:
        report = world.coordinator(owner).run_once()
    assert (report.code, report.results[0].outcome) == (0, Outcome.CANCELLED), report.summary
    store = world.store
    world.sandbox_gone()
    events = store.events(execution_id="e1")
    stop_done = next(i for i, e in enumerate(events)
                     if e.type is EventType.OPERATION_SUCCEEDED
                     and str(e.operation).startswith("stop-container:"))  # fmt: skip
    cancelled = next(i for i, e in enumerate(events)
                     if e.type is EventType.EXECUTION_TRANSITIONED
                     and e.data.get("to") == "cancelled")  # fmt: skip
    assert stop_done < cancelled  # removed and confirmed first, then the transition
    start = [o for o in store.operations("e1") if o.kind is OperationKind.START_CONTAINER]
    assert [o.status for o in start] == [OperationStatus.FAILED]  # settled: removed
    assert store.operation(operation_key(OperationKind.RUN_AGENT, "e1", 1)) is None
    containers = [r for r in store.resources(execution_id="e1") if r.kind is ResourceKind.CONTAINER]
    assert {r.status for r in containers} == {ResourceStatus.RELEASED}
    world.claim_released_and_never_picked_again()
    store.close()
