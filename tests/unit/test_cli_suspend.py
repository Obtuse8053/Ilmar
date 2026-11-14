# SPDX-License-Identifier: Apache-2.0
"""``ilmar suspend``, ``ilmar resume`` and ``ilmar budget`` (ADR-0029 §3, #18).

They open only the execution store, take the instance lock for a write, and
record a person as the actor. ``resume`` lifts a suspension only when its cause
is gone; a budget suspension never lifts by itself.
"""

from __future__ import annotations

import argparse
import io
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from ilmar import cli_suspend
from ilmar.adapters.sqlite import SQLiteExecutionStore
from ilmar.cli import _build_parser
from ilmar.core.layout import WorkspaceLayout
from ilmar.core.ownership import InstanceLock, Ownership
from ilmar.domain.budget import Suspension, SuspensionReason
from ilmar.domain.events import COORDINATOR, ActorKind, Event, EventType
from ilmar.domain.execution import Execution, ExecutionState, Transition
from ilmar.domain.ledger import Commit
from ilmar.domain.tasks import TaskRef

REPO_ROOT = Path(__file__).resolve().parents[2]
NOW = datetime(2026, 3, 1, 12, tzinfo=UTC)
BUDGET = """
[budget]
daily_usd = 2.0
monthly_usd = 10.0
reserve_per_execution_usd = 1.0
provider_cap_attested_usd = 10.0
"""


@pytest.fixture
def state(tmp_path: Path) -> Path:
    return tmp_path.resolve() / ".ilmar"


@pytest.fixture
def config(tmp_path: Path, state: Path) -> Path:
    path = tmp_path.resolve() / "ilmar.toml"
    path.write_text((REPO_ROOT / "ilmar.example.toml").read_text() + BUDGET)
    return path


def ilmar(*argv: str, now: datetime = NOW) -> tuple[int, str, str]:
    args: argparse.Namespace = _build_parser().parse_args(list(argv))
    out, err = io.StringIO(), io.StringIO()
    code = cli_suspend.run(args, out=out, err=err, now=lambda: now)
    return code, out.getvalue(), err.getvalue()


def store(state: Path) -> SQLiteExecutionStore:
    return SQLiteExecutionStore(state / "executions.sqlite3")


def test_suspend_writes_the_record_and_its_event_as_a_person(config: Path, state: Path) -> None:
    code, out, _ = ilmar("suspend", "--config", str(config), "--as", "alice", "--note", "upgrade")
    assert (code, out.strip()) == (0, "suspended: no new work starts until 'ilmar resume'")
    with store(state) as opened:
        suspension = opened.suspension()
        assert suspension == Suspension(SuspensionReason.OPERATOR, NOW, "upgrade")
        (event,) = [e for e in opened.events() if e.type is EventType.INSTANCE_SUSPENDED]
    assert event.reason == "operator-suspended"
    assert (event.actor.kind, event.actor.ref) == (ActorKind.HUMAN, "alice")
    code, out, _ = ilmar("suspend", "--config", str(config), "--as", "alice")
    assert code == 0
    assert out.startswith("already suspended: operator-suspended")


def test_budget_reports_and_exits_78_while_suspended(config: Path) -> None:
    code, out, _ = ilmar("budget", "--config", str(config))
    assert (code, out.startswith("no execution store yet")) == (0, True)
    ilmar("suspend", "--config", str(config), "--as", "alice")
    code, out, _ = ilmar("budget", "--config", str(config))
    assert code == 78
    assert "0 of 2 USD reserved" in out
    assert "suspended: operator-suspended" in out
    ilmar("resume", "--config", str(config), "--as", "alice")
    code, out, _ = ilmar("budget", "--config", str(config))
    assert (code, "not suspended" in out) == (0, True)


