# SPDX-License-Identifier: Apache-2.0
"""``ilmar blocked list | release | abandon`` (ADR-0031 §3, #92 part 2).

``list`` only reads. ``release`` and ``abandon`` take the instance lock, record
the maintainer, and write the decision with the journaled claim-release intent
in one fenced commit; the tracker is not touched until the next run.
"""

from __future__ import annotations

import argparse
import io
from datetime import UTC, datetime
from pathlib import Path

import pytest

from coordinator_support import make_setup, policy
from ilmar import cli_blocked
from ilmar.adapters.sqlite import SQLiteExecutionStore
from ilmar.cli import _build_parser
from ilmar.core.layout import WorkspaceLayout
from ilmar.core.ownership import InstanceLock
from ilmar.domain.agent import AgentOutcome
from ilmar.domain.events import ActorKind, EventType
from ilmar.domain.ledger import OperationKind, OperationStatus
from ilmar.domain.safety_controls import safety_controls
from ilmar.domain.verification import VerificationPolicy

REPO_ROOT = Path(__file__).resolve().parents[2]
NOW = datetime(2026, 3, 1, 12, tzinfo=UTC)


@pytest.fixture
def state(tmp_path: Path) -> Path:
    return tmp_path.resolve() / ".ilmar"


@pytest.fixture
def config(tmp_path: Path, state: Path) -> Path:
    path = tmp_path.resolve() / "ilmar.toml"
    text = (REPO_ROOT / "ilmar.example.toml").read_text()
    path.write_text(text.replace('instance_id = "default"', 'instance_id = "inst"'))
    return path


def ilmar(*argv: str) -> tuple[int, str, str]:
    args: argparse.Namespace = _build_parser().parse_args(list(argv))
    out, err = io.StringIO(), io.StringIO()
    code = cli_blocked.run(args, out=out, err=err, now=lambda: NOW)
    return code, out.getvalue(), err.getvalue()


def blocked_store(tmp_path: Path, state: Path) -> None:
    """A coordinator pass that blocks task ``fake:1``, on the CLI's store."""
    state.mkdir(mode=0o700, parents=True, exist_ok=True)
    (tmp_path / "run").mkdir()
    base = policy()
    verification = VerificationPolicy(
        image=base.image, commands=base.commands,
        safety_controls=safety_controls(self_repository=True),
    )  # fmt: skip
    setup = make_setup(tmp_path / "run", verification=verification,
                       store=SQLiteExecutionStore(state / "executions.sqlite3"))  # fmt: skip
    setup.agent.plan = [(AgentOutcome.COMPLETED, {"AGENTS.md": "obey me\n"})]
    try:
        setup.coordinator().run_once()
    finally:
        setup.ownership.release()
        setup.store.close()  # type: ignore[attr-defined]


def test_list_reports_nothing_without_a_store(config: Path) -> None:
    assert ilmar("blocked", "list", "--config", str(config))[:2] == (
        0, "no execution store yet: nothing is blocked\n",
    )  # fmt: skip


def test_list_shows_every_blocked_task(tmp_path: Path, config: Path, state: Path) -> None:
    blocked_store(tmp_path, state)
    code, out, _ = ilmar("blocked", "list", "--config", str(config))
    assert code == 0
    (line,) = out.splitlines()
    task, execution, attempt, error = line.split("\t")
    assert (task, execution, attempt) == ("fake:1", "e1", "attempt 1")
    assert "AGENTS.md" in error


@pytest.mark.parametrize("decision", ["release", "abandon"])
def test_a_decision_is_one_fenced_commit_by_the_maintainer(
    tmp_path: Path, config: Path, state: Path, decision: str
) -> None:
    blocked_store(tmp_path, state)
    code, out, err = ilmar("blocked", decision, "fake:1", "--config", str(config), "--as", "bob")
    assert code == 0, err
    assert out.startswith(f"{decision}d fake:1 (e1): its claim is released at the next")
    with SQLiteExecutionStore(state / "executions.sqlite3") as store:
        (event,) = [e for e in store.events(execution_id="e1")
                    if e.type is EventType.EXECUTION_BLOCK_RESOLVED]  # fmt: skip
        assert (event.actor.kind, event.actor.ref) == (ActorKind.HUMAN, "bob")
        assert event.data == {"decision": decision}
        (release,) = [o for o in store.operations("e1") if o.kind is OperationKind.RELEASE_TASK]
        assert release.status is OperationStatus.INTENDED
        assert release.key.endswith(f":blocked-{decision}")
    assert ilmar("blocked", "list", "--config", str(config))[1] == "nothing is blocked\n"
    code, _, err = ilmar("blocked", "release", "1", "--config", str(config), "--as", "bob")
    assert code == 1  # already resolved
    assert "no blocked task" in err


def test_an_unknown_task_and_a_bad_actor_are_refused(
    tmp_path: Path, config: Path, state: Path
) -> None:
    blocked_store(tmp_path, state)
    code, _, err = ilmar("blocked", "release", "fake:9", "--config", str(config), "--as", "bob")
    assert code == 1
    assert "no blocked task matches" in err
    code, _, err = ilmar("blocked", "abandon", "1", "--config", str(config), "--as", "Bob Smith")
    assert code == 3
    assert "--as" in err


def test_nothing_is_written_while_a_coordinator_holds_the_lock(
    tmp_path: Path, config: Path, state: Path
) -> None:
    blocked_store(tmp_path, state)
    lock = InstanceLock.acquire(WorkspaceLayout.under(state))
    try:
        code, _, err = ilmar("blocked", "release", "1", "--config", str(config), "--as", "bob")
    finally:
        lock.release()
    assert code == 3
    assert "is a coordinator running?" in err
    assert ilmar("blocked", "list", "--config", str(config))[1].startswith("fake:1\te1")
