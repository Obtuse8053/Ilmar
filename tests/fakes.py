# SPDX-License-Identifier: Apache-2.0
"""In-process fakes of the provider ports, for tests.

The fakes are themselves checked against the contract suites, so tests that
use them exercise the same behaviour a real adapter must provide.
"""

from __future__ import annotations

import hashlib
import itertools
import json
import os
import stat
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from ilmar.domain.agent import AgentOutcome, AgentRequest, AgentResult
from ilmar.domain.changes import (
    CandidateRevision,
    ChangeRequest,
    ChangeRequestSpec,
    ChecksSummary,
    CheckState,
    RepositoryRef,
)
from ilmar.domain.errors import (
    CandidateRejectedError,
    LeaseConflictError,
    NonFastForwardError,
    OwnershipConflictError,
    PolicyViolationError,
    ProviderError,
)
from ilmar.domain.tasks import ClaimOutcome, Task, TaskRef
from ilmar.domain.workspace import (
    CommandResult,
    ContainerHandle,
    ContainerInfo,
    ContainerSpec,
    DirectoryIdentity,
    NetworkPolicy,
    ResourceOwner,
)
from ilmar.ports.container_runtime import ContainerRuntime


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


ProgramHandler = Callable[[Sequence[str], float, Mapping[str, str]], CommandResult]


@dataclass
class _FakeContainer:
    spec: ContainerSpec
    handle: ContainerHandle
    owner: ResourceOwner | None
    running: bool = True


class FakeContainerRuntime:
    """Simulates a sandbox by interpreting a tiny command language.

    Supported ``argv`` forms:

    * ``["echo", text]`` prints ``text`` and a newline;
    * ``["exit", code]`` exits with ``int(code)``;
    * ``["sleep", seconds]`` takes ``float(seconds)`` of simulated time;
    * ``["write", relative_path, content]`` writes a file into the workspace;
    * ``[program, ...]`` for any program registered with :meth:`register`.
    """

    def __init__(self, *, name: str = "fake", allow_unrestricted_network: bool = False) -> None:
        self._name = name
        self._allow_unrestricted = allow_unrestricted_network
        self._ids = itertools.count(1)
        self._containers: dict[str, _FakeContainer] = {}  # by immutable id
        self._programs: dict[str, ProgramHandler] = {}
        self.commands: list[tuple[str, tuple[str, ...]]] = []

    @property
    def name(self) -> str:
        return self._name

    # -- test helpers ----------------------------------------------------

    def register(self, program: str, handler: ProgramHandler) -> None:
        self._programs[program] = handler

    def create_foreign(self, spec: ContainerSpec, owner: ResourceOwner | None) -> ContainerHandle:
        """Simulate a container someone else created under ``spec.name``."""
        return self._create(spec, owner)

    # -- ContainerRuntime ------------------------------------------------

    def start(self, spec: ContainerSpec) -> ContainerHandle:
        if spec.network is NetworkPolicy.UNRESTRICTED and not self._allow_unrestricted:
            raise PolicyViolationError("unrestricted network is not permitted here")
        if not spec.host_workspace.is_dir():
            raise ProviderError(f"workspace does not exist: {spec.host_workspace}")
        info = spec.host_workspace.lstat()
        if (
            spec.host_workspace.is_symlink()
            or DirectoryIdentity(info.st_dev, info.st_ino) != spec.workspace_identity
        ):
            raise OwnershipConflictError(f"{spec.host_workspace} is not the recorded workspace")
        existing = self._by_name(spec.name)
        if existing is not None:
            if existing.owner != spec.owner:
                raise OwnershipConflictError(f"container {spec.name} is owned by someone else")
            del self._containers[existing.handle.id]
        return self._create(spec, spec.owner)

    def find(self, name: str) -> ContainerInfo | None:
        found = self._by_name(name)
        if found is None:
            return None
        return ContainerInfo(handle=found.handle, owner=found.owner, running=found.running)

    def run(
        self,
        handle: ContainerHandle,
        argv: Sequence[str],
        *,
        timeout_seconds: float,
        env: Mapping[str, str] | None = None,
    ) -> CommandResult:
        container = self._containers.get(handle.id)
        if container is None or not container.running:
            raise ProviderError(f"container is not running: {handle.id}")
        self.commands.append((handle.id, tuple(argv)))
        result = self._interpret(container.spec, argv, timeout_seconds, env)
        if result.timed_out:  # the whole container goes (port contract)
            del self._containers[handle.id]
        return result

    def _interpret(
        self,
        spec: ContainerSpec,
        argv: Sequence[str],
        timeout_seconds: float,
        env: Mapping[str, str] | None,
    ) -> CommandResult:
        if argv and argv[0] in self._programs:
            return self._programs[argv[0]](argv, timeout_seconds, dict(env or {}))
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
        container = self._containers.get(handle.id)
        return container is not None and container.running

    def stop(self, handle: ContainerHandle, *, owner: ResourceOwner) -> None:
        container = self._containers.get(handle.id)
        if container is None:
            return
        if container.owner != owner:
            raise OwnershipConflictError(f"container {handle.name} is owned by someone else")
        del self._containers[handle.id]

    # -- internals -------------------------------------------------------

    def _by_name(self, name: str) -> _FakeContainer | None:
        return next((c for c in self._containers.values() if c.handle.name == name), None)

    def _create(self, spec: ContainerSpec, owner: ResourceOwner | None) -> ContainerHandle:
        handle = ContainerHandle(runtime=self._name, id=f"fake-{next(self._ids)}", name=spec.name)
        self._containers[handle.id] = _FakeContainer(spec=spec, handle=handle, owner=owner)
        return handle


