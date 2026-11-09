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

import io
import json
import os
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field, replace
from datetime import timedelta
from functools import partial
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
from ilmar import cli_events
from ilmar.adapters.sqlite import SQLiteExecutionStore
from ilmar.cli import _build_parser
from ilmar.core.components import AgentSandbox, Components, Target
from ilmar.core.coordinator import Coordinator, PassReport
from ilmar.core.layout import WorkspaceLayout
from ilmar.core.ownership import Ownership
from ilmar.domain.agent import AgentOutcome
from ilmar.domain.changes import CandidateRevision
from ilmar.domain.errors import (
    CandidateRejectedError,
    ContainmentError,
    FencingError,
    PolicyViolationError,
)
from ilmar.domain.execution import Execution, ExecutionState
from ilmar.domain.ledger import OperationKind, ResourceKind, ResourceStatus, operation_key
from ilmar.domain.verification import VerificationCommand, VerificationPolicy
from ilmar.domain.workspace import NetworkPolicy, ResourceOwner
from recovery_support import (
    Crash,
    Killable,
    KillableStore,
    Switch,
    event_problems,
    to_previous_schema,
)

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
    verification: VerificationPolicy = field(default_factory=policy)
    scm: FakeSourceControl = field(default_factory=FakeSourceControl)
    runtime: WorkspaceRuntime = field(default_factory=WorkspaceRuntime)
    agent: ScriptedAgent = field(default_factory=ScriptedAgent)
    clock: Clock = field(default_factory=Clock)
    ids: Iterator[str] = field(default_factory=lambda: (f"e{n}" for n in range(1, 1000)))
    # A new runtime for every process (a real restart: no in-memory state
    # survives). ``None`` shares ``runtime`` across processes.
    make_runtime: Callable[[WorkspaceLayout], Any] | None = None

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
            container_runtime=Killable(
                self.make_runtime(self.layout) if self.make_runtime else self.runtime,
                PORT_EFFECTS["runtime"],
                switch,
                "runtime",
            ),
            agent_runner=Killable(self.agent, PORT_EFFECTS["agent"], switch, "agent"),
            execution_store=store,
            agent_sandbox=AgentSandbox("localhost/agent:1", NetworkPolicy.ALLOWLIST, ("api.x",)),
            target=Target(REPO, "main"),
            verification=self.verification,
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
        return Process(inner, ownership, coordinator, switch, self.db)


@dataclass
class Process:
    store: SQLiteExecutionStore
    ownership: Ownership
    coordinator: Coordinator
    switch: Switch
    db: Path

    def run(self) -> PassReport | None:
        try:
            return self.coordinator.run_once()
        except Crash:
            return None

    def exit(self) -> None:
        """The kernel releases the lock; the connection goes away.

        Whatever the process did, crash or not, the event log must rebuild what
        it left (I11, ADR-0024): every crash point and every recovery pass of
        the suite is checked here.
        """
        self.ownership.release()
        self.store.close()
        problems = event_problems(self.db)
        assert problems == [], problems


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


def baselined_unfinished(db: Path) -> dict[str, str]:
    """Operation key -> status, for every operation the baseline found unfinished."""
    with SQLiteExecutionStore.open_read_only(db) as store:
        return {
            str(e.operation): str(e.data["status"])
            for e in store.events()
            if e.type.value == "operation.baseline"
            and e.data["status"] in ("intended", "ambiguous")
        }


def recovered_from_previous_schema(root: Path, switch: Switch) -> list[str]:
    """Crash, leave the store as the previous schema would have, then recover with the
    events migration: the store must be recovered exactly as before, I11 must hold
    (checked at every process exit) and every unfinished operation the baseline
    found must be settled from its baseline status."""
    world = World(root.resolve())
    crashed = world.process(switch)
    assert crashed.run() is None, "no crash"
    crashed.exit()
    to_previous_schema(world.db)
    recover(world)
    problems = check_invariants(world)
    unfinished = baselined_unfinished(world.db)
    with SQLiteExecutionStore.open_read_only(world.db) as store:
        events = store.events()
    for key, status in unfinished.items():
        settled = [e for e in events if e.operation == key and e.data.get("from") == status]
        if not settled:
            problems.append(f"{key} was not settled from its baseline status {status}")
    return problems


