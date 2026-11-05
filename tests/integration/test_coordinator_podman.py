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

from collections.abc import Iterator
from pathlib import Path

import pytest

from coordinator_support import BASE_FILES, REPO, Clock, task
from ilmar.adapters.podman import PodmanRuntime
from ilmar.adapters.podman.runtime import EgressSettings
from ilmar.adapters.sqlite import SQLiteExecutionStore
from ilmar.core.components import AgentSandbox, Components, Target
from ilmar.core.coordinator import Coordinator
from ilmar.core.layout import WorkspaceLayout
from ilmar.core.naming import container_name, verification_container_name
from ilmar.core.ownership import Ownership
from ilmar.core.worker import Outcome
from ilmar.domain.agent import AgentOutcome, AgentRequest, AgentResult
from ilmar.domain.execution import ExecutionState
from ilmar.domain.ledger import ResourceKind, ResourceStatus
from ilmar.domain.verification import Verdict, VerificationCommand, VerificationPolicy
from ilmar.domain.workspace import ContainerHandle, NetworkPolicy
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
        self, runtime: ContainerRuntime, container: ContainerHandle, request: AgentRequest
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
