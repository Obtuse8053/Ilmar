# SPDX-License-Identifier: Apache-2.0
"""``ilmar cancel`` (#16 part 1, ADR-0025 §1).

The command publishes a request and nothing else: it never writes the store,
takes no lock (it works while a coordinator holds ownership), refuses unknown
and finished executions, and never overwrites a pending request.
"""

from __future__ import annotations

import io
import json
import os
from pathlib import Path

import pytest

from event_store_support import digest, dump
from ilmar import cli_cancel
from ilmar.adapters.sqlite import SQLiteExecutionStore
from ilmar.cli import _build_parser, main
from ilmar.core import cancel_inbox as fs
from ilmar.core.cancellation import Cancellations
from ilmar.core.journal import Journal
from ilmar.core.layout import WorkspaceLayout
from ilmar.core.ownership import ExecutionLeases, Ownership
from ilmar.core.state_machine import ExecutionStateMachine
from ilmar.domain.execution import ExecutionState
from ilmar.domain.ledger import Commit
from ilmar.domain.tasks import TaskRef

from helpers import at

REPO_ROOT = Path(__file__).resolve().parents[2]
MACHINE = ExecutionStateMachine(max_attempts=3)


@pytest.fixture
def state(tmp_path: Path) -> Path:
    return tmp_path.resolve() / ".ilmar"


@pytest.fixture
def config(tmp_path: Path, state: Path) -> Path:
    path = tmp_path.resolve() / "ilmar.toml"
    path.write_text((REPO_ROOT / "ilmar.example.toml").read_text())
    return path


@pytest.fixture
def store_path(state: Path) -> Path:
    """A store holding execution e1 (queued) and e9 (failed); ownership released."""
    state.mkdir()
    path = state / "executions.sqlite3"
    with SQLiteExecutionStore(path) as store:
        ownership = Ownership.take(layout=WorkspaceLayout.under(state), store=store,
                                   instance_id="default", now=at(0))  # fmt: skip
        fence = ownership.fence
        for key, execution_id in (("1", "e1"), ("9", "e9")):
            execution, transition = MACHINE.start(TaskRef("fake", key),
                                                  execution_id=execution_id, now=at(1))  # fmt: skip
            store.create(execution, transition, fence=fence)
        failed = store.get("e9")
        assert failed is not None
        updated, step = MACHINE.advance(failed, ExecutionState.FAILED, now=at(2), reason="r",
                                        error="gave up")  # fmt: skip
        store.commit(fence, Commit(applied=((updated, step),), reasons={"e9": "agent-failed"}))
        ownership.release()
    return path


def ilmar(*argv: str) -> tuple[int, str, str]:
    words = list(argv)
    try:
        parsed = _build_parser().parse_args(words)
    except SystemExit:
        return main(words), "", ""
    out, err = io.StringIO(), io.StringIO()
    code = cli_cancel.run(parsed, out=out, err=err, now=lambda: at(30))
    return code, out.getvalue(), err.getvalue()


def inbox_entries(state: Path) -> list[str]:
    path = WorkspaceLayout.under(state).coordinator_root.joinpath(*fs.INBOX_PARTS)
    return sorted(os.listdir(path)) if path.exists() else []


def test_cancel_publishes_a_request_and_never_writes_the_store(
    config: Path, state: Path, store_path: Path
) -> None:
    before = (digest(store_path), dump(store_path))
    code, out, err = ilmar("cancel", "e1", "--config", str(config), "--as", "alice")
    assert (code, err) == (0, "")
    assert "cancellation requested (q-" in out
    assert inbox_entries(state) == ["e1.json"]
    path = WorkspaceLayout.under(state).coordinator_root.joinpath(*fs.INBOX_PARTS, "e1.json")
    stored = json.loads(path.read_text())
    assert (stored["actor"], stored["execution"], stored["written_at"]) == (
        "alice", "e1", "2026-01-01T12:00:30Z")  # fmt: skip
    assert (digest(store_path), dump(store_path)) == before


def test_a_second_cancel_reports_the_pending_request_without_overwriting(
    config: Path, state: Path, store_path: Path
) -> None:
    ilmar("cancel", "e1", "--config", str(config), "--as", "alice")
    path = WorkspaceLayout.under(state).coordinator_root.joinpath(*fs.INBOX_PARTS, "e1.json")
    first = path.read_bytes()
    code, out, _ = ilmar("cancel", "e1", "--config", str(config), "--as", "bob")
    assert code == 0
    assert "already pending" in out
    assert "by alice" in out
    assert path.read_bytes() == first


