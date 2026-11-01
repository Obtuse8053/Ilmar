# SPDX-License-Identifier: Apache-2.0
"""In-process fakes of the provider ports, for tests.

The fakes are themselves checked against the contract suites, so tests that
use them exercise the same behaviour a real adapter must provide.
"""

from __future__ import annotations

import itertools
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field

from ilmar.domain.errors import ProviderError
from ilmar.domain.tasks import ClaimOutcome, Task, TaskRef
from ilmar.domain.workspace import CommandResult, ContainerHandle, ContainerSpec


@dataclass
class _TrackedTask:
    task: Task
    holder: str | None = None
    open: bool = True
    updates: list[str] = field(default_factory=list)


class FakeTaskProvider:
    """A task tracker held in memory. ``worker`` identifies this factory."""

    def __init__(self, tasks: Iterable[Task] = (), *, name: str = "fake", worker: str = "ilmar"):
        self._name = name
        self._worker = worker
        self._tasks: dict[TaskRef, _TrackedTask] = {}
        for task in tasks:
            self.add(task)

    @property
    def name(self) -> str:
        return self._name

    # -- test helpers ----------------------------------------------------

    def add(self, task: Task) -> None:
        if task.ref.provider != self._name:
            raise ValueError("task belongs to a different provider")
        self._tasks[task.ref] = _TrackedTask(task)

    def claim_as(self, ref: TaskRef, worker: str) -> None:
        self._tasks[ref].holder = worker

    def close(self, ref: TaskRef) -> None:
        self._tasks[ref].open = False

    def updates(self, ref: TaskRef) -> Sequence[str]:
        return tuple(self._tasks[ref].updates)

    # -- TaskProvider ----------------------------------------------------

    def list_eligible(self) -> Sequence[Task]:
        return tuple(t.task for t in self._tasks.values() if t.open and t.holder is None)

    def get(self, ref: TaskRef) -> Task | None:
        tracked = self._tasks.get(ref)
        return None if tracked is None else tracked.task

    def claim(self, ref: TaskRef) -> ClaimOutcome:
        tracked = self._tasks.get(ref)
        if tracked is None or not tracked.open:
            return ClaimOutcome.NOT_ELIGIBLE
        if tracked.holder not in (None, self._worker):
            return ClaimOutcome.CLAIMED_BY_OTHER
        tracked.holder = self._worker
        return ClaimOutcome.CLAIMED

    def release(self, ref: TaskRef) -> None:
        tracked = self._tasks.get(ref)
        if tracked is not None and tracked.holder == self._worker:
            tracked.holder = None

    def post_update(self, ref: TaskRef, message: str) -> None:
        tracked = self._tasks.get(ref)
        if tracked is None:
            raise ProviderError(f"unknown task {ref}")
        tracked.updates.append(message)


class FakeContainerRuntime:
    """Simulates a sandbox by interpreting a tiny command language.

    Supported ``argv`` forms:

    * ``["echo", text]`` prints ``text`` and a newline;
    * ``["exit", code]`` exits with ``int(code)``;
    * ``["sleep", seconds]`` takes ``float(seconds)`` of simulated time;
    * ``["write", relative_path, content]`` writes a file into the workspace.
    """

    def __init__(self, *, name: str = "fake") -> None:
        self._name = name
        self._ids = itertools.count(1)
        self._running: dict[str, ContainerSpec] = {}
        self.commands: list[tuple[str, tuple[str, ...]]] = []

    @property
    def name(self) -> str:
        return self._name

    def start(self, spec: ContainerSpec) -> ContainerHandle:
        if not spec.host_workspace.is_dir():
            raise ProviderError(f"workspace does not exist: {spec.host_workspace}")
        handle = ContainerHandle(runtime=self._name, id=f"fake-{next(self._ids)}")
        self._running[handle.id] = spec
        return handle

    def run(
        self,
        handle: ContainerHandle,
        argv: Sequence[str],
        *,
        timeout_seconds: float,
        env: Mapping[str, str] | None = None,
    ) -> CommandResult:
        spec = self._running.get(handle.id)
        if spec is None:
            raise ProviderError(f"container is not running: {handle.id}")
        self.commands.append((handle.id, tuple(argv)))
        match list(argv):
            case ["echo", text]:
                return CommandResult(0, f"{text}\n", "", 0.0)
            case ["exit", code]:
                return CommandResult(int(code), "", "", 0.0)
            case ["sleep", seconds]:
                duration = float(seconds)
                if duration > timeout_seconds:
                    return CommandResult(-9, "", "", timeout_seconds, timed_out=True)
                return CommandResult(0, "", "", duration)
            case ["write", relative, content]:
                target = (spec.host_workspace / relative).resolve()
                if not target.is_relative_to(spec.host_workspace.resolve()):
                    return CommandResult(1, "", "path escapes workspace\n", 0.0)
                target.write_text(content)
                return CommandResult(0, "", "", 0.0)
            case _:
                return CommandResult(127, "", f"unknown command: {argv!r}\n", 0.0)

    def is_running(self, handle: ContainerHandle) -> bool:
        return handle.id in self._running

    def stop(self, handle: ContainerHandle) -> None:
        self._running.pop(handle.id, None)