# ---------------------------------------------------------------------------
# Source control
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _Entry:
    """One tracked path: a regular file (``data``) or a symlink (``link``)."""

    data: bytes | None = None
    link: str | None = None
    executable: bool = False


Tree = dict[str, _Entry]


@dataclass
class _ChangeRequestRecord:
    request: ChangeRequest
    title: str
    description: str
    ilmar_marker: bool
    checks: ChecksSummary = field(default_factory=lambda: ChecksSummary(CheckState.NONE))


class FakeSourceControl:
    """An in-memory source-control platform with content-addressed revisions.

    It never runs git or anything else from a workspace, so it trivially
    satisfies the "nothing in the workspace is executed" rule; real adapters
    prove that rule against real git with the same contract.
    """

    MARKER = "<!-- ilmar -->"

    def __init__(self, *, name: str = "fake") -> None:
        self._name = name
        self._revisions: dict[str, Tree] = {}
        self._parents: dict[str, str | None] = {}
        self._branches: dict[tuple[RepositoryRef, str], str] = {}
        self._change_requests: dict[tuple[RepositoryRef, str], _ChangeRequestRecord] = {}
        self._keys = itertools.count(1)
        self.pushes: list[tuple[RepositoryRef, str, str]] = []

    @property
    def name(self) -> str:
        return self._name

    # -- test helpers ----------------------------------------------------

    def seed(self, repository: RepositoryRef, branch: str, files: Mapping[str, str]) -> str:
        tree = {path: _Entry(data=text.encode()) for path, text in files.items()}
        revision = self._store(None, tree, "seed")
        self._branches[(repository, branch)] = revision
        return revision

    def push_external(
        self, repository: RepositoryRef, branch: str, files: Mapping[str, str]
    ) -> str:
        """Simulate someone else moving ``branch``."""
        base = self._branches[(repository, branch)]
        tree = dict(self._revisions[base])
        tree.update({path: _Entry(data=text.encode()) for path, text in files.items()})
        revision = self._store(base, tree, "external")
        self._branches[(repository, branch)] = revision
        return revision

    def open_foreign_change_request(self, repository: RepositoryRef, source_branch: str) -> None:
        request = ChangeRequest(
            repository=repository,
            key=str(next(self._keys)),
            url=f"https://example.invalid/cr/{source_branch}",
            source_branch=source_branch,
            target_branch="main",
        )
        self._change_requests[(repository, source_branch)] = _ChangeRequestRecord(
            request, "human title", "opened by a person", ilmar_marker=False
        )

    def set_checks(self, change_request: ChangeRequest, summary: ChecksSummary) -> None:
        key = (change_request.repository, change_request.source_branch)
        self._change_requests[key].checks = summary

    def change_request_text(self, repository: RepositoryRef, source_branch: str) -> tuple[str, str]:
        record = self._change_requests[(repository, source_branch)]
        return record.title, record.description

    # -- SourceControlProvider -------------------------------------------

    def branch_head(self, repository: RepositoryRef, branch: str) -> str | None:
        return self._branches.get((repository, branch))

    def checkout(
        self, repository: RepositoryRef, *, base_branch: str, branch: str, destination: Path
    ) -> str:
        if destination.exists():
            raise ProviderError(f"destination exists: {destination}")
        head = self._branches.get((repository, branch)) or self._branches.get(
            (repository, base_branch)
        )
        if head is None:
            raise ProviderError(f"unknown base branch: {base_branch}")
        self._materialise(self._revisions[head], destination)
        return head

    def freeze_candidate(
        self, repository: RepositoryRef, *, base_revision: str, workspace: Path, message: str
    ) -> CandidateRevision:
        base = self._revisions.get(base_revision)
        if base is None:
            raise ProviderError(f"unknown base revision: {base_revision}")
        tree = _read_tree(workspace)
        changed = sorted(path for path in set(base) | set(tree) if base.get(path) != tree.get(path))
        revision = self._store(base_revision, tree, message)
        return CandidateRevision(revision, base_revision, tuple(changed))

    def checkout_revision(
        self, repository: RepositoryRef, *, revision: str, destination: Path
    ) -> None:
        if destination.exists():
            raise ProviderError(f"destination exists: {destination}")
        tree = self._revisions.get(revision)
        if tree is None:
            raise ProviderError(f"unknown revision: {revision}")
        self._materialise(tree, destination)

    def publish_revision(
        self,
        repository: RepositoryRef,
        *,
        revision: str,
        branch: str,
        expected_head: str | None,
    ) -> None:
        if revision not in self._revisions:
            raise ProviderError(f"unknown revision: {revision}")
        head = self._branches.get((repository, branch))
        if head == revision:
            return
        if head != expected_head:
            raise LeaseConflictError(f"{branch} is at {head}, expected {expected_head}")
        if head is not None and not self._descends_from(revision, head):
            raise NonFastForwardError(f"{revision} does not descend from {branch} at {head}")
        self._branches[(repository, branch)] = revision
        self.pushes.append((repository, branch, revision))

    def upsert_change_request(self, spec: ChangeRequestSpec) -> ChangeRequest:
        key = (spec.repository, spec.source_branch)
        record = self._change_requests.get(key)
        description = f"{spec.description}\n\n{self.MARKER} task={spec.task}"
        if record is not None:
            if not record.ilmar_marker:
                raise OwnershipConflictError(
                    f"change request on {spec.source_branch} was not opened by Ilmar"
                )
            record.title, record.description = spec.title, description
            return record.request
        request = ChangeRequest(
            repository=spec.repository,
            key=str(next(self._keys)),
            url=f"https://example.invalid/cr/{spec.source_branch}",
            source_branch=spec.source_branch,
            target_branch=spec.target_branch,
        )
        self._change_requests[key] = _ChangeRequestRecord(
            request, spec.title, description, ilmar_marker=True
        )
        return request

    def checks(self, change_request: ChangeRequest) -> ChecksSummary:
        record = self._change_requests.get(
            (change_request.repository, change_request.source_branch)
        )
        if record is None or record.request.key != change_request.key:
            raise ProviderError(f"unknown change request: {change_request.key}")
        return record.checks

    # -- internals -------------------------------------------------------

    def _store(self, parent: str | None, tree: Tree, message: str) -> str:
        digest = hashlib.sha256()
        digest.update(f"{parent}\0{message}\0".encode())
        for path in sorted(tree):
            entry = tree[path]
            digest.update(path.encode() + b"\0")
            digest.update(b"L" + entry.link.encode() if entry.link is not None else b"F")
            digest.update(hashlib.sha256(entry.data or b"").digest())
            digest.update(b"x" if entry.executable else b"-")
        revision = digest.hexdigest()[:40]
        self._revisions.setdefault(revision, dict(tree))
        self._parents.setdefault(revision, parent)
        return revision

    def _descends_from(self, revision: str, ancestor: str) -> bool:
        current: str | None = revision
        while current is not None:
            if current == ancestor:
                return True
            current = self._parents.get(current)
        return False

    @staticmethod
    def _materialise(tree: Tree, destination: Path) -> None:
        destination.mkdir(parents=True)
        for path, entry in sorted(tree.items()):
            target = destination / path
            target.parent.mkdir(parents=True, exist_ok=True)
            if entry.link is not None:
                target.symlink_to(entry.link)
            else:
                target.write_bytes(entry.data or b"")
                target.chmod(0o755 if entry.executable else 0o644)