def test_cancel_works_while_a_coordinator_holds_ownership(
    config: Path, state: Path, store_path: Path
) -> None:
    with SQLiteExecutionStore(store_path) as store:
        ownership = Ownership.take(layout=WorkspaceLayout.under(state), store=store,
                                   instance_id="default", now=at(40))  # fmt: skip
        try:
            code, _, err = ilmar("cancel", "e1", "--config", str(config), "--as", "alice")
            assert (code, err) == (0, "")
            # The coordinator applies it; the command reports the accepted request.
            journal = Journal(store=store, ownership=ownership, clock=lambda: at(41))
            lease = ExecutionLeases().acquire("e1")
            execution = store.get("e1")
            assert execution is not None
            layout = WorkspaceLayout.under(state)
            cancellations = Cancellations(store=store, journal=journal, layout=layout)
            applied = cancellations.apply(execution, lease)
            assert applied.decision == "accepted"
        finally:
            ownership.release()
    code, out, _ = ilmar("cancel", "e1", "--config", str(config), "--as", "bob")
    assert code == 0
    assert "already accepted" in out
    assert inbox_entries(state) == []  # nothing new was published


@pytest.mark.parametrize(
    ("execution", "message"), [("e9", "already finished"), ("nobody", "no execution")]
)
def test_an_unknown_or_finished_execution_is_refused(
    config: Path, state: Path, store_path: Path, execution: str, message: str
) -> None:
    code, _, err = ilmar("cancel", execution, "--config", str(config), "--as", "alice")
    assert code == 3
    assert message in err
    assert inbox_entries(state) == []


@pytest.mark.parametrize(
    "argv",
    [
        ("cancel", "e1", "--as", "alice"),  # no --config
        ("cancel", "e1", "--config", "x"),  # no --as
        ("cancel",),
    ],
)
def test_usage_errors_are_exit_3(argv: tuple[str, ...]) -> None:
    with pytest.raises(SystemExit):
        _build_parser().parse_args(list(argv))
    assert main(list(argv)) == 3


def test_an_invalid_actor_is_refused_and_nothing_is_published(
    config: Path, state: Path, store_path: Path
) -> None:
    code, _, err = ilmar("cancel", "e1", "--config", str(config), "--as", "Alice <a@b.c>")
    assert code == 3
    assert "--as" in err
    assert [n for n in inbox_entries(state) if not n.startswith(".tmp-")] == []


def test_an_invalid_execution_id_is_refused(config: Path, store_path: Path) -> None:
    code, _, err = ilmar("cancel", "../e1", "--config", str(config), "--as", "alice")
    assert code == 3
    assert "error:" in err


def test_an_unsafe_inbox_is_exit_3(config: Path, state: Path, store_path: Path) -> None:
    requests = WorkspaceLayout.under(state).coordinator_root / "requests"
    requests.mkdir(mode=0o700)
    requests.chmod(0o755)
    code, _, err = ilmar("cancel", "e1", "--config", str(config), "--as", "alice")
    assert code == 3
    assert "open to other users" in err


def test_an_invalid_entry_is_reported_and_left_alone(
    config: Path, state: Path, store_path: Path
) -> None:
    box = WorkspaceLayout.under(state).coordinator_root.joinpath(*fs.INBOX_PARTS)
    box.mkdir(parents=True, mode=0o700)
    (box.parent).chmod(0o700)
    (box / "e1.json").symlink_to(state / "executions.sqlite3")
    code, _, err = ilmar("cancel", "e1", "--config", str(config), "--as", "alice")
    assert code == 3
    assert "symlink" in err
    assert (box / "e1.json").is_symlink()


def test_a_missing_store_is_exit_3_and_creates_nothing(config: Path, state: Path) -> None:
    code, _, err = ilmar("cancel", "e1", "--config", str(config), "--as", "alice")
    assert code == 3
    assert "error:" in err
    assert not state.exists()


def test_a_store_kind_without_an_event_log_is_exit_3(config: Path) -> None:
    config.write_text(config.read_text().replace('kind = "sqlite"', 'kind = "memory"'))
    code, _, err = ilmar("cancel", "e1", "--config", str(config), "--as", "alice")
    assert code == 3
    assert "error:" in err
