# SPDX-License-Identifier: Apache-2.0
"""A complete, offline coordinator setup for end-to-end tests (#11b, ADR-0021)."""

from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from ilmar.adapters.memory import InMemoryExecutionStore
from ilmar.core.components import AgentSandbox, Components, Target
from ilmar.core.coordinator import Coordinator
from ilmar.core.layout import WorkspaceLayout
from ilmar.core.ownership import Ownership
from ilmar.core.secret_scan import SecretScanner
from ilmar.core.state_machine import NO_BACKOFF, RetryBackoff
from ilmar.domain.agent import AgentOutcome, AgentRequest, AgentResult
from ilmar.domain.budget import BudgetLimits
from ilmar.domain.changes import RepositoryRef
from ilmar.domain.tasks import Task, TaskRef
from ilmar.domain.verification import VerificationCommand, VerificationPolicy
from ilmar.domain.workspace import (
    CommandResult,
    ContainerHandle,
    NetworkPolicy,
    StopSignal,
)
from ilmar.ports.container_runtime import ContainerRuntime, StreamConsumer
from ilmar.ports.execution_store import ExecutionStore

from fakes import FakeContainerRuntime, FakeSourceControl, FakeTaskProvider

REPO = RepositoryRef("fake", "owner/repo")
BASE_FILES = {"src/app.py": "print('bug')\n", "README.md": "readme\n"}
START = datetime(2026, 11, 5, 12, 0, tzinfo=UTC)


class Clock:
    """Strictly increasing time, one second per reading."""

    def __init__(self) -> None:
        self.now = START

    def __call__(self) -> datetime:
        self.now += timedelta(seconds=1)
        return self.now


class WorkspaceRuntime(FakeContainerRuntime):
    """The fake runtime, plus a ``check`` program that reads the container's own workspace.

    ``["check", path, text]`` exits 0 if ``path`` in the mounted workspace
    contains ``text``. ``stop_hook`` lets a test break removal.
    """

    def __init__(self) -> None:
        super().__init__(allow_unrestricted_network=False)
        self.stop_hook: Callable[[ContainerHandle], None] | None = None
        self.started: list[str] = []

    def start(self, spec: Any) -> ContainerHandle:
        handle = super().start(spec)
        self.started.append(spec.name)
        return handle

    def run(
        self,
        handle: ContainerHandle,
        argv: Sequence[str],
        *,
        timeout_seconds: float,
        env: Mapping[str, str] | None = None,
        stop: StopSignal | None = None,
        idle_timeout_seconds: float | None = None,
        stdout_consumer: StreamConsumer | None = None,
    ) -> CommandResult:
        if argv and argv[0] == "check":
            container = self._containers[handle.id]
            target = container.spec.host_workspace / argv[1]
            ok = target.is_file() and argv[2] in target.read_text()
            self.commands.append((handle.id, tuple(argv)))
            return CommandResult(0 if ok else 1, "", "" if ok else "not fixed\n", 0.1)
        return super().run(
            handle, argv, timeout_seconds=timeout_seconds, env=env, stop=stop,
            idle_timeout_seconds=idle_timeout_seconds, stdout_consumer=stdout_consumer,
        )  # fmt: skip

    def stop(self, handle: ContainerHandle, *, owner: Any) -> None:
        if self.stop_hook is not None:
            self.stop_hook(handle)
        super().stop(handle, owner=owner)

    def remove_owned(self, name: str, *, owner: Any) -> bool:
        found = self.find(name)
        if self.stop_hook is not None and found is not None:
            self.stop_hook(found.handle)
        return super().remove_owned(name, owner=owner)


