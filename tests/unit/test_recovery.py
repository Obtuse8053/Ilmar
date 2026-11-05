# SPDX-License-Identifier: Apache-2.0
"""Crash recovery at every journaled boundary (#11c, ADR-0022).

The world outside Ilmar (tracker, source control, runtime, agent) is a set of
fakes that survive "process death". A coordinator process is a store
connection, an ``Ownership`` and a ``Coordinator`` on one SQLite file.

* **Crash at write N:** the N-th write to the store raises ``Crash`` (a
  ``BaseException``, like a killed process) and every later write does too,
  so nothing after that point is persisted.
* **Crash before effect K:** the K-th call that changes the outside world
  raises ``Crash`` *before* acting; the store dies with it.
* **Repeated crashes:** a second crash during recovery.

After a crash, a new process recovers until the execution is finished, and
every invariant of ADR-0022 is checked.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

import pytest

from coordinator_support import (
    BASE_FILES,
    REPO,
    Clock,
    ScriptedAgent,
    WorkspaceRuntime,
    policy,
    task,
)
from ilmar.adapters.sqlite import SQLiteExecutionStore
from ilmar.core.components import AgentSandbox, Components, Target
from ilmar.core.coordinator import Coordinator, PassReport
from ilmar.core.layout import WorkspaceLayout
from ilmar.core.ownership import Ownership
from ilmar.domain.changes import CandidateRevision
from ilmar.domain.errors import CandidateRejectedError, FencingError
from ilmar.domain.execution import Execution, ExecutionState
from ilmar.domain.ledger import OperationKind, ResourceKind, ResourceStatus
from ilmar.domain.workspace import NetworkPolicy, ResourceOwner
from recovery_support import Crash, Killable, KillableStore, Switch

from fakes import FakeSourceControl, FakeTaskProvider

S = ExecutionState
K = OperationKind
R = ResourceStatus

PORT_EFFECTS = {
    "tasks": {"claim", "release", "post_update"},
    "scm": {"checkout", "freeze_candidate", "checkout_revision", "publish_revision",
            "upsert_change_request"},
    "runtime": {"start", "run", "stop", "remove_owned"},
    "agent": {"run"},
}  # fmt: skip


@dataclass
class World:
    """Everything that outlives a coordinator process."""

    root: Path
    tasks: FakeTaskProvider = field(default_factory=lambda: FakeTaskProvider((task("1"),)))
    tasks_override: tuple[str, ...] = ()
    scm: FakeSourceControl = field(default_factory=FakeSourceControl)
    runtime: WorkspaceRuntime = field(default_factory=WorkspaceRuntime)
    agent: ScriptedAgent = field(default_factory=ScriptedAgent)
    clock: Clock = field(default_factory=Clock)
    ids: Iterator[str] = field(default_factory=lambda: (f"e{n}" for n in range(1, 1000)))

    def __post_init__(self) -> None:
        if self.tasks_override:
            self.tasks = FakeTaskProvider(tuple(task(k) for k in self.tasks_override))
        self.scm.seed(REPO, "main", BASE_FILES)
        self.state = self.root / "state"
        self.state.mkdir()
        self.layout = WorkspaceLayout.under(self.state)
        self.layout.workspaces_root.mkdir()
        self.db = self.root / "executions.sqlite3"

    def process(self, switch: Switch | None = None) -> Process:
        switch = switch or Switch()
        inner = SQLiteExecutionStore(self.db)
        store = KillableStore(inner, switch)
        components = Components(
            instance_id="inst",
            layout=self.layout,
            task_provider=Killable(self.tasks, PORT_EFFECTS["tasks"], switch, "tasks"),
            source_control=Killable(self.scm, PORT_EFFECTS["scm"], switch, "scm"),
            container_runtime=Killable(self.runtime, PORT_EFFECTS["runtime"], switch, "runtime"),
            agent_runner=Killable(self.agent, PORT_EFFECTS["agent"], switch, "agent"),
            execution_store=store,
            agent_sandbox=AgentSandbox("localhost/agent:1", NetworkPolicy.ALLOWLIST, ("api.x",)),
            target=Target(REPO, "main"),
            verification=policy(),
        )
        ownership = Ownership.take(
            layout=self.layout, store=inner, instance_id="inst", now=self.clock()
        )
        coordinator = Coordinator.create(
            components,
            ownership,
            max_attempts=3,
            agent_timeout_seconds=60,
            clock=self.clock,
            new_id=lambda: next(self.ids),
            new_token=lambda: "tok-" + next(self.ids),
        )
        return Process(inner, ownership, coordinator, switch)


@dataclass
class Process:
    store: SQLiteExecutionStore
    ownership: Ownership
    coordinator: Coordinator
    switch: Switch

    def run(self) -> PassReport | None:
        try:
            return self.coordinator.run_once()
        except Crash:
            return None

    def exit(self) -> None:
        """The kernel releases the lock; the connection goes away."""
        self.ownership.release()
        self.store.close()


def recover(world: World, *, passes: int = 6) -> list[PassReport]:
    reports = []
    for _ in range(passes):
        process = world.process()
        report = process.run()
        assert report is not None, "no crash is injected during recovery here"
        reports.append(report)
        done = _settled(process.store)
        process.exit()
        if done:
            break
    return reports


def _settled(store: SQLiteExecutionStore) -> bool:
    from ilmar.core.worker import needs_attention

    executions = store.list_in_states(set(ExecutionState))
    return bool(executions) and all(
        e.state.is_terminal and not needs_attention(store, e) for e in executions
    )


def published(store: SQLiteExecutionStore) -> Execution:
    (execution,) = store.list_in_states({S.SUCCEEDED})
    return execution


def check_invariants(world: World) -> list[str]:
    """ADR-0022's invariants after recovery; returns what is violated."""
    problems = []
    with SQLiteExecutionStore(world.db) as store:
        executions = store.list_in_states(set(ExecutionState))
        succeeded = [e for e in executions if e.state is S.SUCCEEDED]
        if len(succeeded) != 1 or len(executions) != 1:
            return [
                "expected one published execution, got "
                + str([(e.id, e.state.value) for e in executions])
            ]
        execution = succeeded[0]
        if len(world.scm.pushes) != 1:
            problems.append(f"{len(world.scm.pushes)} pushes")
        elif world.scm.pushes[0][2] != execution.candidate_revision:
            problems.append("the pushed revision is not the recorded candidate")
        if len(world.scm._change_requests) != 1:
            problems.append(f"{len(world.scm._change_requests)} change requests")
        evidence = [e for e in store.evidence(execution.id) if e.attempt == execution.attempt]
        if not evidence or evidence[-1].candidate_revision != execution.candidate_revision:
            problems.append("the evidence is not for the published candidate")
        if world.scm.branch_head(REPO, execution.branch or "") != execution.candidate_revision:
            problems.append("the branch is not at the verified candidate")
        if execution.attempt > 3:
            problems.append(f"{execution.attempt} attempts")
        runs = [o for o in store.operations(execution.id) if o.kind is K.RUN_AGENT]
        if len({o.attempt for o in runs}) != len(runs):
            problems.append("an attempt ran the agent twice")
        freezes = [o for o in store.operations(execution.id) if o.kind is K.FREEZE_CANDIDATE]
        if any(r.attempt > f.attempt for r in runs for f in freezes):
            problems.append("the agent ran after a candidate was frozen")
        if world.agent.runs > execution.attempt:
            problems.append(
                f"the agent ran {world.agent.runs} times in {execution.attempt} attempts"
            )
        if world.runtime._containers:
            problems.append(
                f"live sandboxes: {[c.spec.name for c in world.runtime._containers.values()]}"
            )
        if store.unfinished_operations():
            problems.append(f"unfinished: {[o.key for o in store.unfinished_operations()]}")
        rows = store.resources(execution_id=execution.id)
        if any(r.status is R.QUARANTINED for r in rows):
            problems.append("a resource was quarantined")
        if any(r.kind is ResourceKind.CONTAINER and r.status is not R.RELEASED for r in rows):
            problems.append("a sandbox row is not released")
        final = [
            r for r in rows if r.attempt == execution.attempt and r.kind is ResourceKind.WORKSPACE
        ]
        if any(r.status is not R.RELEASED for r in final):
            problems.append("the published attempt's workspace is not deleted")
        if world.tasks.list_eligible() != ():
            problems.append("the task is not claimed while its change request is open")
    return problems


