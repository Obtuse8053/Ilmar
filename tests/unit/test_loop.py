# SPDX-License-Identifier: Apache-2.0
"""``ilmar run --loop`` (#91 part 3; ADR-0030 §1, §3, ADR-0033, ADR-0040).

* Each missing condition refuses with its exit code and reason: 3 when loop
  mode is not enabled (mode, gate record, its repository or agent version,
  the live repository's immutable ID), 78 when suspended (kill switch,
  suspension, clock).
* A revoked record, a suspension or a kill switch stops the loop at the next
  check; a signal stops it cleanly (0); it never restarts itself.
* The start is recorded before anything else; a restart loop suspends (78).
* Two loops on one store: the second is refused (3).
"""

from __future__ import annotations

import io
import threading
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from coordinator_support import BASE_FILES, REPO, ScriptedAgent, WorkspaceRuntime, policy, task
from ilmar import cli_loop
from ilmar.adapters.memory import InMemoryExecutionStore
from ilmar.core.components import AgentSandbox, Components, Target
from ilmar.core.layout import WorkspaceLayout
from ilmar.core.loop import LoopFacts, loop_blocker, run_loop
from ilmar.core.ownership import Ownership
from ilmar.core.starts import StartLimits, start_log_path
from ilmar.domain.budget import Suspension, SuspensionReason
from ilmar.domain.events import Actor, ActorKind, EventContext
from ilmar.domain.gates import GateEvidence, GateRecord, GateRecorded, GateRevoked
from ilmar.domain.ledger import Commit
from ilmar.domain.workspace import NetworkPolicy

from fakes import FakeSourceControl, FakeTaskProvider

NOW = datetime(2026, 3, 1, 12, tzinfo=UTC)
REPO_ID = 987654321
EVIDENCE = GateEvidence(
    mode="supervised", repository_id=REPO_ID, repository="someone/ilmar-disposable",
    agent_version="2.1.318", evidence="https://github.com/Obtuse8053/Ilmar/pull/130",
    evidence_commit="0123456789abcdef0123456789abcdef01234567",
    live_test="https://github.com/Obtuse8053/Ilmar/actions/runs/43567969214",
    fixture="tests/fixtures/claude_code_streams/2.1.318-recorded.json",
    fixture_sha256="a" * 64, gate1="https://github.com/Obtuse8053/Ilmar/issues/93",
)  # fmt: skip
GATE = GateRecord(1, EVIDENCE, NOW, "alice")


def facts(**changes: Any) -> LoopFacts:
    base = LoopFacts(
        configuration="sha256:aa", started_with="sha256:aa", mode="supervised",
        expected_version="2.1.318", repository_id=REPO_ID,
        live_repository_id=REPO_ID, gates=(GATE,), kill_switch_engaged=False,
        suspension=None, latest_update=NOW, now=NOW,
    )  # fmt: skip
    return replace(base, **changes)


# -- the checks -------------------------------------------------------------------------


def test_every_condition_holding_allows_a_pass() -> None:
    assert loop_blocker(facts()) is None


@pytest.mark.parametrize(
    ("changes", "code", "reason"),
    [
        ({"configuration": "sha256:bb"}, 3, "configuration changed"),
        ({"configuration": "unreadable: No such file or directory"}, 3, "configuration changed"),
        ({"mode": "manual"}, 3, "needs \"supervised\""),
        ({"gates": ()}, 3, "no active supervised gate record"),
        ({"gates": (replace(GATE, revoked_at=NOW, revoked_by="bob"),)}, 3, "no active"),
        ({"repository_id": 111}, 3, "gate record is for repository 987654321"),
        ({"expected_version": "2.1.319"}, 3, "for agent 2.1.318"),
        ({"expected_version": None}, 3, "for agent 2.1.318"),
        ({"live_repository_id": 111}, 3, "a fork or another repository"),
        ({"live_repository_id": "404 Not Found"}, 3, "cannot be read"),
        ({"kill_switch_engaged": True}, 78, "kill switch"),
        ({"suspension": Suspension(SuspensionReason.OPERATOR, NOW)}, 78, "operator-suspended"),
        ({"now": NOW - timedelta(seconds=1)}, 78, "clock is behind"),
    ],
)  # fmt: skip
def test_each_missing_condition_refuses_with_its_code(
    changes: dict[str, Any], code: int, reason: str
) -> None:
    refused = loop_blocker(facts(**changes))
    assert refused is not None
    assert refused[0] == code
    assert reason in refused[1]