def test_resume_lifts_an_operator_suspension(config: Path, state: Path) -> None:
    ilmar("suspend", "--config", str(config), "--as", "alice")
    code, out, _ = ilmar("resume", "--config", str(config), "--as", "bob",
                         now=NOW + timedelta(minutes=1))  # fmt: skip
    assert code == 0
    assert "operator-suspended suspension is lifted" in out
    with store(state) as opened:
        assert opened.suspension() is None
        (event,) = [e for e in opened.events() if e.type is EventType.INSTANCE_RESUMED]
    assert dict(event.data) == {"cleared": "operator-suspended"}
    assert event.actor.ref == "bob"
    code, out, _ = ilmar("resume", "--config", str(config), "--as", "bob")
    assert (code, out.strip()) == (0, "not suspended")


def suspend_with(state: Path, reason: SuspensionReason) -> None:
    state.mkdir(exist_ok=True)
    with store(state) as opened:
        ownership = Ownership.take(layout=WorkspaceLayout.under(state), store=opened,
                                   instance_id="default", now=NOW)  # fmt: skip
        with ownership:
            opened.commit(ownership.fence, Commit(suspend=Suspension(reason, NOW)))


def test_a_provider_cap_suspension_needs_the_operator_to_confirm(config: Path, state: Path) -> None:
    suspend_with(state, SuspensionReason.PROVIDER_CAP)
    code, _, err = ilmar("resume", "--config", str(config), "--as", "bob")
    assert code == 78
    assert "--provider-cap-resolved" in err
    code, _, _ = ilmar("resume", "--config", str(config), "--as", "bob",
                       "--provider-cap-resolved")  # fmt: skip
    assert code == 0


def test_a_failed_suspension_check_needs_the_operator_and_a_clean_check(
    config: Path, state: Path, tmp_path: Path
) -> None:
    """#122 review: operator intervention, then a check that succeeds."""
    kill = tmp_path / "KILL"
    config.write_text(config.read_text() + f'kill_switch_file = "{kill}"\n')
    suspend_with(state, SuspensionReason.SUSPENSION_CHECK_FAILED)
    code, _, err = ilmar("resume", "--config", str(config), "--as", "bob")
    assert code == 78
    assert "--check-fault-resolved" in err
    kill.write_text("")  # the check does not pass: the kill switch reads engaged
    code, _, err = ilmar("resume", "--config", str(config), "--as", "bob",
                         "--check-fault-resolved")  # fmt: skip
    assert code == 78
    assert "kill switch" in err
    kill.unlink()
    code, out, _ = ilmar("resume", "--config", str(config), "--as", "bob",
                         "--check-fault-resolved")  # fmt: skip
    assert code == 0
    assert "suspension-check-failed suspension is lifted" in out
    with store(state) as opened:
        assert opened.suspension() is None


def test_a_budget_suspension_lifts_only_when_a_reservation_fits(config: Path, state: Path) -> None:
    """With nothing charged the allowance fits again at once; the rule itself
    (charges filling the day) is covered with the coordinator."""
    suspend_with(state, SuspensionReason.BUDGET_DAILY)
    code, out, _ = ilmar("resume", "--config", str(config), "--as", "bob")
    assert code == 0
    assert "budget-daily suspension is lifted" in out


def test_a_budget_suspension_without_a_budget_stays(tmp_path: Path, state: Path) -> None:
    plain = tmp_path.resolve() / "plain.toml"
    plain.write_text((REPO_ROOT / "ilmar.example.toml").read_text())
    suspend_with(state, SuspensionReason.BUDGET_MONTHLY)
    code, _, err = ilmar("resume", "--config", str(plain), "--as", "bob")
    assert code == 78
    assert "cannot be checked" in err


def test_the_commands_refuse_while_a_coordinator_holds_the_instance(
    config: Path, state: Path
) -> None:
    state.mkdir()
    lock = InstanceLock.acquire(WorkspaceLayout.under(state))
    try:
        code, _, err = ilmar("suspend", "--config", str(config), "--as", "alice")
    finally:
        lock.release()
    assert code == 3
    assert "cannot own this instance" in err


