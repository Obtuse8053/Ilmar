# SPDX-License-Identifier: Apache-2.0
"""Crash recovery on real Podman (#11c, ADR-0022).

* A coordinator dies while the agent runs in a real allowlisted sandbox (its
  container, egress sidecar and network all exist). A new process, with a new
  ``PodmanRuntime`` that has no in-memory state, removes all three from the
  ledger and labels alone, never reuses the sandbox, and publishes from a new
  attempt.
* ``remove_owned`` on real resources: complete, partial leftovers, and a
  foreign owner (refused before anything is touched).

Needs ``ILMAR_TEST_PODMAN_IMAGE`` and ``ILMAR_TEST_PODMAN_PROXY_IMAGE``;
skips without them unless ``ILMAR_TEST_PODMAN_REQUIRED=1``.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from coordinator_support import BASE_FILES, REPO, Clock, task
from ilmar.adapters.podman import EgressSettings, PodmanRuntime
from ilmar.adapters.sqlite import SQLiteExecutionStore
from ilmar.core.components import AgentSandbox, Components, Target
from ilmar.core.coordinator import Coordinator, PassReport
from ilmar.core.layout import WorkspaceLayout
from ilmar.core.naming import container_name, verification_container_name
from ilmar.core.ownership import Ownership
from ilmar.domain.agent import AgentOutcome, AgentRequest, AgentResult
from ilmar.domain.errors import OwnershipConflictError
from ilmar.domain.execution import ExecutionState
from ilmar.domain.ledger import ResourceKind, ResourceStatus
from ilmar.domain.verification import VerificationCommand, VerificationPolicy
from ilmar.domain.workspace import ContainerHandle, ContainerSpec, NetworkPolicy, ResourceOwner
from ilmar.ports.container_runtime import ContainerRuntime
from recovery_support import Crash, Killable, KillableStore, Switch

from fakes import FakeSourceControl, FakeTaskProvider
from integration.podman_support import (
    IMAGE,
    PODMAN,
    PROXY_IMAGE,
    identity_of,
    make_workspace,
    podman,
    remove_everything,
    require,
)

pytestmark = pytest.mark.integration

INSTANCE = "it-rec"
NAMES = (
    *(container_name(INSTANCE, "e1", attempt) for attempt in (1, 2, 3)),
    *(verification_container_name(INSTANCE, "e1", attempt, 1, "tests") for attempt in (1, 2, 3)),
    "ilmar-it-rec-direct",
)
OWNER = ResourceOwner(instance_id="inst-r", execution_id="exec-1", attempt=1, token="tok-r")
FOREIGN = ResourceOwner(instance_id="inst-r", execution_id="exec-1", attempt=1, token="other")


@pytest.fixture(autouse=True)
def _require() -> Iterator[None]:
    require(("podman", PODMAN), ("ILMAR_TEST_PODMAN_IMAGE", IMAGE), ("proxy image", PROXY_IMAGE))
    remove_everything(*NAMES)
    yield
    remove_everything(*NAMES)


def exists(kind: str, name: str) -> bool:
    return podman(kind, "exists", name, check=False).returncode == 0


def sandbox_parts(name: str) -> dict[str, bool]:
    return {
        "container": exists("container", name),
        "sidecar": exists("container", name + "-egress"),
        "network": exists("network", name + "-net"),
    }


# -- a coordinator killed in the middle of the agent run -----------------------------


class ShellAgent:
    """Edits the workspace inside its real sandbox, or dies there on demand."""

    def __init__(self, switch: Switch | None = None) -> None:
        self.switch = switch
        self.runs = 0
        self.containers: list[str] = []

    @property
    def name(self) -> str:
        return "shell"

    def run(
        self, runtime: ContainerRuntime, container: ContainerHandle, request: AgentRequest
    ) -> AgentResult:
        self.containers.append(container.name)
        if self.switch is not None:
            # The agent has started in its sandbox; the coordinator dies now,
            # so nothing (not even a ``finally``) can remove the sandbox.
            runtime.run(container, ["sh", "-c", "echo half > src/half.txt"], timeout_seconds=60)
            self.switch.dead = True
            raise Crash("killed during the agent run")
        self.runs += 1
        script = "printf \"print('fixed')\\n\" > src/app.py"
        done = runtime.run(container, ["sh", "-c", script], timeout_seconds=60)
        assert done.exit_code == 0, done.stderr
        return AgentResult(AgentOutcome.COMPLETED, "fixed it", 0)


def coordinator_process(
    tmp: Path, layout: WorkspaceLayout, world: tuple[FakeTaskProvider, FakeSourceControl],
    agent: ShellAgent, switch: Switch, ids: Iterator[str], clock: Clock,
) -> tuple[SQLiteExecutionStore, Ownership, Coordinator]:  # fmt: skip
    tasks, scm = world
    inner = SQLiteExecutionStore(tmp / "executions.sqlite3")
    runtime = PodmanRuntime(layout=layout, egress=EgressSettings(proxy_image=PROXY_IMAGE))
    policy = VerificationPolicy(
        image=IMAGE,
        commands=(
            VerificationCommand(
                "fixed", ("sh", "-c", "grep -q fixed src/app.py"), timeout_seconds=60, job="tests"
            ),
            VerificationCommand(
                "clean", ("sh", "-c", "! test -e src/half.txt"), timeout_seconds=60, job="tests"
            ),
        ),
    )
    components = Components(
        instance_id=INSTANCE,
        layout=layout,
        task_provider=tasks,
        source_control=scm,
        container_runtime=Killable(runtime, {"stop", "remove_owned", "start"}, switch, "rt"),
        agent_runner=agent,
        execution_store=KillableStore(inner, switch),
        agent_sandbox=AgentSandbox(IMAGE, NetworkPolicy.ALLOWLIST, ("api.anthropic.com",)),
        target=Target(REPO, "main"),
        verification=policy,
    )
    owner = Ownership.take(layout=layout, store=inner, instance_id=INSTANCE, now=clock())
    coordinator = Coordinator.create(
        components,
        owner,
        max_attempts=3,
        agent_timeout_seconds=120,
        clock=clock,
        new_id=lambda: next(ids),
    )
    return inner, owner, coordinator


def test_a_coordinator_killed_mid_agent_run_recovers_on_real_podman(tmp_path: Path) -> None:
    tmp = tmp_path.resolve()
    state = tmp / "state"
    state.mkdir()
    layout = WorkspaceLayout.under(state)
    layout.workspaces_root.mkdir()
    scm = FakeSourceControl()
    scm.seed(REPO, "main", BASE_FILES)
    world = (FakeTaskProvider((task("1"),)), scm)
    ids = iter(["e1"])
    clock = Clock()  # time moves on across the restart, as a wall clock does

    switch = Switch()
    killer = ShellAgent(switch)
    store, owner, coordinator = coordinator_process(tmp, layout, world, killer, switch, ids, clock)
    with pytest.raises(Crash):
        coordinator.run_once()
    owner.release()  # the kernel drops the lock of a dead process
    store.close()
    first = container_name(INSTANCE, "e1", 1)
    assert sandbox_parts(first) == {"container": True, "sidecar": True, "network": True}

    agent = ShellAgent()
    store, owner, coordinator = coordinator_process(tmp, layout, world, agent, Switch(), ids, clock)
    report: PassReport = coordinator.run_once()
    try:
        assert report.code == 0, report.summary
        assert sandbox_parts(first) == {"container": False, "sidecar": False, "network": False}
        execution = store.get("e1")
        assert execution is not None
        assert execution.state is ExecutionState.SUCCEEDED
        assert execution.attempt == 2  # decision 3: a new attempt in a new sandbox
        assert agent.containers == [container_name(INSTANCE, "e1", 2)]
        assert agent.runs == 1
        (pushed,) = scm.pushes
        assert pushed[2] == execution.candidate_revision
        evidence = [e for e in store.evidence("e1") if e.attempt == 2]
        assert evidence[-1].candidate_revision == execution.candidate_revision
        assert evidence[-1].run == 1
        rows = [r for r in store.resources(execution_id="e1") if r.kind is ResourceKind.CONTAINER]
        assert {r.status for r in rows} == {ResourceStatus.RELEASED}
        assert store.unfinished_operations() == ()
        leftovers = podman("ps", "-a", "--format", "{{.Names}}").stdout.split()
        assert not [n for n in leftovers if n.startswith(f"ilmar-{INSTANCE}-")]
        networks = podman("network", "ls", "--format", "{{.Name}}").stdout.split()
        assert not [n for n in networks if n.startswith(f"ilmar-{INSTANCE}-")]
        assert layout.attempt_dir("e1", 1).exists()  # the failed attempt's workspace is kept
    finally:
        owner.release()
        store.close()


# -- remove_owned on real resources ---------------------------------------------------------


@pytest.fixture
def layout(tmp_path: Path) -> WorkspaceLayout:
    layout = WorkspaceLayout.under(tmp_path.resolve() / "state")
    layout.workspaces_root.mkdir(parents=True)
    return layout


def allowlisted(layout: WorkspaceLayout, name: str) -> None:
    """Start an allowlisted sandbox, then forget it: only Podman knows it exists."""
    workspace = make_workspace(layout, "rec")
    PodmanRuntime(layout=layout, egress=EgressSettings(proxy_image=PROXY_IMAGE)).start(
        ContainerSpec(
            image=IMAGE,
            host_workspace=workspace,
            name=name,
            owner=OWNER,
            workspace_identity=identity_of(workspace),
            network=NetworkPolicy.ALLOWLIST,
            allowed_hosts=("api.anthropic.com",),
        )
    )


def fresh(layout: WorkspaceLayout) -> PodmanRuntime:
    return PodmanRuntime(layout=layout, egress=EgressSettings(proxy_image=PROXY_IMAGE))


def test_remove_owned_removes_container_sidecar_and_network_without_memory(
    layout: WorkspaceLayout,
) -> None:
    name = "ilmar-it-rec-direct"
    allowlisted(layout, name)
    assert sandbox_parts(name) == {"container": True, "sidecar": True, "network": True}
    runtime = fresh(layout)
    assert runtime.remove_owned(name, owner=OWNER) is True
    assert sandbox_parts(name) == {"container": False, "sidecar": False, "network": False}
    assert runtime.remove_owned(name, owner=OWNER) is False  # idempotent


def test_remove_owned_finishes_an_interrupted_removal(layout: WorkspaceLayout) -> None:
    name = "ilmar-it-rec-direct"
    allowlisted(layout, name)
    podman("rm", "--force", "--time", "0", name, name + "-egress")  # only the network is left
    assert sandbox_parts(name) == {"container": False, "sidecar": False, "network": True}
    assert fresh(layout).remove_owned(name, owner=OWNER) is True
    assert sandbox_parts(name)["network"] is False


def test_remove_owned_refuses_a_foreign_owner_before_touching_anything(
    layout: WorkspaceLayout,
) -> None:
    name = "ilmar-it-rec-direct"
    allowlisted(layout, name)
    with pytest.raises(OwnershipConflictError):
        fresh(layout).remove_owned(name, owner=FOREIGN)
    assert sandbox_parts(name) == {"container": True, "sidecar": True, "network": True}