# -- the loop ---------------------------------------------------------------------------


def drive(checks: list[tuple[int, str] | None], codes: list[int]) -> tuple[int, int, list[str]]:
    passes: list[int] = [0]
    lines: list[str] = []
    stop = threading.Event()

    def check() -> tuple[int, str] | None:
        if not checks:
            stop.set()
            return None
        return checks.pop(0)

    def run_pass() -> int:
        passes[0] += 1
        return codes.pop(0) if codes else 0

    code = run_loop(check=check, run_pass=run_pass, poll_interval=0.001, stop=stop,
                    report=lines.append)  # fmt: skip
    return code, passes[0], lines


def test_passes_run_one_after_another_until_a_check_refuses() -> None:
    code, passes, lines = drive([None, None, (3, "revoked")], [0, 1])
    assert (code, passes) == (3, 2)  # a failed execution (1) is not a reason to stop
    assert lines == ["loop stopped: revoked"]


@pytest.mark.parametrize("bad", [2, 3, 78])
def test_a_pass_needing_a_person_ends_the_loop(bad: int) -> None:
    code, passes, _ = drive([None, None, None], [0, bad, 0])
    assert (code, passes) == (bad, 2)


def test_a_signal_stops_the_loop_cleanly() -> None:
    stop = threading.Event()
    stop.set()
    assert run_loop(check=lambda: None, run_pass=lambda: 0, poll_interval=60, stop=stop,
                    report=lambda _: None) == 0  # fmt: skip


# -- the command ------------------------------------------------------------------------

CONFIG = """
[ilmar]
state_dir = "{state}"
[limits]
max_concurrent_executions = 1
[autonomy]
mode = "{mode}"
poll_interval_seconds = 1
repository_id = 987654321
[budget]
daily_usd = 20.0
monthly_usd = 100.0
reserve_per_execution_usd = 1.0
provider_cap_attested_usd = 100.0
kill_switch_file = "{kill}"
[verification]
image = "localhost/verify:1"
[[verification.commands]]
name = "tests"
job = "unit"
argv = ["pytest"]
timeout_seconds = 600
{adapters}
"""


@pytest.fixture
def world(tmp_path: Path) -> dict[str, Any]:
    tmp = tmp_path.resolve()
    (tmp / "state").mkdir(mode=0o700)
    ports = ("task_provider", "source_control", "container_runtime", "execution_store")
    adapters = "".join(f'[adapters.{port}]\nkind = "fake"\n' for port in ports)
    adapters += '[adapters.agent_runner]\nkind = "fake"\n'
    adapters += 'options = { expected_version = "2.1.318" }\n'
    store = InMemoryExecutionStore()
    scm = FakeSourceControl()
    scm.seed(REPO, "main", BASE_FILES)
    scm.repository_ids = {REPO.slug: REPO_ID}
    return {"tmp": tmp, "adapters": adapters, "store": store, "scm": scm}


def config(world: dict[str, Any], mode: str = "supervised") -> Path:
    path: Path = world["tmp"] / "ilmar.toml"
    text = CONFIG.format(state=world["tmp"] / "state", mode=mode, kill=world["tmp"] / "KILL",
                         adapters=world["adapters"])  # fmt: skip
    path.write_text(text)
    return path


def builder(world: dict[str, Any]) -> Callable[[Any], Components]:
    def build(cfg: Any) -> Components:
        layout = WorkspaceLayout.under(cfg.state_dir)
        layout.workspaces_root.mkdir(parents=True, exist_ok=True)
        return Components(
            instance_id="inst", layout=layout, task_provider=FakeTaskProvider((task("1"),)),
            source_control=world["scm"], container_runtime=WorkspaceRuntime(),
            agent_runner=ScriptedAgent(), execution_store=world["store"],
            agent_sandbox=AgentSandbox("localhost/agent:1", NetworkPolicy.ALLOWLIST,
                                       ("api.example",)),
            target=Target(REPO, "main"), verification=policy(),
        )  # fmt: skip

    return build