def test_a_store_left_by_the_previous_schema_is_recovered_at_every_crash_point(
    tmp_path: Path,
) -> None:
    writes, effects = totals(tmp_path)
    failures = {}
    unfinished_seen = 0
    points = [(f"w{n}", Switch(write_at=n)) for n in range(1, writes + 1)]
    points += [(f"k{k}", Switch(effect_at=k)) for k in range(1, effects + 1)]
    for label, switch in points:
        root = tmp_path / label
        root.mkdir()
        problems = recovered_from_previous_schema(root, switch)
        if problems:
            failures[label] = problems
        unfinished_seen += len(baselined_unfinished(root / "executions.sqlite3"))
    assert failures == {}
    assert unfinished_seen > 0  # some crash points left intended or ambiguous operations


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


# -- review of 28a4a89 ------------------------------------------------------------------


@pytest.mark.parametrize(
    "error",
    [PolicyViolationError("forbidden"), ContainmentError("escaped", container_id="c", name="c")],
)
def test_a_recorded_security_failure_is_never_retried_after_a_crash(
    world: World, error: Exception
) -> None:
    """The agent run fails with a never-transient error, recorded on its operation;
    the process dies before the failure moves the execution. Recovery must fail
    it, not abandon the run and retry (ADR-0021)."""
    switch = Switch()

    def violate() -> None:
        switch.write_at = switch.writes + 2  # die right after the outcome is recorded
        raise error

    world.agent.during = violate
    crashed = world.process(switch)
    assert crashed.run() is None
    crashed.exit()
    with SQLiteExecutionStore(world.db) as store:
        (run,) = [o for o in store.operations("e1") if o.kind is K.RUN_AGENT]
        assert run.note == type(error).__name__  # the class is on record
    world.agent.during = None
    recover(world)
    execution = one_execution(world)
    assert execution.state is S.FAILED, execution
    assert execution.attempt == 1
    assert world.agent.runs == 1
    assert world.runtime._containers == {}
    assert world.scm.pushes == []
    with SQLiteExecutionStore(world.db) as store:
        assert store.unfinished_operations() == ()


def test_a_security_failure_leaves_nothing_for_later_passes(world: World) -> None:
    """Without a crash too: the failure is final, so the next pass has nothing to redo."""

    def violate() -> None:
        raise PolicyViolationError("forbidden")

    world.agent.during = violate
    process = world.process()
    first = process.run()
    second = process.run()
    process.exit()
    assert first is not None
    assert first.code == 1, first.summary
    assert second is not None
    assert (second.code, second.summary) == (0, "no eligible task")
    with SQLiteExecutionStore(world.db) as store:
        (run,) = [o for o in store.operations("e1") if o.kind is K.RUN_AGENT]
        assert (run.status.value, run.note) == ("failed", "PolicyViolationError")
        assert store.unfinished_operations() == ()


@pytest.mark.parametrize("boundary", ["agent.run", "scm.publish_revision", "scm.freeze_candidate"])
def test_a_wall_clock_stepped_back_across_a_restart_does_not_stop_recovery(
    world: World, boundary: str
) -> None:
    """An NTP step back of a day between crash and restart (ADR-0022, clock rules)."""
    crash_before(world, boundary)
    world.clock.now -= timedelta(days=1)
    reports = recover(world)
    assert [r.code for r in reports] == [0], [r.summary for r in reports]
    assert check_invariants(world) == []
    with SQLiteExecutionStore(world.db) as store:
        history = store.history("e1")
    stamps = [t.at for t in history]
    assert stamps == sorted(stamps)  # persisted time never runs backwards


def test_a_wall_clock_stepped_back_while_running_does_not_stop_the_pass(world: World) -> None:
    def step_back() -> None:
        world.clock.now -= timedelta(days=1)

    world.agent.during = step_back
    reports = recover(world)
    assert [r.code for r in reports] == [0], [r.summary for r in reports]
    assert check_invariants(world) == []


