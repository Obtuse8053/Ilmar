# SPDX-License-Identifier: Apache-2.0
"""The coordinator process that ``test_sigkill_podman.py`` kills (#90, ADR-0029 §1).

A real ``Coordinator`` with a real ``PodmanRuntime``, a SQLite store and
in-memory task and source-control fakes. Its agent starts a command that would
run for ten minutes in the real sandbox, saves the fakes' state (which a real
tracker and forge would hold) and prints ``AGENT-RUNNING``. The test then
SIGKILLs this process: nothing in it gets to clean up or stop anything.

Usage: ``python sigkill_child.py <state dir> <execution_wall_seconds>``.
"""

from __future__ import annotations

import pickle
import sys
from datetime import UTC, datetime
from pathlib import Path

from coordinator_support import BASE_FILES, REPO, task
from ilmar.adapters.podman import EgressSettings, PodmanRuntime
from ilmar.adapters.sqlite import SQLiteExecutionStore
from ilmar.core.components import AgentSandbox, Components, Target
from ilmar.core.coordinator import Coordinator
from ilmar.core.layout import WorkspaceLayout
from ilmar.core.ownership import Ownership
from ilmar.core.state_machine import RetryBackoff
from ilmar.domain.agent import AgentRequest, AgentResult
from ilmar.domain.verification import VerificationCommand, VerificationPolicy
from ilmar.domain.workspace import ContainerHandle, NetworkPolicy, StopSignal
from ilmar.ports.container_runtime import ContainerRuntime

from fakes import FakeSourceControl, FakeTaskProvider
from integration.podman_support import IMAGE, PROXY_IMAGE

INSTANCE = "it-kill"
BACKOFF = RetryBackoff(5.0, 5.0)
MARKER = "AGENT-RUNNING"


def utc_now() -> datetime:
    return datetime.now(UTC)


def world_path(tmp: Path) -> Path:
    return tmp / "world.pickle"


def components(
    tmp: Path, world: tuple[FakeTaskProvider, FakeSourceControl], agent: object
) -> tuple[SQLiteExecutionStore, Components]:
    layout = WorkspaceLayout.under(tmp / "state")
    store = SQLiteExecutionStore(tmp / "executions.sqlite3")
    tasks, scm = world
    policy = VerificationPolicy(
        image=IMAGE,
        commands=(
            VerificationCommand(
                "fixed", ("sh", "-c", "grep -q fixed src/app.py"), timeout_seconds=60, job="tests"
            ),
        ),
    )
    return store, Components(
        instance_id=INSTANCE,
        layout=layout,
        task_provider=tasks,
        source_control=scm,
        container_runtime=PodmanRuntime(
            layout=layout, egress=EgressSettings(proxy_image=PROXY_IMAGE)
        ),
        agent_runner=agent,  # type: ignore[arg-type]
        execution_store=store,
        agent_sandbox=AgentSandbox(IMAGE, NetworkPolicy.ALLOWLIST, ("api.anthropic.com",)),
        target=Target(REPO, "main"),
        verification=policy,
    )


class HangingAgent:
    """Starts a long command in the sandbox and never returns on its own."""

    def __init__(self, tmp: Path, world: tuple[FakeTaskProvider, FakeSourceControl]) -> None:
        self.tmp = tmp
        self.world = world

    @property
    def name(self) -> str:
        return "hanging"

    def run(
        self,
        runtime: ContainerRuntime,
        container: ContainerHandle,
        request: AgentRequest,
        *,
        stop: StopSignal | None = None,
    ) -> AgentResult:
        # The claim and checkout are done: keep what GitHub would remember.
        world_path(self.tmp).write_bytes(pickle.dumps(self.world))
        print(f"{MARKER} {container.name}", flush=True)
        runtime.run(container, ["sh", "-c", "sleep 600"], timeout_seconds=600, stop=stop)
        raise AssertionError("the test kills this process before the command ends")


def main(tmp: Path, wall: float) -> None:
    scm = FakeSourceControl()
    scm.seed(REPO, "main", BASE_FILES)
    world = (FakeTaskProvider((task("1"),)), scm)
    store, parts = components(tmp, world, HangingAgent(tmp, world))
    owner = Ownership.take(layout=parts.layout, store=store, instance_id=INSTANCE, now=utc_now())
    coordinator = Coordinator.create(
        parts,
        owner,
        max_attempts=3,
        agent_timeout_seconds=wall,
        execution_wall_seconds=wall,
        retry_backoff=BACKOFF,
        clock=utc_now,
        new_id=lambda: "e1",
    )
    coordinator.run_once()


if __name__ == "__main__":
    main(Path(sys.argv[1]), float(sys.argv[2]))
