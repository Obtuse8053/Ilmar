# SPDX-License-Identifier: Apache-2.0
"""``ilmar cancel`` (#16 part 1, ADR-0025 §1).

The command publishes a request and nothing else: it never writes the store,
takes no lock (it works while a coordinator holds ownership), refuses unknown
and finished executions, and never overwrites a pending request.
"""

from __future__ import annotations

import io
import json
import multiprocessing
import os
from pathlib import Path
from typing import Any

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


# -- ADR-0025 acceptance: first request wins, through the CLI ------------------------------

_FORK = multiprocessing.get_context("fork")


def _cli_in_child(config: str, actor: str, start: Any, results: Any) -> None:
    parsed = _build_parser().parse_args(["cancel", "e1", "--config", config, "--as", actor])
    out, err = io.StringIO(), io.StringIO()
    start.wait()
    code = cli_cancel.run(parsed, out=out, err=err, now=lambda: at(30))
    results.put((actor, code, out.getvalue(), err.getvalue()))


def apply_once(state: Path, store_path: Path) -> list[Any]:
    """The worker's side: the lease holder applies the inbox; the recorded events."""
    with SQLiteExecutionStore(store_path) as store:
        layout = WorkspaceLayout.under(state)
        ownership = Ownership.take(layout=layout, store=store, instance_id="default", now=at(60))
        try:
            journal = Journal(store=store, ownership=ownership, clock=lambda: at(61))
            execution = store.get("e1")
            assert execution is not None
            lease = ExecutionLeases().acquire("e1")
            Cancellations(store=store, journal=journal, layout=layout).apply(execution, lease)
            return [e for e in store.events(execution_id="e1")
                    if e.type.value == "execution.cancel_requested"]  # fmt: skip
        finally:
            ownership.release()


@pytest.mark.parametrize("round_", range(5))
def test_sixteen_concurrent_cancels_publish_exactly_one_request(
    config: Path, state: Path, store_path: Path, round_: int
) -> None:
    count = 16
    start = _FORK.Barrier(count)
    results = _FORK.Queue()
    actors = [f"user{n}" for n in range(count)]
    children = [_FORK.Process(target=_cli_in_child, args=(str(config), a, start, results))
                for a in actors]  # fmt: skip
    for child in children:
        child.start()
    for child in children:
        child.join(60)
        assert child.exitcode == 0
    outcomes = [results.get(timeout=5) for _ in actors]
    assert {code for _, code, _, _ in outcomes} == {0}  # every process exits 0
    assert all(err == "" for _, _, _, err in outcomes)
    box = WorkspaceLayout.under(state).coordinator_root.joinpath(*fs.INBOX_PARTS)
    assert sorted(os.listdir(box)) == ["e1.json"]  # one final file, no temporary left
    stored = json.loads((box / "e1.json").read_bytes())
    winner_id, winner_actor = stored["request"], stored["actor"]
    winners = [a for a, _, out, _ in outcomes if "cancellation requested" in out]
    assert winners == [winner_actor]  # byte for byte that submitter's request
    for actor, _, out, _ in outcomes:  # everyone reports that same request and actor
        assert winner_id in out
        if actor != winner_actor:
            assert f"by {winner_actor}" in out
    (event,) = apply_once(state, store_path)  # the worker records exactly one
    assert (event.data["request"], event.actor.ref) == (winner_id, winner_actor)
    assert os.listdir(box) == []


def _valid_entry(box: Path, tmp: Path) -> Path:
    path = box / "e1.json"
    path.write_text(fs.encode({"schema": fs.SCHEMA, "request": "q-" + "a" * 32,
                               "execution": "e1", "actor": "alice",
                               "written_at": "2026-11-09T10:00:00Z"}))  # fmt: skip
    path.chmod(0o600)
    return path


def _invalid_entry(box: Path, tmp: Path) -> Path:
    path = box / "e1.json"
    path.write_text('{"force":true}')
    path.chmod(0o600)
    return path


def _symlink_entry(box: Path, tmp: Path) -> Path:
    target = tmp / "valid-elsewhere.json"
    _valid_entry(tmp, tmp).rename(target)
    (box / "e1.json").symlink_to(target)
    return box / "e1.json"


def _directory_entry(box: Path, tmp: Path) -> Path:
    (box / "e1.json").mkdir(mode=0o700)
    return box / "e1.json"