def _read_tree(workspace: Path) -> Tree:
    """Read a workspace as plain data: never follow links, never execute anything."""
    tree: Tree = {}
    for directory, subdirs, files in os.walk(workspace, followlinks=False):
        relative_dir = Path(directory).relative_to(workspace)
        if relative_dir == Path("."):
            subdirs[:] = [d for d in subdirs if d != ".git"]
            files = [f for f in files if f != ".git"]
        for name in [*subdirs, *files]:
            if name == ".git":
                raise CandidateRejectedError(f"nested repository at {relative_dir / name}")
        for name in list(subdirs):
            if (Path(directory) / name).is_symlink():
                subdirs.remove(name)
                files.append(name)
        for name in files:
            full = Path(directory) / name
            relative = (relative_dir / name).as_posix()
            info = full.lstat()
            if stat.S_ISLNK(info.st_mode):
                tree[relative] = _Entry(link=os.readlink(full))
            elif stat.S_ISREG(info.st_mode):
                tree[relative] = _Entry(
                    data=full.read_bytes(), executable=bool(info.st_mode & 0o111)
                )
            else:
                raise CandidateRejectedError(f"special file not allowed: {relative}")
    return tree


# ---------------------------------------------------------------------------
# Agent runner
# ---------------------------------------------------------------------------