def test_an_unexpected_error_in_one_execution_does_not_stop_unrelated_work(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Decision 4 holds for bugs too: one execution's unexpected error blocks only it."""
    world = World(tmp_path.resolve(), tasks_override=("1", "2"))
    crash_before(world, "scm.publish_revision")
    real_get = world.tasks.get

    def buggy(ref: Any) -> Any:
        if ref.key == "1":
            raise RuntimeError("tracker adapter bug")
        return real_get(ref)

    monkeypatch.setattr(world.tasks, "get", buggy)
    results = []
    for _ in range(2):
        process = world.process()
        results.append(process.run())
        process.exit()
    first, second = results
    assert first is not None
    assert first.code == 2, first.summary
    assert "RuntimeError" in first.summary
    assert second is not None
    assert second.code == 2  # still blocked, still reported, never silently skipped
    with SQLiteExecutionStore(world.db) as store:
        states = {e.task.key: e.state for e in store.list_in_states(set(ExecutionState))}
    assert states == {"1": S.PUBLISHING, "2": S.SUCCEEDED}


def test_evidence_from_another_verification_policy_never_publishes(world: World) -> None:
    """The policy changed while Ilmar was down: the old run's evidence is not enough."""
    crash_before(world, "scm.publish_revision")
    world.verification = policy(
        VerificationCommand("fixed", ("check", "src/app.py", "fixed"), job="tests"),
        VerificationCommand("stricter", ("check", "src/app.py", "never"), job="tests"),
    )
    process = world.process()
    report = process.run()
    process.exit()
    assert report is not None
    assert report.code == 2, report.summary
    assert "policy" in report.summary
    assert world.scm.pushes == []
    assert world.agent.runs == 1


# -- crash injection beyond the happy path (review of 28a4a89) ---------------------------
#
# The same exhaustive crash points for the other paths an execution can take:
# a transient agent failure and a new attempt; a verification failure (the
# claim is released, the workspace kept); and the follow-up after a merge.

FIXED = {"src/app.py": "print('fixed')\n"}


def retry_world(root: Path) -> World:
    world = World(root)
    world.agent.plan = [(AgentOutcome.TIMED_OUT, {}), (AgentOutcome.COMPLETED, FIXED)]
    return world


def failing_world(root: Path) -> World:
    world = World(root)
    world.agent.plan = [(AgentOutcome.COMPLETED, {"src/app.py": "print('still broken')\n"})]
    return world


def notes_posted_once(world: World) -> list[str]:
    updates = world.tasks.updates(task("1").ref)
    return [f"note posted {n} times: {u!r}" for u in set(updates) if (n := updates.count(u)) > 1]


def check_retry(world: World) -> list[str]:
    execution = one_execution(world)
    if execution.state is S.FAILED:
        # Bounded by design: each interrupted attempt costs one of the three.
        problems = [p for p in check_failed(world, verified=False) if "verified" not in p]
        if execution.attempt != 3 or "attempts exhausted" not in (execution.last_error or ""):
            problems.append(f"failed early: attempt {execution.attempt}, {execution.last_error}")
        return problems
    problems = check_invariants(world)
    execution = one_execution(world)
    if execution.attempt < 2:
        problems.append("the timed-out attempt was not followed by a new one")
    return problems + notes_posted_once(world)


def exhausted_world(root: Path) -> World:
    world = World(root)
    world.agent.plan = [(AgentOutcome.TIMED_OUT, {})]
    return world


def security_world(root: Path) -> World:
    world = World(root)

    def violate() -> None:
        raise PolicyViolationError("forbidden")

    world.agent.during = violate
    return world


def check_failed(world: World, *, verified: bool = True) -> list[str]:
    problems = notes_posted_once(world)
    with SQLiteExecutionStore(world.db) as store:
        executions = store.list_in_states(set(ExecutionState))
        if [e.state for e in executions] != [S.FAILED]:
            return [f"expected one failed execution: {[e.state.value for e in executions]}"]
        (execution,) = executions
        if store.unfinished_operations():
            problems.append(f"unfinished: {[o.key for o in store.unfinished_operations()]}")
        rows = store.resources(execution_id=execution.id)
        if any(r.status is R.QUARANTINED for r in rows):
            problems.append("a resource was quarantined")
        if any(r.kind is ResourceKind.CONTAINER and r.status is not R.RELEASED for r in rows):
            problems.append("a sandbox row is not released")
        kept = [r for r in rows if r.kind is ResourceKind.WORKSPACE and r.status is R.ACTIVE]
        if not kept:
            problems.append("the failed workspace was not kept")
        if store.operation(f"release-task:{execution.id}:{execution.attempt}:failed") is None:
            problems.append("the claim was not released")
    if world.scm.pushes or world.scm._change_requests:
        problems.append("a failed execution published something")
        if world.agent.runs > execution.attempt:
            problems.append(f"{world.agent.runs} agent runs in {execution.attempt} attempts")
        evidence = store.evidence(execution.id)
        if not verified and evidence:
            problems.append("an unverifiable execution was verified")
        if verified and (len(evidence) != 1 or evidence[0].attempt != execution.attempt):
            problems.append(f"a deterministic failure was retried: {len(evidence)} verifications")
    if world.runtime._containers:
        problems.append("live sandboxes remain")
    if world.tasks.list_eligible() != (task("1"),):
        problems.append("the failed task's claim is still held")
    return problems


def merge(world: World) -> None:
    ((_, request),) = world.scm._change_requests.items()
    world.scm.finish_change_request(request.request, merged=True)


def close(world: World) -> None:
    ((_, request),) = world.scm._change_requests.items()
    world.scm.finish_change_request(request.request, merged=False)


def check_merged(world: World, *, merged: bool = True) -> list[str]:
    problems = check_invariants(world)
    problems = [p for p in problems if "not claimed" not in p]  # released after a merge
    if world.tasks.list_eligible() != (task("1"),):
        problems.append("the claim was not released after the merge")
    with SQLiteExecutionStore(world.db) as store:
        rows = store.resources(execution_id="e1")
        events = store.events(execution_id="e1")
    requests = [r for r in rows if r.kind is ResourceKind.CHANGE_REQUEST]
    note = "merged: task complete" if merged else "closed without merging"
    if [(r.status, r.note) for r in requests] != [(R.RELEASED, note)]:
        problems.append(f"change request row: {[(r.status.value, r.note) for r in requests]}")
    # The observation that decided the release is on its events (ADR-0024 §1).
    step, observed = ("merged", "change-request-merged") if merged else (
        "closed", "change-request-closed")  # fmt: skip
    release = [e for e in events if e.operation == f"release-task:e1:1:{step}"]
    if not release or {e.data.get("observed") for e in release} != {observed}:
        problems.append(f"release events without {observed}: {[e.type.value for e in release]}")
    if release and release[-1].type.value != "operation.succeeded":
        problems.append("the release did not finish")
    return problems + notes_posted_once(world)


def submit_cancel(world: World, once: list[bool]) -> None:
    """A person runs ``ilmar cancel`` once, for the execution that is running: a
    file in the private inbox, which survives every crash (ADR-0025 §1a)."""
    from ilmar.core import cancel_inbox

    if once:
        return
    with SQLiteExecutionStore.open_read_only(world.db) as store:
        active = store.list_in_states({s for s in ExecutionState if not s.is_terminal})
    fd = cancel_inbox.open_inbox(world.layout.coordinator_root, create=True)
    try:
        for execution in active:
            cancel_inbox.submit(fd, execution.id, "alice", now=world.clock())
            once.append(True)
    finally:
        os.close(fd)


def cancelled_during_run_world(root: Path) -> World:
    world = World(root)
    once: list[bool] = []
    world.agent.during = lambda: submit_cancel(world, once)
    return world


def cancelled_after_claim_world(root: Path) -> World:
    world = World(root)
    original = world.tasks.claim
    once: list[bool] = []

    def claim(ref: Any) -> Any:
        outcome = original(ref)
        submit_cancel(world, once)
        return outcome

    world.tasks.claim = claim  # type: ignore[method-assign]
    return world


def check_cancelled(world: World) -> list[str]:
    """ADR-0025 after recovery: I12, the claim released once, the request recorded
    once, nothing left in the inbox, the task never picked again (and I11, checked
    by every ``Process.exit``)."""
    from ilmar.core import cancel_inbox
    from ilmar.domain.events import EventType

    problems = notes_posted_once(world)
    with SQLiteExecutionStore(world.db) as store:
        executions = store.list_in_states(set(ExecutionState))
        if [e.state for e in executions] != [S.CANCELLED]:
            return [f"expected one cancelled execution: {[e.state.value for e in executions]}"]
        (execution,) = executions
        events = store.events(execution_id=execution.id)
        recorded = [e for e in events if e.type is EventType.EXECUTION_CANCEL_REQUESTED]
        if [e.data.get("decision") for e in recorded] != ["accepted"]:
            problems.append(f"recorded: {[e.data for e in recorded]}")
        accepted = min((e.seq or 0) for e in recorded) if recorded else 0
        forbidden = ("run-agent:", "freeze-candidate:", "verify:", "publish-revision:")
        late = [e.operation for e in events if e.type is EventType.OPERATION_INTENDED
                and (e.seq or 0) > accepted and str(e.operation).startswith(forbidden)]  # fmt: skip
        if late:
            problems.append(f"I12: started after the cancellation was accepted: {late}")
        into = [e.reason for e in events if e.type is EventType.EXECUTION_TRANSITIONED
                and e.data.get("to") == "cancelled"]  # fmt: skip
        if into != ["cancel-requested"]:
            problems.append(f"transition reasons: {into}")
        if store.unfinished_operations():
            problems.append(f"unfinished: {[o.key for o in store.unfinished_operations()]}")
        rows = store.resources(execution_id=execution.id)
        if any(r.kind is ResourceKind.CONTAINER and r.status is not R.RELEASED for r in rows):
            problems.append("a sandbox row is not released")
        if any(r.status is R.QUARANTINED for r in rows):
            problems.append("a resource was quarantined")
        release = store.operation(f"release-task:{execution.id}:{execution.attempt}:cancelled")
        if release is None or release.status.value != "succeeded":
            problems.append("the claim was not released")
        if any(o.kind is K.FREEZE_CANDIDATE for o in store.operations(execution.id)):
            problems.append("a cancelled run was frozen")
        if world.agent.runs > execution.attempt:
            problems.append(f"{world.agent.runs} agent runs in {execution.attempt} attempts")
    if world.scm.pushes or world.scm._change_requests:
        problems.append("a cancelled execution published something")
    if world.runtime._containers:
        problems.append("live sandboxes remain")
    if world.tasks.list_eligible() != (task("1"),):
        problems.append("the cancelled task's claim is still held")
    box = world.layout.coordinator_root.joinpath(*cancel_inbox.INBOX_PARTS)
    left = [n for n in os.listdir(box) if n != cancel_inbox.REJECTED] if box.exists() else []
    if left:
        problems.append(f"inbox leftovers: {left}")
    if box.joinpath(cancel_inbox.REJECTED).exists():
        problems.append("a valid request was set aside")
    again = world.process()
    report = again.run()
    again.exit()
    if report is None or report.summary != "no eligible task":
        problems.append(f"picked again: {report.summary if report else 'crash'}")
    return problems


def merge_with_a_stranded_request(world: World) -> None:
    """The change request is merged, and a cancellation that raced with the finish
    sits in the inbox for the terminal execution (ADR-0025 §3)."""
    from ilmar.core import cancel_inbox

    merge(world)
    fd = cancel_inbox.open_inbox(world.layout.coordinator_root, create=True)
    try:
        cancel_inbox.submit(fd, "e1", "alice", now=world.clock())
    finally:
        os.close(fd)


def check_stranded(world: World) -> list[str]:
    """The merge is followed up as usual, and the stranded request is recorded
    ``already-terminal`` exactly once and cleaned up; nothing else changes."""
    from ilmar.core import cancel_inbox
    from ilmar.domain.events import EventType

    problems = check_merged(world)
    with SQLiteExecutionStore(world.db) as store:
        execution = store.get("e1")
        events = store.events(execution_id="e1")
    if execution is None or execution.state is not S.SUCCEEDED:
        problems.append("the published execution changed state")
    recorded = [e.data.get("decision") for e in events
                if e.type is EventType.EXECUTION_CANCEL_REQUESTED]  # fmt: skip
    if recorded != ["already-terminal"]:
        problems.append(f"recorded: {recorded}")
    if len(world.scm.pushes) != 1:
        problems.append(f"{len(world.scm.pushes)} pushes")
    box = world.layout.coordinator_root.joinpath(*cancel_inbox.INBOX_PARTS)
    if os.listdir(box):
        problems.append(f"inbox leftovers: {os.listdir(box)}")
    return problems


Scenario = tuple[
    Callable[[Path], World], Callable[[World], None] | None, Callable[[World], list[str]]
]
SCENARIOS: dict[str, Scenario] = {
    "retry": (retry_world, None, check_retry),
    "failing": (failing_world, None, check_failed),
    "exhausted": (exhausted_world, None, partial(check_failed, verified=False)),
    "security": (security_world, None, partial(check_failed, verified=False)),
    "merged": (World, merge, check_merged),
    "closed": (World, close, partial(check_merged, merged=False)),
    "cancelled-during-run": (cancelled_during_run_world, None, check_cancelled),
    "cancelled-after-claim": (cancelled_after_claim_world, None, check_cancelled),
    "stranded-after-merge": (World, merge_with_a_stranded_request, check_stranded),
}


def scenario_points(tmp_path: Path, name: str) -> tuple[int, int]:
    make, between, _ = SCENARIOS[name]
    root = tmp_path / f"{name}-reference"
    root.mkdir()
    world = make(root.resolve())
    process = world.process()
    report = process.run()
    process.exit()
    assert report is not None
    if between is None:
        return process.switch.writes, process.switch.effects
    between(world)
    second = world.process()
    assert second.run() is not None
    second.exit()
    return second.switch.writes, second.switch.effects


def crash_scenario(tmp_path: Path, name: str, switch: Switch, label: str) -> list[str]:
    make, between, check = SCENARIOS[name]
    root = tmp_path / label
    root.mkdir()
    world = make(root.resolve())
    if between is not None:  # the crash happens in the pass after the merge
        first = world.process()
        assert first.run() is not None
        first.exit()
        between(world)
    crashed = world.process(switch)
    if crashed.run() is not None:
        return [f"{label}: no crash"]
    crashed.exit()
    recover(world)
    return check(world)


@pytest.mark.parametrize("name", sorted(SCENARIOS))
def test_every_crash_point_is_recovered_on_every_path(tmp_path: Path, name: str) -> None:
    writes, effects = scenario_points(tmp_path, name)
    assert writes > 3
    failures: dict[str, list[str]] = {}
    for n in range(1, writes + 1):
        if problems := crash_scenario(tmp_path, name, Switch(write_at=n), f"w{n}"):
            failures[f"write {n}"] = problems
    for k in range(1, effects + 1):
        if problems := crash_scenario(tmp_path, name, Switch(effect_at=k), f"e{k}"):
            failures[f"effect {k}"] = problems
    assert failures == {}


@pytest.mark.parametrize("name", sorted(SCENARIOS))
def test_recovery_interrupted_again_is_recovered_on_every_path(tmp_path: Path, name: str) -> None:
    """Every first crash point, then a second crash early, midway and late in recovery."""
    writes, _ = scenario_points(tmp_path, name)
    make, between, check = SCENARIOS[name]
    failures: dict[str, list[str]] = {}
    for first in range(1, writes + 1):
        for second in (1, 4, 9):
            root = tmp_path / f"d{first}-{second}"
            root.mkdir()
            world = make(root.resolve())
            if between is not None:
                setup = world.process()
                assert setup.run() is not None
                setup.exit()
                between(world)
            crashed = world.process(Switch(write_at=first))
            assert crashed.run() is None
            crashed.exit()
            again = world.process(Switch(write_at=second))
            again.run()  # may finish before reaching the second crash point
            again.exit()
            recover(world)
            if problems := check(world):
                failures[f"{first}/{second}"] = problems
    assert failures == {}


def test_interrupted_provisioning_stays_transient_through_a_second_crash(tmp_path: Path) -> None:
    """Review of 28a4a89: the abandon was recorded, then a crash before the retry; the
    next recovery read the abandoned step as a deterministic failure."""
    world = retry_world(tmp_path.resolve())
    crashed = world.process(Switch(write_at=6))  # in attempt 1's provisioning
    assert crashed.run() is None
    crashed.exit()
    again = world.process(Switch(write_at=4))  # after the abandon, before the retry
    assert again.run() is None
    again.exit()
    recover(world)
    execution = one_execution(world)
    assert execution.state is S.SUCCEEDED, execution.last_error
    assert check_retry(world) == []


# -- containment across restarts (#15, ADR-0014, ADR-0015, ADR-0020) ----------------------


def podman_world(root: Path, podman: Any) -> World:
    """A world whose every process gets a *new* PodmanRuntime over one fake podman,
    so the adapter's in-memory quarantine is gone after each restart: only the
    ledger can keep the workspace blocked."""
    from ilmar.adapters.podman import EgressSettings, PodmanRuntime

    def make(layout: WorkspaceLayout) -> PodmanRuntime:
        return PodmanRuntime(
            layout=layout, environ={"PATH": "/usr/bin"}, executor=podman,
            mount_probe=podman.mount_probe,
            egress=EgressSettings(proxy_image="localhost/ilmar-proxy:1"),
        )  # fmt: skip

    world = World(root, make_runtime=make)
    world.agent.plan = [(AgentOutcome.COMPLETED, {})]  # no edits: the fake podman runs nothing
    return world


@pytest.mark.parametrize("part", ["proxy", "network"])
def test_a_failed_sidecar_removal_keeps_the_execution_blocked_across_restarts(
    tmp_path: Path, part: str
) -> None:
    """A failed removal of the egress proxy or of the sandbox network quarantines the
    sandbox in the ledger. Across restarts (each with a fresh runtime), the
    workspace is never frozen, mounted or reused, no agent runs again, and the
    claim is kept. Per ADR-0020 the quarantine is final for automation: even once
    removal would now succeed, nothing retries or releases it (a human does, #59)."""
    from fake_podman import FakePodman

    podman = FakePodman()
    sandbox = "ilmar-inst-e1-1"
    if part == "proxy":
        podman.fail_rm_names.add(sandbox + "-egress")
    else:
        podman.network_rm_fails = True
    world = podman_world(tmp_path.resolve(), podman)

    first = world.process()
    report = first.run()
    first.exit()
    assert report is not None
    assert report.code == 2, report.summary
    assert report.results[0].detail == "a sandbox could not be removed"  # the containment itself
    assert world.agent.runs == 1

    for repaired in (False, True):  # a restart; then a restart after the cause is gone
        if repaired:
            podman.fail_rm_names.clear()
            podman.network_rm_fails = False
        calls_before = len(podman.calls)
        process = world.process()
        report = process.run()
        execution = process.store.get("e1")
        rows = process.store.resources(execution_id="e1")
        frozen = process.store.operation(operation_key(K.FREEZE_CANDIDATE, "e1", 1))
        process.exit()
        assert report is not None
        assert report.code == 2, report.summary
        assert report.results[0].detail == "quarantined resources remain"
        assert execution is not None
        assert (execution.state, execution.attempt) == (S.FAILED, 1)
        containers = [r for r in rows if r.kind is ResourceKind.CONTAINER]
        assert [r.status for r in containers] == [R.QUARANTINED]
        assert frozen is None  # never frozen
        assert world.agent.runs == 1  # never run again
        assert world.tasks.list_eligible() == ()  # the claim is kept for a human
        assert world.layout.attempt_dir("e1", 1).exists()  # the workspace is kept
        new_commands = [c.argv[1:3] for c in podman.calls[calls_before:]]
        assert ["run"] not in [c[:1] for c in new_commands]  # no sandbox on that workspace
        assert not [c for c in new_commands if c[0] == "rm" or c == ["network", "rm"]], (
            "a quarantined sandbox is final for automation (ADR-0020)"
        )


# -- every non-terminal state is crashed in (#15) ------------------------------------------


NON_TERMINAL = {state for state in ExecutionState if not state.is_terminal}


def states_at_crash_points(root: Path, make: Callable[[Path], World]) -> set[ExecutionState]:
    reference = root / "reference"
    reference.mkdir()
    world = make(reference)
    process = world.process()
    assert process.run() is not None
    process.exit()
    switches = [Switch(write_at=n) for n in range(1, process.switch.writes + 1)]
    switches += [Switch(effect_at=k) for k in range(1, process.switch.effects + 1)]
    states: set[ExecutionState] = set()
    for number, switch in enumerate(switches):
        point = root / f"p{number}"
        point.mkdir()
        world = make(point)
        crashed = world.process(switch)
        assert crashed.run() is None, "every point crashes"
        crashed.exit()
        with SQLiteExecutionStore(world.db) as store:
            states.update(e.state for e in store.list_in_states(set(ExecutionState)))
    return states


def test_every_non_terminal_state_has_a_crash_point(tmp_path: Path) -> None:
    """#15: the crash suite crashes in every non-terminal state, not just some.
    The happy path and the retry path between them reach all seven."""
    expected = {S.QUEUED, S.CLAIMED, S.PROVISIONING, S.RUNNING_AGENT, S.VERIFYING,
                S.PUBLISHING, S.RETRY_WAIT}  # fmt: skip
    assert expected == NON_TERMINAL  # a new state must be added here and crashed in
    root = tmp_path.resolve()
    (root / "happy").mkdir()
    (root / "retry").mkdir()
    seen = states_at_crash_points(root / "happy", World)
    seen |= states_at_crash_points(root / "retry", retry_world)
    assert NON_TERMINAL - seen == set()


def test_ilmar_events_reads_and_verifies_a_recovered_crash(tmp_path: Path) -> None:
    """``ilmar events`` over normal recovery history (#17 part 4)."""
    world = World(tmp_path.resolve())
    crashed = world.process(Switch(effect_at=4))
    assert crashed.run() is None, "no crash"
    crashed.exit()
    recover(world)

    def ilmar(*argv: str) -> tuple[int, str]:
        out = io.StringIO()
        code = cli_events.run(_build_parser().parse_args(list(argv)), out=out, err=io.StringIO())
        return code, out.getvalue()

    db = str(world.db)
    code, out = ilmar("events", "verify", "--db", db)
    assert code == 0, out
    assert "historical findings (information): 0" in out  # covered from the start
    assert "integrity failures: 0" in out
    code, out = ilmar("events", "--db", db, "--type", "operation.reconciled")
    assert code == 0
    settled = [json.loads(line) for line in out.splitlines()]
    assert settled
    assert all("observed" in e["data"] for e in settled)
    every = [json.loads(line) for line in ilmar("events", "--db", db)[1].splitlines()]
    passes = {e.get("pass") for e in every if e["actor"]["kind"] == "coordinator"}
    assert len(passes - {None}) >= 2  # the crashed pass and the recovering one


def test_a_failure_code_survives_a_crash_into_the_next_pass_output(tmp_path: Path) -> None:
    """#17: the code is read back from the durable events, so a pass that finishes a
    failure recorded before a crash still reports its code in `ilmar run --once`."""
    (tmp_path / "reference").mkdir()
    reference = failing_world((tmp_path / "reference").resolve())
    process = reference.process()
    assert process.run() is not None
    process.exit()
    crashed_after_failing = 0
    for effect in range(1, process.switch.effects + 1):
        root = tmp_path / f"k{effect}"
        root.mkdir()
        world = failing_world(root.resolve())
        crashed = world.process(Switch(effect_at=effect))
        if crashed.run() is not None:
            continue
        crashed.exit()
        if one_execution(world).state is not S.FAILED:
            continue  # crashed before the failure was recorded
        crashed_after_failing += 1
        (report,) = recover(world, passes=1)
        assert "e1: failed [verification-failed] verification checks failed" in report.summary
    assert crashed_after_failing > 0  # some crash points lie between the failure and the end