def _fifo_entry(box: Path, tmp: Path) -> Path:
    os.mkfifo(box / "e1.json", 0o600)
    return box / "e1.json"


STANDING = {
    "valid": (_valid_entry, 0),
    "invalid": (_invalid_entry, 3),
    "symlink-to-valid": (_symlink_entry, 3),
    "directory": (_directory_entry, 3),
    "fifo": (_fifo_entry, 3),
}


def fingerprint(path: Path) -> tuple[Any, ...]:
    info = path.lstat()
    if path.is_symlink():
        content: Any = os.readlink(path)
    elif path.is_file():
        content = path.read_bytes()
    else:
        content = None
    return (info.st_ino, info.st_dev, info.st_mode, info.st_nlink, content)


@pytest.mark.parametrize("case", sorted(STANDING))
def test_a_submission_never_touches_a_standing_entry(
    config: Path, state: Path, store_path: Path, tmp_path: Path, case: str
) -> None:
    box = WorkspaceLayout.under(state).coordinator_root.joinpath(*fs.INBOX_PARTS)
    os.close(fs.open_inbox(WorkspaceLayout.under(state).coordinator_root, create=True))
    make, expected = STANDING[case]
    entry = make(box, tmp_path)
    before = fingerprint(entry)
    target_before = (tmp_path / "valid-elsewhere.json").read_bytes() if case.startswith(
        "symlink") else None  # fmt: skip
    code, _, _ = ilmar("cancel", "e1", "--config", str(config), "--as", "bob")
    assert code == expected
    assert fingerprint(entry) == before  # byte for byte, inode for inode
    assert sorted(os.listdir(box)) == ["e1.json"]  # no temporary left behind
    if target_before is not None:
        assert (tmp_path / "valid-elsewhere.json").read_bytes() == target_before


def _requests_symlink(root: Path, tmp: Path) -> None:
    elsewhere = tmp / "elsewhere"
    elsewhere.mkdir(mode=0o700)
    (root / "requests").symlink_to(elsewhere)


def _cancel_symlink(root: Path, tmp: Path) -> None:
    (root / "requests").mkdir(mode=0o700)
    elsewhere = tmp / "elsewhere"
    elsewhere.mkdir(mode=0o700)
    (root / "requests" / "cancel").symlink_to(elsewhere)


def _requests_group_open(root: Path, tmp: Path) -> None:
    (root / "requests").mkdir()
    (root / "requests").chmod(0o770)


def _cancel_world_open(root: Path, tmp: Path) -> None:
    (root / "requests" / "cancel").mkdir(parents=True)
    (root / "requests").chmod(0o700)
    (root / "requests" / "cancel").chmod(0o707)


UNSAFE_INBOXES = {
    "requests-symlink": _requests_symlink,
    "cancel-symlink": _cancel_symlink,
    "requests-group-open": _requests_group_open,
    "cancel-open-to-others": _cancel_world_open,
}


def tree(path: Path) -> list[tuple[str, int]]:
    return sorted((str(p.relative_to(path)), p.lstat().st_mode) for p in path.rglob("*"))


@pytest.mark.parametrize("case", sorted(UNSAFE_INBOXES))
def test_an_unsafe_inbox_is_refused_and_nothing_is_written(
    config: Path, state: Path, store_path: Path, tmp_path: Path, case: str
) -> None:
    root = WorkspaceLayout.under(state).coordinator_root
    outside = tmp_path / "outside"
    outside.mkdir()
    UNSAFE_INBOXES[case](root, outside)
    before = (tree(root), tree(outside))
    code, _, err = ilmar("cancel", "e1", "--config", str(config), "--as", "alice")
    assert code == 3
    assert "error:" in err
    assert (tree(root), tree(outside)) == before


def test_an_inbox_owned_by_another_user_is_refused_and_nothing_is_written(
    config: Path, state: Path, store_path: Path
) -> None:
    root = WorkspaceLayout.under(state).coordinator_root
    before = tree(root)
    parsed = _build_parser().parse_args(["cancel", "e1", "--config", str(config), "--as", "a"])
    out, err = io.StringIO(), io.StringIO()
    code = cli_cancel.run(parsed, out=out, err=err, now=lambda: at(30), uid=os.geteuid() + 1)
    assert code == 3
    assert "belongs to another user" in err.getvalue()
    assert tree(root) == before