@pytest.fixture
def world(tmp_path: Path) -> World:
    return World(tmp_path.resolve())


def clean_run(world: World) -> Switch:
    process = world.process()
    report = process.run()
    assert report is not None
    assert report.code == 0, report.summary
    process.exit()
    return process.switch


def test_a_clean_run_satisfies_every_invariant(world: World) -> None:
    switch = clean_run(world)
    assert check_invariants(world) == []
    assert switch.writes > 20
    assert switch.effects > 10


def totals(tmp_path: Path) -> tuple[int, int]:
    (tmp_path / "reference").mkdir()
    reference = World((tmp_path / "reference").resolve())
    switch = clean_run(reference)
    return switch.writes, switch.effects


def test_a_crash_at_every_store_write_is_recovered(tmp_path: Path) -> None:
    writes, _ = totals(tmp_path)
    failures = {}
    for n in range(1, writes + 1):
        root = tmp_path / f"w{n}"
        root.mkdir()
        world = World(root.resolve())
        crashed = world.process(Switch(write_at=n))
        assert crashed.run() is None, f"no crash at write {n}"
        crashed.exit()
        recover(world)
        problems = check_invariants(world)
        if problems:
            failures[n] = problems
    assert failures == {}


def test_a_crash_before_every_external_effect_is_recovered(tmp_path: Path) -> None:
    _, effects = totals(tmp_path)
    failures = {}
    for k in range(1, effects + 1):
        root = tmp_path / f"k{k}"
        root.mkdir()
        world = World(root.resolve())
        crashed = world.process(Switch(effect_at=k))
        assert crashed.run() is None, f"no crash before effect {k}"
        crashed.exit()
        recover(world)
        problems = check_invariants(world)
        if problems:
            failures[k] = problems
    assert failures == {}


