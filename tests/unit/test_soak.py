# SPDX-License-Identifier: Apache-2.0
"""A fake-provider soak of unattended operation (#96, Gate 2 rehearsal).

No credential, network or container. The real ``ilmar run --loop`` runs again and
again, as an outside supervisor would restart it, against fakes and an in-memory
store, on a simulated clock. A seeded schedule injects faults: agent failures,
provider spend-cap refusals, rate limits, a kill switch, operator suspensions and
quiet days. A scripted operator answers each stop only through the real resume
rule, and every invariant is checked after every process lifetime:

* each exit code is one Ilmar defines, and only 0, 1 and 78 occur here;
* a suspension stops all new work until the operator resumes it, and a resume is
  refused while its cause is still there (the kill switch, an exhausted budget);
* what is charged never exceeds the daily or monthly allowance;
* at most one execution is ever in progress;
* nothing is ever merged by Ilmar;
* the start log accounts for every start.

``ILMAR_SOAK_LIFETIMES`` (default 12) and ``ILMAR_SOAK_SEED`` (default 1) make it
longer or different; the default run takes a few seconds.
"""

from __future__ import annotations

import io
import os
import random
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from coordinator_support import BASE_FILES, REPO, ScriptedAgent, WorkspaceRuntime, policy, task
from ilmar import cli_loop
from ilmar.adapters.memory import InMemoryExecutionStore
from ilmar.core import kill_switch
from ilmar.core.components import AgentSandbox, Components, Target
from ilmar.core.coordinator import ExitCode
from ilmar.core.layout import WorkspaceLayout
from ilmar.core.loop import run_loop
from ilmar.core.ownership import Ownership
from ilmar.core.starts import StartLimits, read_starts, start_log_path
from ilmar.core.suspension import resume_blocker
from ilmar.domain.agent import AgentOutcome
from ilmar.domain.budget import BudgetLimits, Suspension, SuspensionReason
from ilmar.domain.changes import ChangeRequestState
from ilmar.domain.events import Actor, ActorKind, EventContext
from ilmar.domain.execution import ExecutionState
from ilmar.domain.gates import GateEvidence, GateRecorded
from ilmar.domain.ledger import Commit
from ilmar.domain.workspace import NetworkPolicy

from fakes import FakeSourceControl, FakeTaskProvider