def gate(world: dict[str, Any], change: GateRecorded | GateRevoked) -> None:
    store = world["store"]
    layout = WorkspaceLayout.under(world["tmp"] / "state")
    with Ownership.take(layout=layout, store=store, instance_id="inst", now=NOW) as owner:
        context = EventContext(at=NOW, instance="inst", epoch=owner.fence.epoch,
                               actor=Actor(ActorKind.HUMAN, "alice"))  # fmt: skip
        store.commit(owner.fence, Commit(gate=change), context=context)


ANY_RATE = StartLimits(min_interval_seconds=0)


def loop(world: dict[str, Any], *, passes: int = 1, mode: str = "supervised",
         limits: StartLimits = ANY_RATE,
         between: Callable[[Path], None] | None = None) -> tuple[int, str]:  # fmt: skip
    """``between(config)`` runs right before the second check, after one pass."""
    stop = threading.Event()
    seen = [0]
    real = world["scm"].repository_id
    path = world["tmp"] / "ilmar.toml"

    def counting(repository: Any) -> int:  # stops the loop after ``passes`` checks
        seen[0] += 1
        if seen[0] == 2 and between is not None:
            between(path)
        if seen[0] > passes:
            stop.set()
        return int(real(repository))

    world["scm"].repository_id = counting
    err = io.StringIO()
    try:
        code = cli_loop.run(config(world, mode), now=lambda: NOW, stop=stop,
                            builder=builder(world), boot_id=lambda: "boot-1", limits=limits,
                            out=io.StringIO(), err=err)  # fmt: skip
    finally:
        world["scm"].repository_id = real
    return code, err.getvalue()


def worked(world: dict[str, Any]) -> bool:
    return any(e.type.value == "execution.created" for e in world["store"].events())


def test_manual_mode_never_loops_and_records_no_start(world: dict[str, Any]) -> None:
    code, err = loop(world, mode="manual")
    assert code == 3
    assert "--once` is unchanged" in err
    assert not start_log_path(world["tmp"] / "state" / "coordinator").exists()


def test_supervised_without_a_gate_record_is_not_enabled(world: dict[str, Any]) -> None:
    code, err = loop(world)
    assert (code, "no active supervised gate record" in err) == (3, True)
    assert start_log_path(world["tmp"] / "state" / "coordinator").exists()  # recorded first


def test_with_a_gate_the_loop_runs_passes_until_stopped(world: dict[str, Any]) -> None:
    gate(world, GateRecorded(EVIDENCE, NOW))
    code, err = loop(world, passes=2)
    assert code == 0
    assert "stopped by a signal" in err
    assert worked(world)  # a pass did work


def test_a_fork_is_never_the_approved_target(world: dict[str, Any]) -> None:
    gate(world, GateRecorded(EVIDENCE, NOW))
    world["scm"].repository_ids = {REPO.slug: 111}  # same name, another repository
    code, err = loop(world)
    assert (code, "a fork or another repository" in err) == (3, True)
    assert not worked(world)


def test_a_revoked_record_disables_the_loop(world: dict[str, Any]) -> None:
    gate(world, GateRecorded(EVIDENCE, NOW))
    gate(world, GateRevoked("supervised", NOW))
    code, _ = loop(world)
    assert code == 3


def test_the_kill_switch_between_passes_suspends(world: dict[str, Any]) -> None:
    gate(world, GateRecorded(EVIDENCE, NOW))
    (world["tmp"] / "KILL").write_text("")
    code, err = loop(world)
    assert (code, "kill switch" in err) == (78, True)


def test_a_restart_loop_suspends_before_any_work(world: dict[str, Any]) -> None:
    gate(world, GateRecorded(EVIDENCE, NOW))
    tight = StartLimits(min_interval_seconds=3600)
    assert loop(world, limits=tight)[0] == 0
    code, err = loop(world, limits=tight)
    assert (code, "restart loop" in err) == (78, True)
    held = world["store"].suspension()
    assert held is not None
    assert held.reason is SuspensionReason.RESTART_LOOP


def test_two_loops_on_one_store_have_one_owner(world: dict[str, Any]) -> None:
    gate(world, GateRecorded(EVIDENCE, NOW))
    layout = WorkspaceLayout.under(world["tmp"] / "state")
    with Ownership.take(layout=layout, store=world["store"], instance_id="inst", now=NOW):
        code, err = loop(world)
    assert (code, "is another loop running" in err) == (3, True)


def _to_manual(path: Path) -> None:
    path.write_text(path.read_text().replace('mode = "supervised"', 'mode = "manual"'))


def _corrupt(path: Path) -> None:
    path.write_text("[autonomy\n")