@pytest.mark.parametrize(("first", "second"), [(5, 1), (9, 3), (14, 2), (18, 4), (22, 6), (26, 1)])
def test_recovery_survives_crashing_again(tmp_path: Path, first: int, second: int) -> None:
    """Recovery is idempotent: a crash in the middle of recovering is recovered too."""
    world = World(tmp_path.resolve())
    crashed = world.process(Switch(write_at=first))
    assert crashed.run() is None
    crashed.exit()
    again = world.process(Switch(write_at=second))
    again.run()  # may or may not reach the crash point
    again.exit()
    recover(world)
    assert check_invariants(world) == []


# -- one test per reconciliation rule that must stop for a human -------------


def crash_before(world: World, effect: str) -> None:
    crashed = world.process(Switch(before=effect))
    assert crashed.run() is None, f"never reached {effect}"
    crashed.exit()


def one_execution(world: World) -> Execution:
    with SQLiteExecutionStore(world.db) as store:
        (execution,) = store.list_in_states(set(ExecutionState))
        return execution


def test_a_branch_moved_during_an_interrupted_push_blocks_and_is_never_overwritten(
    world: World,
) -> None:
    crash_before(world, "scm.publish_revision")
    branch = one_execution(world).branch
    assert branch
    foreign = world.scm.seed(REPO, branch, {"src/app.py": "someone else"})

    first, second = (world.process() for _ in range(1)), None
    process = next(first)
    report = process.run()
    process.exit()
    assert report is not None
    assert report.code == 2, report
    assert "unverified head" in report.summary
    again = world.process()
    second = again.run()
    again.exit()
    assert second is not None
    assert second.code == 2  # stays blocked; idempotent
    assert world.scm.pushes == []
    assert world.scm.branch_head(REPO, branch) == foreign
    assert world.agent.runs == 1


def test_a_blocked_execution_does_not_stop_unrelated_work(tmp_path: Path) -> None:
    world = World(tmp_path.resolve(), tasks_override=("1", "2"))
    crash_before(world, "scm.publish_revision")
    blocked = one_execution(world)
    world.scm.seed(REPO, blocked.branch or "", {"src/app.py": "someone else"})

    reports = [world.process() for _ in range(1)]
    process = reports[0]
    report = process.run()
    process.exit()
    assert report is not None
    assert report.code == 2  # worst result of the pass
    with SQLiteExecutionStore(world.db) as store:
        states = {e.task.key: e.state for e in store.list_in_states(set(ExecutionState))}
    assert states == {"1": S.PUBLISHING, "2": S.SUCCEEDED}
    assert len(world.scm.pushes) == 1


def test_a_sandbox_with_a_foreign_owner_is_quarantined_not_removed(world: World) -> None:
    crash_before(world, "agent.run")
    (container,) = world.runtime._containers.values()
    container.owner = ResourceOwner("someone-else", "x", 1, "t")
    process = world.process()
    report = process.run()
    with process.store as store:
        rows = store.resources(execution_id=one_execution(world).id)
    process.exit()
    assert report is not None
    assert report.code == 2, report
    assert world.runtime._containers  # untouched
    assert [r.status for r in rows if r.kind is ResourceKind.CONTAINER] == [R.QUARANTINED]
    assert world.agent.runs == 0


def test_an_interrupted_agent_run_gets_a_new_sandbox_and_a_new_attempt(world: World) -> None:
    """Decision 3: a possibly contaminated sandbox is removed, never reused."""
    crash_before(world, "agent.run")
    first = list(world.runtime._containers.values())
    assert len(first) == 1
    recover(world)
    assert check_invariants(world) == []
    execution = one_execution(world)
    assert execution.attempt == 2
    assert world.agent.runs == 1
    agent_sandboxes = [n for n in world.runtime.started if "verify" not in n]
    assert len(set(agent_sandboxes)) == len(agent_sandboxes) == 2