START = datetime(2026, 3, 2, 8, tzinfo=UTC)  # a Monday
REPO_ID = 987654321
EVIDENCE = GateEvidence(
    mode="supervised", repository_id=REPO_ID, repository="owner/repo", agent_version="2.1.318",
    evidence="https://github.com/Obtuse8053/Ilmar/pull/1",
    evidence_commit="0123456789abcdef0123456789abcdef01234567",
    live_test="https://github.com/Obtuse8053/Ilmar/actions/runs/1",
    fixture="tests/fixtures/claude_code_streams/2.1.318-recorded.json",
    fixture_sha256="a" * 64, gate1="https://github.com/Obtuse8053/Ilmar/issues/93",
)  # fmt: skip
DAILY_USD, MONTHLY_USD, RESERVE_USD = 6, 40, 1
CONFIG = """
[ilmar]
state_dir = "{state}"
[limits]
max_concurrent_executions = 1
[autonomy]
mode = "supervised"
poll_interval_seconds = 1
repository_id = {repo_id}
[budget]
daily_usd = {daily}
monthly_usd = {monthly}
reserve_per_execution_usd = {reserve}
provider_cap_attested_usd = {monthly}
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
FAULTS = ("none", "none", "agent-failure", "provider-cap", "rate-limited", "kill-switch",
          "operator", "quiet-day")  # fmt: skip
OUTCOMES = {
    "agent-failure": AgentOutcome.FAILED,
    "provider-cap": AgentOutcome.PROVIDER_REFUSED,
    "rate-limited": AgentOutcome.RATE_LIMITED,
}
EDIT = {"src/app.py": "print('fixed')\n"}


@dataclass
class World:
    tmp: Path
    clock: list[datetime]
    store: InMemoryExecutionStore = field(default_factory=InMemoryExecutionStore)
    scm: FakeSourceControl = field(default_factory=FakeSourceControl)
    tasks: FakeTaskProvider = field(
        default_factory=lambda: FakeTaskProvider(tuple(task(str(n)) for n in range(1, 81)))
    )
    plan: list[tuple[AgentOutcome, dict[str, str]]] = field(default_factory=list)
    log: list[str] = field(default_factory=list)

    @property
    def now(self) -> datetime:
        return self.clock[0]

    @property
    def state(self) -> Path:
        return self.tmp / "state"

    @property
    def kill_file(self) -> Path:
        return self.tmp / "KILL"

    @property
    def layout(self) -> WorkspaceLayout:
        return WorkspaceLayout.under(self.state)

    def config_path(self) -> Path:
        ports = ("task_provider", "source_control", "container_runtime", "execution_store")
        adapters = "".join(f'[adapters.{port}]\nkind = "fake"\n' for port in ports)
        adapters += (
            '[adapters.agent_runner]\nkind = "fake"\noptions = { expected_version = "2.1.318" }\n'
        )
        path = self.tmp / "ilmar.toml"
        path.write_text(CONFIG.format(
            state=self.state, repo_id=REPO_ID, daily=DAILY_USD, monthly=MONTHLY_USD,
            reserve=RESERVE_USD, kill=self.kill_file, adapters=adapters,
        ))  # fmt: skip
        return path

    def build(self, cfg: Any) -> Components:
        self.layout.workspaces_root.mkdir(parents=True, exist_ok=True)
        return Components(
            instance_id="inst", layout=self.layout, task_provider=self.tasks,
            source_control=self.scm, container_runtime=WorkspaceRuntime(),
            agent_runner=ScriptedAgent(plan=list(self.plan) or [(AgentOutcome.COMPLETED, EDIT)]),
            execution_store=self.store,
            agent_sandbox=AgentSandbox("localhost/agent:1", NetworkPolicy.ALLOWLIST,
                                       ("api.example",)),
            target=Target(REPO, "main"), verification=policy(),
        )  # fmt: skip

    def human(self) -> Ownership:
        return Ownership.take(layout=self.layout, store=self.store, instance_id="inst",
                              now=self.now)  # fmt: skip

    def context(self, owner: Ownership) -> EventContext:
        return EventContext(at=self.now, instance="inst", epoch=owner.fence.epoch,
                            actor=Actor(ActorKind.HUMAN, "operator"))  # fmt: skip


@pytest.fixture(autouse=True)
def no_real_waiting(monkeypatch: pytest.MonkeyPatch) -> None:
    """The loop waits its poll interval on the real clock; the soak's clock is
    simulated, so the wait between passes is shortened."""

    def quick(**kwargs: Any) -> int:
        return run_loop(**{**kwargs, "poll_interval": 0.0005})

    monkeypatch.setattr("ilmar.cli_loop.run_loop", quick)


@pytest.fixture
def world(tmp_path: Path) -> World:
    tmp = tmp_path.resolve()
    (tmp / "state").mkdir(mode=0o700)
    w = World(tmp=tmp, clock=[START])
    w.scm.seed(REPO, "main", BASE_FILES)
    w.scm.repository_ids = {REPO.slug: REPO_ID}
    with w.human() as owner:  # the maintainer records the gate once
        w.store.commit(owner.fence, Commit(gate=GateRecorded(EVIDENCE, w.now)),
                       context=w.context(owner))  # fmt: skip
    return w


def lifetime(w: World, *, passes: int, during: Callable[[int], None] | None = None) -> int:
    """One process lifetime of ``ilmar run --loop``: stopped (signal, exit 0) after
    ``passes`` checks unless it stops by itself first. ``during(n)`` runs before
    check ``n``. The clock moves ten minutes a check."""
    stop = threading.Event()
    seen = [0]
    real = w.scm.repository_identity

    def counting(repository: Any) -> Any:
        seen[0] += 1
        if during is not None:
            during(seen[0])
        if seen[0] > passes:
            stop.set()
        w.clock[0] += timedelta(minutes=10)
        return real(repository)

    w.scm.repository_identity = counting  # type: ignore[method-assign]
    started = w.now
    try:
        code = cli_loop.run(w.config_path(), now=lambda: w.now, stop=stop, builder=w.build,
                            boot_id=lambda: "boot-1", limits=StartLimits(),
                            out=io.StringIO(), err=io.StringIO())  # fmt: skip
    finally:
        w.scm.repository_identity = real  # type: ignore[method-assign]
    starts = read_starts(start_log_path(w.layout.coordinator_root))
    assert any(r.at == started for r in starts), "every process start is on record"
    return code


def try_resume(w: World, **answers: bool) -> str | None:
    """The operator's ``ilmar resume``: the real rule decides; ``None`` if lifted."""
    with w.human() as owner:
        current = w.store.suspension()
        if current is None:
            return None
        blocker = resume_blocker(
            current, now=w.now, totals=w.store.budget_totals(w.now), limits=LIMITS,
            latest_update=w.store.latest_update(),
            provider_cap_raised=answers.get("provider_cap", False),
            kill_switch_present=kill_switch.checker(w.kill_file)(),
        )  # fmt: skip
        if blocker is None:
            w.store.commit(owner.fence, Commit(resume=w.now), context=w.context(owner))
        return blocker