class FakeAgentRunner:
    """Runs a ``fake-agent`` program in the container and parses its JSON result.

    The fake agent prints one JSON object on its last stdout line:
    ``{"type": "result", "is_error": bool, "summary": str}``. COMPLETED needs
    both exit status 0 and a parsed, non-error result (F-021).
    """

    PROGRAM = "fake-agent"

    @property
    def name(self) -> str:
        return "fake"

    def run(
        self, runtime: ContainerRuntime, container: ContainerHandle, request: AgentRequest
    ) -> AgentResult:
        result = runtime.run(
            container,
            [self.PROGRAM, "--print", request.instructions],
            timeout_seconds=request.timeout_seconds,
        )
        if result.timed_out:
            return AgentResult(AgentOutcome.TIMED_OUT, "agent timed out", result.exit_code)
        parsed = _parse_result(result.stdout)
        if parsed is None:
            return AgentResult(AgentOutcome.FAILED, "no structured result", result.exit_code)
        is_error, summary = parsed
        ok = result.exit_code == 0 and not is_error
        outcome = AgentOutcome.COMPLETED if ok else AgentOutcome.FAILED
        return AgentResult(outcome, summary, result.exit_code)


def _parse_result(stdout: str) -> tuple[bool, str] | None:
    lines = [line for line in stdout.splitlines() if line.strip()]
    if not lines:
        return None
    try:
        data = json.loads(lines[-1])
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict) or data.get("type") != "result":
        return None
    is_error, summary = data.get("is_error"), data.get("summary")
    if not isinstance(is_error, bool) or not isinstance(summary, str):
        return None
    return is_error, summary