def test_a_candidate_that_no_longer_matches_its_freeze_blocks_publishing(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    crash_before(world, "scm.publish_revision")
    real = world.scm.load_candidate

    def tampered(*args: Any, **kwargs: Any) -> CandidateRevision:
        found = real(*args, **kwargs)
        return replace(found, changed_paths=tuple(sorted({*found.changed_paths, "extra.txt"})))

    monkeypatch.setattr(world.scm, "load_candidate", tampered)
    process = world.process()
    report = process.run()
    process.exit()
    assert report is not None
    assert report.code == 2, report
    assert world.scm.pushes == []


def test_a_rejected_candidate_blocks_publishing(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    crash_before(world, "scm.publish_revision")

    def rejected(*args: Any, **kwargs: Any) -> CandidateRevision:
        raise CandidateRejectedError("not a frozen candidate on that base")

    monkeypatch.setattr(world.scm, "load_candidate", rejected)
    process = world.process()
    report = process.run()
    process.exit()
    assert report is not None
    assert report.code == 2, report
    assert world.scm.pushes == []
    assert world.agent.runs == 1  # never a new agent run after a freeze


def agent_workspace(world: World) -> Path:
    execution = one_execution(world)
    return world.layout.agent_workspace(execution.id, execution.attempt)


def test_a_replaced_agent_workspace_is_never_frozen(world: World) -> None:
    crash_before(world, "scm.freeze_candidate")
    path = agent_workspace(world)
    path.rename(path.with_name("moved-aside"))  # keeps the old inode in use
    path.mkdir()
    (path / "planted.txt").write_text("planted\n")
    process = world.process()
    report = process.run()
    process.exit()
    assert report is not None
    assert report.code == 2, report
    assert "not the recorded one" in report.summary
    assert world.scm.pushes == []
    assert world.agent.runs == 1


def test_a_symlinked_agent_workspace_is_never_frozen(world: World, tmp_path: Path) -> None:
    crash_before(world, "scm.freeze_candidate")
    path = agent_workspace(world)
    elsewhere = tmp_path / "elsewhere"
    path.rename(elsewhere)
    path.symlink_to(elsewhere, target_is_directory=True)
    process = world.process()
    report = process.run()
    process.exit()
    assert report is not None
    assert report.code == 2, report
    assert world.scm.pushes == []


def test_a_stale_process_is_fenced_out_after_a_takeover(world: World) -> None:
    """A process that lost ownership can never write, even mid-execution."""
    stale = world.process(Switch(before="scm.publish_revision"))
    assert stale.run() is None
    stale.ownership.release()  # its lock is gone, its connection is not
    successor = world.process(Switch(write_at=1))  # takes ownership, dies at once
    assert successor.run() is None
    successor.exit()
    stale.switch.dead, stale.switch.before = False, None
    writes = stale.switch.writes
    with pytest.raises(FencingError):
        stale.coordinator.run_once()
    stale.store.close()
    assert world.scm.pushes == []
    assert stale.switch.writes - writes <= 1  # the refused write itself
    recover(world)
    assert check_invariants(world) == []


def test_several_interrupted_executions_are_recovered_together(tmp_path: Path) -> None:
    """One blocked, one interrupted; once a human clears the block both finish."""
    world = World(tmp_path.resolve(), tasks_override=("1", "2"))
    crash_before(world, "scm.publish_revision")
    first = one_execution(world)
    world.scm.seed(REPO, first.branch or "", {"src/app.py": "someone else"})
    crash_before(world, "agent.run")  # recovering 1 blocks; 2 starts and dies mid-run

    with SQLiteExecutionStore(world.db) as store:
        states = {e.task.key: e.state for e in store.list_in_states(set(ExecutionState))}
    assert states == {"1": S.PUBLISHING, "2": S.RUNNING_AGENT}

    del world.scm._branches[(REPO, first.branch or "")]  # a human deleted it
    process = world.process()
    report = process.run()
    process.exit()
    assert report is not None
    assert report.code == 0, report.summary
    with SQLiteExecutionStore(world.db) as store:
        executions = store.list_in_states(set(ExecutionState))
        assert {e.task.key: e.state for e in executions} == {"1": S.SUCCEEDED, "2": S.SUCCEEDED}
        assert store.unfinished_operations() == ()
        for execution in executions:
            assert world.scm.branch_head(REPO, execution.branch or "") == (
                execution.candidate_revision
            )
    assert len(world.scm.pushes) == 2
    assert world.agent.runs == 2  # task 2's interrupted run never happened; its retry did
    assert world.runtime._containers == {}