LIMITS = BudgetLimits(daily_micros=DAILY_USD * 1_000_000, monthly_micros=MONTHLY_USD * 1_000_000,
                      reserve_micros=RESERVE_USD * 1_000_000)  # fmt: skip


def all_executions(w: World) -> list[Any]:
    return list(w.store.list_in_states(tuple(ExecutionState)))


def check_invariants(w: World, code: int) -> None:
    assert code in {int(c) for c in ExitCode}, code
    assert code in (0, 1, 78), f"exit {code} needs a person: {w.log[-3:]}"
    totals = w.store.budget_totals(w.now)
    assert totals.day_micros <= LIMITS.daily_micros
    assert totals.month_micros <= LIMITS.monthly_micros
    active = [e for e in all_executions(w) if not e.state.is_terminal]
    in_progress = [e for e in active if e.state not in (ExecutionState.QUEUED,
                                                         ExecutionState.RETRY_WAIT)]  # fmt: skip
    assert len(in_progress) <= 1, in_progress
    merged = [r for r in w.scm._change_requests.values() if r.state is ChangeRequestState.MERGED]
    assert merged == []  # Mode 1: Ilmar never merges


def suspended_does_no_work(w: World) -> None:
    """While suspended, a fresh process starts nothing."""
    before = len(all_executions(w))
    assert lifetime(w, passes=2) == 78
    assert len(all_executions(w)) == before


def next_day(w: World) -> None:
    w.clock[0] = (w.now + timedelta(days=1)).replace(hour=8, minute=0)


def operate(w: World, rng: random.Random, number: int) -> None:
    fault = rng.choice(FAULTS)
    w.log.append(f"{number}: {fault}")
    w.plan = [(OUTCOMES.get(fault, AgentOutcome.COMPLETED), EDIT), (AgentOutcome.COMPLETED, EDIT)]
    if fault == "quiet-day":
        next_day(w)
        return
    during = None
    if fault == "kill-switch":

        def during(n: int) -> None:
            if n == 2:
                w.kill_file.write_text("stop")
    elif fault == "operator":  # `ilmar suspend` needs the lock: between processes
        with w.human() as owner:
            suspension = Suspension(SuspensionReason.OPERATOR, w.now, "soak")
            w.store.commit(owner.fence, Commit(suspend=suspension), context=w.context(owner))
    code = lifetime(w, passes=rng.randint(1, 4), during=during)
    check_invariants(w, code)
    if code != 78:
        return
    current = w.store.suspension()
    if current is None:  # the kill switch stops the loop even before it is recorded
        assert w.kill_file.exists(), "exit 78 with neither a suspension nor a kill switch"
        reason = SuspensionReason.KILL_SWITCH
    else:
        reason = current.reason
    w.log.append(f"   suspended: {reason.value}")
    suspended_does_no_work(w)
    if reason is SuspensionReason.KILL_SWITCH:
        if current is not None:
            assert try_resume(w) is not None  # refused while the file is there
        w.kill_file.unlink()
    elif reason in (SuspensionReason.BUDGET_DAILY, SuspensionReason.BUDGET_MONTHLY):
        assert try_resume(w) is not None  # refused while the allowance is used up
        next_day(w)
        if try_resume(w) is not None:  # the month is used up too: wait for the next one
            w.log.append("   monthly allowance used up")
            first = w.now.replace(day=1) + timedelta(days=32)
            w.clock[0] = first.replace(day=1, hour=8, minute=0)
    blocker = try_resume(w, provider_cap=reason is SuspensionReason.PROVIDER_CAP)
    assert blocker is None, f"{reason.value}: {blocker}"


def test_an_unattended_soak_keeps_every_invariant(world: World) -> None:
    lifetimes = int(os.environ.get("ILMAR_SOAK_LIFETIMES", "12"))
    rng = random.Random(int(os.environ.get("ILMAR_SOAK_SEED", "1")))  # noqa: S311 - a schedule
    for number in range(lifetimes):
        operate(world, rng, number)
        world.clock[0] += timedelta(hours=2)  # the supervisor's delay, generously
    suspensions = [line for line in world.log if "suspended:" in line]
    assert suspensions, "the schedule must exercise suspensions"
    assert all_executions(world), "and real work"
