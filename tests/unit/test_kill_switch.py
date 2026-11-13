# SPDX-License-Identifier: Apache-2.0
"""The kill-switch file (ADR-0029 §4, #18 part 2).

While the file exists, Ilmar suspends with ``kill-switch``. It is checked
before each claim, before each container start and between passes. Presence
fails closed: anything but "no such file" counts as engaged. ``ilmar resume``
lifts the suspension only once the file is gone.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import pytest

from coordinator_support import Setup, make_setup, task
from ilmar.adapters.sqlite import SQLiteExecutionStore
from ilmar.config import parse_config
from ilmar.core import kill_switch
from ilmar.core.coordinator import ExitCode
from ilmar.core.suspension import resume_blocker
from ilmar.core.worker import Outcome
from ilmar.domain.agent import AgentOutcome
from ilmar.domain.budget import BudgetLimits, BudgetTotals, SuspensionReason
from ilmar.domain.errors import ConfigError
from ilmar.domain.events import EventType
from unit.test_config import minimal

LIMITS = BudgetLimits(daily_micros=5_000_000, monthly_micros=10_000_000, reserve_micros=1_000_000)
FIXED = {"src/app.py": "print('fixed')\n"}


# -- presence -------------------------------------------------------------------------


def test_an_absent_file_is_not_engaged(tmp_path: Path) -> None:
    assert not kill_switch.engaged(tmp_path / "KILL")
    assert not kill_switch.checker(None)()


@pytest.mark.parametrize("kind", ["file", "directory", "broken-link", "link"])
def test_anything_at_the_path_engages_it(tmp_path: Path, kind: str) -> None:
    path = tmp_path / "KILL"
    if kind == "file":
        path.write_text("")
    elif kind == "directory":
        path.mkdir()
    elif kind == "broken-link":
        path.symlink_to(tmp_path / "nowhere")
    else:
        (tmp_path / "target").write_text("")
        path.symlink_to(tmp_path / "target")
    assert kill_switch.engaged(path)
    assert kill_switch.checker(path)()


def test_a_path_that_cannot_be_checked_engages_it(tmp_path: Path) -> None:
    (tmp_path / "file").write_text("")
    assert kill_switch.engaged(tmp_path / "file" / "KILL")  # ENOTDIR: doubt means yes
    if os.geteuid() != 0:
        locked = tmp_path / "locked"
        locked.mkdir(mode=0o000)
        try:
            assert kill_switch.engaged(locked / "KILL")  # EACCES
        finally:
            locked.chmod(0o700)


# -- configuration --------------------------------------------------------------------

BUDGET = {"daily_usd": 5, "monthly_usd": 10, "reserve_per_execution_usd": 1,
          "provider_cap_attested_usd": 10}  # fmt: skip


def budget_with(**extra: object) -> object:
    data = minimal() | {"ilmar": {"state_dir": "/srv/ilmar"}, "budget": BUDGET | extra}
    config = parse_config(data, base_dir=Path("/etc/ilmar"))
    assert config.budget is not None
    return config.budget.kill_switch_file


def test_the_kill_switch_file_is_optional_and_resolved_against_the_config() -> None:
    assert budget_with() is None
    assert budget_with(kill_switch_file="KILL") == Path("/etc/ilmar/KILL")
    assert budget_with(kill_switch_file="/srv/ilmar/KILL") == Path("/srv/ilmar/KILL")


@pytest.mark.parametrize(
    "path",
    [
        "/srv/ilmar/workspaces/KILL",
        "/srv/ilmar/workspaces",
        "/srv/ilmar/coordinator/repos/github/KILL",
        "/srv/ilmar/other/../workspaces/x/KILL",
    ],
)
def test_a_kill_switch_a_sandbox_could_reach_is_refused(path: str) -> None:
    with pytest.raises(ConfigError, match="outside"):
        budget_with(kill_switch_file=path)


@pytest.mark.parametrize("value", ["", 5, True])
def test_a_malformed_kill_switch_is_refused(value: object) -> None:
    with pytest.raises(ConfigError, match="kill_switch_file"):
        budget_with(kill_switch_file=value)


# -- through the coordinator ----------------------------------------------------------


@pytest.fixture
def setup(tmp_path: Path) -> Iterator[Setup]:
    store = SQLiteExecutionStore(tmp_path / "executions.sqlite3")
    made = make_setup(tmp_path, tasks=(task("1"), task("2")), store=store)
    made.agent.plan = [(AgentOutcome.COMPLETED, FIXED)]
    yield made
    made.ownership.release()
    store.close()


def suspended_with(setup: Setup) -> str | None:
    current = setup.store.suspension()
    return None if current is None else current.reason.value


def test_an_engaged_kill_switch_suspends_before_any_claim(setup: Setup, tmp_path: Path) -> None:
    kill = tmp_path / "KILL"
    kill.write_text("")
    report = setup.coordinator(budget=LIMITS, kill_switch=kill_switch.checker(kill)).run_once()
    assert report.code is ExitCode.SUSPENDED
    assert suspended_with(setup) == "kill-switch"
    assert setup.store.get("e1") is None  # nothing claimed
    assert setup.runtime.started_specs == []  # nothing started
    assert setup.store.charges("e1") == ()
    events = [e.type for e in setup.store.events()]
    assert EventType.INSTANCE_SUSPENDED in events


def test_a_kill_switch_engaged_after_the_claim_stops_the_container_start(setup: Setup) -> None:
    """Checked again just before the attempt starts: the execution exists, but
    no reservation, no container and no attempt are written."""
    calls = iter([False])  # clear at the claim, engaged from then on

    def engaged() -> bool:
        return next(calls, True)

    report = setup.coordinator(budget=LIMITS, kill_switch=engaged).run_once()
    assert report.code is ExitCode.SUSPENDED
    assert [(r.outcome, r.code) for r in report.results] == [(Outcome.SUSPENDED, "kill-switch")]
    assert suspended_with(setup) == "kill-switch"
    assert setup.runtime.started_specs == []
    assert setup.store.charges("e1") == ()
    execution = setup.store.get("e1")
    assert execution is not None
    assert not execution.state.is_terminal  # waits, untouched, for the resume


def test_a_clear_kill_switch_changes_nothing(setup: Setup, tmp_path: Path) -> None:
    report = setup.coordinator(
        budget=LIMITS, kill_switch=kill_switch.checker(tmp_path / "KILL")
    ).run_once()
    assert report.code is ExitCode.SUCCESS
    assert suspended_with(setup) is None


def test_an_existing_suspension_is_kept_as_it_is(setup: Setup, tmp_path: Path) -> None:
    kill = tmp_path / "KILL"
    kill.write_text("")
    coordinator = setup.coordinator(budget=LIMITS, kill_switch=kill_switch.checker(kill))
    coordinator.run_once()
    first = setup.store.suspension()
    assert coordinator.run_once().code is ExitCode.SUSPENDED  # between passes: still suspended
    assert setup.store.suspension() == first


def test_resume_needs_the_file_gone(tmp_path: Path) -> None:
    from datetime import UTC, datetime

    from ilmar.domain.budget import Suspension

    now = datetime(2026, 1, 1, tzinfo=UTC)
    held = Suspension(SuspensionReason.KILL_SWITCH, now)
    kill = tmp_path / "KILL"
    kill.write_text("")

    def blocker() -> str | None:
        return resume_blocker(
            held, now=now, totals=BudgetTotals(), limits=LIMITS, latest_update=None,
            provider_cap_raised=False, kill_switch_present=kill_switch.checker(kill)(),
        )  # fmt: skip

    assert blocker() == "the kill-switch file still exists"
    kill.unlink()
    assert blocker() is None
