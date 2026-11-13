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
from ilmar.domain.events import ActorKind, EventType
from ilmar.domain.ledger import Commit

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