def _remove(path: Path) -> None:
    path.unlink()


def _same_meaning(path: Path) -> None:  # even a harmless edit is never applied live
    path.write_text(path.read_text() + "\n# a comment\n")


@pytest.mark.parametrize("change", [_to_manual, _corrupt, _remove, _same_meaning])
def test_a_configuration_change_while_running_stops_before_the_next_pass(
    world: dict[str, Any], change: Callable[[Path], None]
) -> None:
    gate(world, GateRecorded(EVIDENCE, NOW))
    code, err = loop(world, passes=3, between=change)
    assert code == 3
    assert "configuration changed, vanished or cannot be read" in err
    created = [e for e in world["store"].events() if e.type.value == "execution.created"]
    assert len(created) == 1  # the first pass ran; no pass after the change


# -- uncertain spend cannot be bypassed (ADR-0039 with ADR-0040) -------------------------


def uncertain_spend(world: dict[str, Any], *, also: SuspensionReason | None) -> None:
    """An uncertain settlement, as the worker records it, while ``also`` (another
    suspension) may already be in force."""
    from ilmar.domain.events import COORDINATOR, Event, EventType
    from ilmar.domain.execution import Execution, ExecutionState, Transition
    from ilmar.domain.tasks import TaskRef

    store = world["store"]
    layout = WorkspaceLayout.under(world["tmp"] / "state")
    with Ownership.take(layout=layout, store=store, instance_id="inst", now=NOW) as owner:
        queued = Execution("x1", TaskRef("fake", "9"), ExecutionState.QUEUED, 1, 1, NOW, NOW)
        store.commit(owner.fence, Commit(created=(
            (queued, Transition("x1", None, ExecutionState.QUEUED, NOW, "queued")),)))  # fmt: skip
        if also is not None:
            store.commit(owner.fence, Commit(suspend=Suspension(also, NOW)))
        spend = Event.create(
            EventType.EXECUTION_MODEL_SPEND, at=NOW, instance="inst", epoch=owner.fence.epoch,
            actor=COORDINATOR, pass_id="p-0123456789", execution="x1", attempt=1,
            reason="spend-uncertain",
            data={"requests": 1, "charged_micros": 0, "unsettled_micros": 1,
                  "overrun_micros": 0, "extra_micros": 0},
        )  # fmt: skip
        suspend = None if also else Suspension(SuspensionReason.SPEND_UNCERTAIN, NOW)
        store.commit(owner.fence, Commit(extra_events=(spend,), suspend=suspend))


@pytest.mark.parametrize(
    "also", [None, SuspensionReason.OPERATOR, SuspensionReason.KILL_SWITCH,
             SuspensionReason.RESTART_LOOP, SuspensionReason.CLOCK_ANOMALY],
)  # fmt: skip
def test_unreviewed_uncertain_spend_keeps_the_loop_stopped_whatever_the_suspension(
    world: dict[str, Any], also: SuspensionReason | None
) -> None:
    """The loop relies on the suspension. Every way to clear one is
    ``ilmar resume`` (see the architecture test), and it refuses, whatever the
    suspension's reason, until the spend is reviewed: so the loop never runs
    another pass over unreviewed uncertain spend."""
    from ilmar.core.suspension import resume_blocker, unreviewed_spend
    from ilmar.domain.budget import BudgetTotals

    gate(world, GateRecorded(EVIDENCE, NOW))
    uncertain_spend(world, also=also)
    held = world["store"].suspension()
    assert held is not None
    pending = unreviewed_spend(world["store"].events())
    assert len(pending) == 1
    for flags in ({}, {"provider_cap_raised": True, "check_fault_resolved": True}):
        blocker = resume_blocker(
            held, now=NOW + timedelta(days=2), totals=BudgetTotals(), limits=None,
            latest_update=NOW, kill_switch_present=False, restart_breach=None,
            unreviewed=len(pending), **{"provider_cap_raised": False, **flags},
        )  # fmt: skip
        assert blocker is not None
        assert "--provider-spend-reviewed" in blocker
    code, err = loop(world)
    assert (code, "suspended" in err) == (78, True)
    assert not worked_beyond(world, "x1")


def worked_beyond(world: dict[str, Any], *known: str) -> bool:
    return any(e.type.value == "execution.created" and e.execution not in known
               for e in world["store"].events())  # fmt: skip