@dataclass
class ScriptedAgent:
    """An agent that edits the mounted workspace through the runtime, then reports.

    ``plan`` is consumed one entry per run: ``(outcome, {path: content})``.
    ``during`` runs inside the agent's turn (to simulate things happening
    while it works).
    """

    plan: list[tuple[AgentOutcome, dict[str, str]]] = field(
        default_factory=lambda: [(AgentOutcome.COMPLETED, {"src/app.py": "print('fixed')\n"})]
    )
    during: Callable[[], None] | None = None
    runs: int = 0
    summary: str | None = None
    """What the agent reports; by default ``run <n>: <outcome>``."""

    @property
    def name(self) -> str:
        return "scripted"

    def run(
        self,
        runtime: ContainerRuntime,
        container: ContainerHandle,
        request: AgentRequest,
        *,
        stop: StopSignal | None = None,
    ) -> AgentResult:
        outcome, edits = self.plan[min(self.runs, len(self.plan) - 1)]
        self.runs += 1
        for path, content in edits.items():
            runtime.run(container, ["write", path, content], timeout_seconds=10)
        if self.during is not None:
            self.during()
        said = self.summary if self.summary is not None else f"run {self.runs}: {outcome.value}"
        return AgentResult(outcome, said, 0)


def policy(*commands: VerificationCommand) -> VerificationPolicy:
    return VerificationPolicy(
        image="localhost/verify:1",
        commands=commands
        or (VerificationCommand("fixed", ("check", "src/app.py", "fixed"), job="tests"),),
    )


@dataclass
class Setup:
    layout: WorkspaceLayout
    tasks: FakeTaskProvider
    scm: FakeSourceControl
    runtime: WorkspaceRuntime
    agent: ScriptedAgent
    store: ExecutionStore
    components: Components
    ownership: Ownership
    clock: Clock
    ids: Iterator[str]

    def coordinator(
        self,
        *,
        max_attempts: int = 3,
        watch_interval_seconds: float = 2.0,
        retry_backoff: RetryBackoff = NO_BACKOFF,
        execution_wall_seconds: float | None = None,
        budget: BudgetLimits | None = None,
        kill_switch: Callable[[], bool] = lambda: False,
    ) -> Coordinator:
        return Coordinator.create(
            self.components,
            self.ownership,
            max_attempts=max_attempts,
            agent_timeout_seconds=60,
            retry_backoff=retry_backoff,
            execution_wall_seconds=execution_wall_seconds,
            budget=budget,
            kill_switch=kill_switch,
            clock=self.clock,
            new_id=lambda: next(self.ids),
            new_token=lambda: "tok-" + next(self.ids),
            watch_interval_seconds=watch_interval_seconds,
        )


def make_setup(
    tmp_path: Path,
    *,
    tasks: Sequence[Task] = (),
    store: ExecutionStore | None = None,
    verification: VerificationPolicy | None = None,
    secret_scanner: SecretScanner | None = None,
    agent_sandbox: AgentSandbox | None = None,
) -> Setup:
    state = tmp_path.resolve() / "state"
    state.mkdir()
    layout = WorkspaceLayout.under(state)
    layout.workspaces_root.mkdir()
    provider = FakeTaskProvider(tasks or (task("1"),))
    scm = FakeSourceControl()
    scm.seed(REPO, "main", BASE_FILES)
    runtime = WorkspaceRuntime()
    agent = ScriptedAgent()
    store = store or InMemoryExecutionStore()
    components = Components(
        instance_id="inst",
        layout=layout,
        task_provider=provider,
        source_control=scm,
        container_runtime=runtime,
        agent_runner=agent,
        execution_store=store,
        agent_sandbox=agent_sandbox
        or AgentSandbox("localhost/agent:1", NetworkPolicy.ALLOWLIST, ("api.example",)),
        target=Target(REPO, "main"),
        verification=verification or policy(),
        secret_scanner=secret_scanner,
    )
    clock = Clock()
    ownership = Ownership.take(layout=layout, store=store, instance_id="inst", now=clock())
    ids = (f"e{n}" for n in range(1, 10_000))
    return Setup(layout, provider, scm, runtime, agent, store, components, ownership, clock, ids)


def task(key: str, title: str = "Fix the bug") -> Task:
    return Task(TaskRef("fake", key), title, "Make src/app.py print 'fixed'.")