@pytest.mark.parametrize("name", ["Alice", "a b", "", "x" * 40])
def test_the_actor_label_is_validated(config: Path, name: str) -> None:
    code, _, err = ilmar("suspend", "--config", str(config), "--as", name)
    assert code == 3
    assert "--as" in err


# -- uncertain gateway spend (ADR-0039) -----------------------------------------------


def uncertain_spend(state: Path, *, suspend: SuspensionReason | None) -> None:
    """What a settlement records when an attempt's gateway spend is uncertain:
    the event, and a suspension unless ``suspend`` is another one in force."""
    state.mkdir(exist_ok=True)
    with store(state) as opened:
        ownership = Ownership.take(layout=WorkspaceLayout.under(state), store=opened,
                                   instance_id="default", now=NOW)  # fmt: skip
        with ownership:
            event = Event.create(
                EventType.EXECUTION_MODEL_SPEND, at=NOW, instance="default",
                epoch=ownership.fence.epoch, actor=COORDINATOR, pass_id="p-0123456789",
                execution="e1", attempt=1, reason="spend-uncertain",
                data={"requests": 1, "charged_micros": 0, "unsettled_micros": 500,
                      "overrun_micros": 0, "extra_micros": 0},
            )  # fmt: skip
            queued = Execution("e1", TaskRef("github", "o/r#1"), ExecutionState.QUEUED, 1, 1,
                               NOW, NOW)  # fmt: skip
            opened.commit(ownership.fence, Commit(created=(
                (queued, Transition("e1", None, ExecutionState.QUEUED, NOW, "queued")),
            )))  # fmt: skip
            if suspend is not None:
                opened.commit(ownership.fence, Commit(suspend=Suspension(suspend, NOW)))
            opened.commit(
                ownership.fence,
                Commit(
                    extra_events=(event,),
                    suspend=None if suspend else Suspension(SuspensionReason.SPEND_UNCERTAIN, NOW),
                ),
            )


def test_uncertain_spend_needs_the_operator_to_review_the_provider_s_figures(
    config: Path, state: Path
) -> None:
    uncertain_spend(state, suspend=None)
    code, out, _ = ilmar("budget", "--config", str(config))
    assert code == 78
    assert "uncertain gateway spend awaiting review: execution e1 attempt 1" in out
    code, _, err = ilmar("resume", "--config", str(config), "--as", "bob")
    assert code == 78
    assert "--provider-spend-reviewed" in err
    assert "execution e1 attempt 1" in err
    code, out, _ = ilmar("resume", "--config", str(config), "--as", "bob",
                         "--provider-spend-reviewed")  # fmt: skip
    assert code == 0
    assert "spend-uncertain suspension is lifted" in out
    with store(state) as opened:
        (review,) = [e for e in opened.events() if e.type is EventType.EXECUTION_SPEND_REVIEWED]
        spend = next(e for e in opened.events() if e.type is EventType.EXECUTION_MODEL_SPEND)
    assert (review.execution, review.attempt, review.reason) == ("e1", 1, "spend-reviewed")
    assert review.data["spend_seq"] == spend.seq
    assert (review.actor.kind, review.actor.ref) == (ActorKind.HUMAN, "bob")
    code, out, _ = ilmar("budget", "--config", str(config))
    assert (code, "awaiting review" in out) == (0, False)


def test_another_suspension_cannot_hide_uncertain_spend(config: Path, state: Path) -> None:
    """The spend became uncertain while an operator suspension was in force: no
    spend-uncertain suspension could be written, yet the operator's own resume
    is still held until the spend is reviewed."""
    uncertain_spend(state, suspend=SuspensionReason.OPERATOR)
    with store(state) as opened:
        held = opened.suspension()
        assert held is not None
        assert held.reason is SuspensionReason.OPERATOR
    code, _, err = ilmar("resume", "--config", str(config), "--as", "bob")
    assert code == 78
    assert "--provider-spend-reviewed" in err
    code, _, _ = ilmar("resume", "--config", str(config), "--as", "bob",
                       "--provider-spend-reviewed")  # fmt: skip
    assert code == 0
