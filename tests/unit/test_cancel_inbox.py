# SPDX-License-Identifier: Apache-2.0
"""The cancellation request inbox (#16 part 1, ADR-0025 §1 and §1a).

* the one validation routine refuses every malicious or malformed entry, never
  blocks, never writes and never follows a symlink;
* first request wins: concurrent submitters publish exactly one request and
  never overwrite one, also when a submitter crashes at any step;
* an unsafe inbox, or a filesystem without hard links, is refused.
"""

from __future__ import annotations

import errno
import json
import multiprocessing
import os
import stat
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

from ilmar.core import cancel_inbox as fs
from ilmar.core.cancel_inbox import InvalidEntryError, UnsafeInboxError

from helpers import at

EXECUTION = "e1"
FINAL = fs.final_name(EXECUTION)
REQUEST_ID = "q-" + "a" * 32

_FORK = multiprocessing.get_context("fork")


@pytest.fixture
def root(tmp_path: Path) -> Path:
    path = tmp_path.resolve() / "coordinator"
    path.mkdir(mode=0o700)
    return path


@pytest.fixture
def inbox(root: Path) -> Iterator[int]:
    fd = fs.open_inbox(root, create=True)
    yield fd
    os.close(fd)


def box(root: Path) -> Path:
    return root.joinpath(*fs.INBOX_PARTS)


def fields(**changes: str) -> dict[str, str]:
    base = {
        "schema": fs.SCHEMA,
        "request": REQUEST_ID,
        "execution": EXECUTION,
        "actor": "alice",
        "written_at": "2026-11-09T10:00:00Z",
    }
    base.update(changes)
    return base


def place(root: Path, content: bytes | str, *, name: str = FINAL, mode: int = 0o600) -> Path:
    path = box(root) / name
    path.write_bytes(content.encode() if isinstance(content, str) else content)
    path.chmod(mode)
    return path


def snapshot(root: Path) -> dict[str, tuple[int, bytes | None]]:
    """Every name under the inbox with its mode and (regular files) content."""
    seen: dict[str, tuple[int, bytes | None]] = {}
    for path in sorted(box(root).rglob("*")):
        info = path.lstat()
        content = path.read_bytes() if stat.S_ISREG(info.st_mode) else None
        seen[str(path.relative_to(box(root)))] = (info.st_mode, content)
    return seen


# -- the validation routine ---------------------------------------------------------------


def test_a_valid_request_reads_back_with_its_identity(root: Path, inbox: int) -> None:
    raw = fs.encode(fields())
    path = place(root, raw)
    reading = fs.read_cancel_request(inbox, EXECUTION)
    assert reading.problem is None
    request = reading.request
    assert request is not None
    assert (request.request_id, request.actor, request.written_at) == (
        REQUEST_ID, "alice", "2026-11-09T10:00:00Z")  # fmt: skip
    info = path.stat()
    assert (request.device, request.inode) == (info.st_dev, info.st_ino)
    assert fs.matches(fs.identify(inbox, FINAL), request)


def test_no_entry_is_empty(inbox: int) -> None:
    assert fs.read_cancel_request(inbox, EXECUTION).empty


MALFORMED: dict[str, bytes] = {
    "empty": b"",
    "not-json": b"cancel please",
    "not-utf8": b"\xff\xfe{}",
    "array": b"[]",
    "spaces": json.dumps(fields(), sort_keys=True).encode(),  # valid JSON, not canonical
    "unsorted": json.dumps(fields(), separators=(",", ":")).encode(),
    "newline": (fs.encode(fields()) + "\n").encode(),
    "duplicate-key": fs.encode(fields())[:-1].encode() + b',"actor":"mallory"}',
    "extra-key": fs.encode({**fields(), "force": "1"}).encode(),
    "missing-key": fs.encode({k: v for k, v in fields().items() if k != "actor"}).encode(),
    "nan": fs.encode(fields())[:-1].encode() + b',"x":NaN}',
    "number": fs.encode(fields())[:-1].encode() + b',"x":1}',
    "wrong-schema": fs.encode(fields(schema="ilmar.cancel-request/2")).encode(),
    "wrong-execution": fs.encode(fields(execution="e2")).encode(),
    "bad-request-id": fs.encode(fields(request="q-XYZ")).encode(),
    "bad-actor": fs.encode(fields(actor="Alice <alice@example.com>")).encode(),
    "token-as-actor": fs.encode(fields(actor="ghp_" + "x" * 36)).encode(),
    "bad-time": fs.encode(fields(written_at="yesterday")).encode(),
    "non-ascii-escape": fs.encode(fields(actor="alïce")).encode(),
    "too-large": fs.encode(fields(actor="a" * 2000)).encode(),
}


@pytest.mark.parametrize("case", sorted(MALFORMED))
def test_a_malformed_request_is_a_problem_and_left_alone(root: Path, inbox: int, case: str) -> None:
    place(root, MALFORMED[case])
    before = snapshot(root)
    reading = fs.read_cancel_request(inbox, EXECUTION)
    assert reading.request is None
    assert reading.problem
    assert snapshot(root) == before


def _symlink(root: Path) -> None:
    target = root.parent / "elsewhere.json"
    target.write_text(fs.encode(fields()))
    target.chmod(0o600)
    (box(root) / FINAL).symlink_to(target)


def _dangling(root: Path) -> None:
    (box(root) / FINAL).symlink_to(root.parent / "nothing")


def _fifo(root: Path) -> None:
    os.mkfifo(box(root) / FINAL, 0o600)  # an open without O_NONBLOCK would hang here


def _directory(root: Path) -> None:
    (box(root) / FINAL).mkdir(mode=0o700)


def _socket(root: Path) -> None:
    import socket

    with socket.socket(socket.AF_UNIX) as server:
        server.bind(str(box(root) / FINAL))


def _group_readable(root: Path) -> None:
    place(root, fs.encode(fields()), mode=0o640)


def _world_writable(root: Path) -> None:
    place(root, fs.encode(fields()), mode=0o602)


def _hard_link_elsewhere(root: Path) -> None:
    path = place(root, fs.encode(fields()))
    os.link(path, root / "kept-by-someone-else")


def _hard_link_to_wrong_temporary(root: Path) -> None:
    path = place(root, fs.encode(fields()))
    os.link(path, box(root) / fs.temporary_name(EXECUTION, "q-" + "b" * 32))


def _three_links(root: Path) -> None:
    path = place(root, fs.encode(fields()))
    os.link(path, box(root) / fs.temporary_name(EXECUTION, REQUEST_ID))
    os.link(path, root / "third")


def _temporary_is_a_different_file(root: Path) -> None:
    path = place(root, fs.encode(fields()))
    os.link(path, root / "other-link")  # nlink 2, and the temporary name is another inode
    place(root, fs.encode(fields()), name=fs.temporary_name(EXECUTION, REQUEST_ID))


HOSTILE: dict[str, Callable[[Path], None]] = {
    "symlink": _symlink,
    "dangling-symlink": _dangling,
    "fifo": _fifo,
    "directory": _directory,
    "socket": _socket,
    "group-readable": _group_readable,
    "world-writable": _world_writable,
    "hard-link-elsewhere": _hard_link_elsewhere,
    "hard-link-to-a-wrong-temporary": _hard_link_to_wrong_temporary,
    "three-links": _three_links,
    "temporary-is-a-different-file": _temporary_is_a_different_file,
}


@pytest.mark.parametrize("case", sorted(HOSTILE))
def test_a_hostile_entry_is_a_problem_and_left_alone(root: Path, inbox: int, case: str) -> None:
    HOSTILE[case](root)
    before = snapshot(root)
    reading = fs.read_cancel_request(inbox, EXECUTION)
    assert reading.request is None
    assert reading.problem
    assert snapshot(root) == before


def test_an_entry_owned_by_another_user_is_a_problem(root: Path, inbox: int) -> None:
    place(root, fs.encode(fields()))
    reading = fs.read_cancel_request(inbox, EXECUTION, uid=os.geteuid() + 1)
    assert reading.problem == "owned by another user"


def test_the_crash_leftover_of_a_submitter_is_valid(root: Path, inbox: int) -> None:
    """Final and temporary name are one inode (linkat done, unlink not yet)."""
    path = place(root, fs.encode(fields()), name=fs.temporary_name(EXECUTION, REQUEST_ID))
    os.link(path, box(root) / FINAL)
    reading = fs.read_cancel_request(inbox, EXECUTION)
    assert reading.request is not None
    assert reading.request.request_id == REQUEST_ID


def test_a_temporary_name_unlinked_during_the_read_is_still_valid(
    root: Path, inbox: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression (CI on the #74 merge): a loser read the winner's request with two
    links, and the winner unlinked its temporary name before the companion check."""
    temporary = place(root, fs.encode(fields()), name=fs.temporary_name(EXECUTION, REQUEST_ID))
    os.link(temporary, box(root) / FINAL)
    real = os.stat

    def winner_unlinks_first(path: object, *args: object, **kwargs: object) -> os.stat_result:
        if path == temporary.name:
            os.unlink(temporary)  # the winning submitter's last step, at the worst moment
        return real(path, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(os, "stat", winner_unlinks_first)
    reading = fs.read_cancel_request(inbox, EXECUTION)
    assert reading.problem is None
    assert reading.request is not None
    assert reading.request.request_id == REQUEST_ID


def test_a_second_link_that_stays_is_refused_even_if_the_temporary_name_is_gone(
    root: Path, inbox: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    temporary = place(root, fs.encode(fields()), name=fs.temporary_name(EXECUTION, REQUEST_ID))
    os.link(temporary, box(root) / FINAL)
    os.link(temporary, root / "kept-by-someone-else")
    os.unlink(temporary)
    assert fs.read_cancel_request(inbox, EXECUTION).problem == (
        "a hard link to the request from elsewhere")  # fmt: skip


def test_a_file_that_grows_while_read_is_refused(
    root: Path, inbox: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    place(root, fs.encode(fields()))
    real = os.read

    def growing(fd: int, size: int) -> bytes:
        return real(fd, size) + b" " * fs.MAX_BYTES  # fstat said small; the read is not

    monkeypatch.setattr(os, "read", growing)
    assert fs.read_cancel_request(inbox, EXECUTION).problem == "the request is larger than 1 KiB"


@pytest.mark.parametrize("execution_id", ["", "../e1", "e1/x", ".e1", "a" * 65, "e 1"])
def test_an_unsafe_execution_id_is_refused(inbox: int, execution_id: str) -> None:
    with pytest.raises(ValueError, match="execution id"):
        fs.read_cancel_request(inbox, execution_id)
    with pytest.raises(ValueError, match="execution id"):
        fs.submit(inbox, execution_id, "alice", now=at(0))


# -- the inbox directory ------------------------------------------------------------------


def test_the_inbox_is_created_private(root: Path, inbox: int) -> None:
    for path in (root / "requests", box(root)):
        assert path.lstat().st_mode & 0o777 == 0o700


def test_a_missing_inbox_is_not_created_by_readers(root: Path) -> None:
    with pytest.raises(FileNotFoundError):
        fs.open_inbox(root, create=False)
    assert list(root.iterdir()) == []


def _requests_symlink(root: Path) -> None:
    elsewhere = root.parent / "elsewhere"
    elsewhere.mkdir(mode=0o700)
    (root / "requests").symlink_to(elsewhere)


def _cancel_symlink(root: Path) -> None:
    (root / "requests").mkdir(mode=0o700)
    elsewhere = root.parent / "elsewhere"
    elsewhere.mkdir(mode=0o700)
    (root / "requests" / "cancel").symlink_to(elsewhere)


def _requests_open(root: Path) -> None:
    (root / "requests").mkdir()
    (root / "requests").chmod(0o755)


def _cancel_group_writable(root: Path) -> None:
    (root / "requests" / "cancel").mkdir(parents=True)
    (root / "requests").chmod(0o700)
    (root / "requests" / "cancel").chmod(0o770)


def _requests_a_file(root: Path) -> None:
    (root / "requests").write_text("")


def _root_world_writable(root: Path) -> None:
    root.chmod(0o777)


UNSAFE: dict[str, Callable[[Path], None]] = {
    "requests-symlink": _requests_symlink,
    "cancel-symlink": _cancel_symlink,
    "requests-readable-by-others": _requests_open,
    "cancel-group-writable": _cancel_group_writable,
    "requests-a-file": _requests_a_file,
    "root-world-writable": _root_world_writable,
}


@pytest.mark.parametrize("case", sorted(UNSAFE))
@pytest.mark.parametrize("create", [True, False])
def test_an_unsafe_inbox_is_refused(root: Path, case: str, create: bool) -> None:
    UNSAFE[case](root)
    with pytest.raises(UnsafeInboxError):
        os.close(fs.open_inbox(root, create=create))


def test_a_root_that_is_a_symlink_is_refused(root: Path) -> None:
    link = root.parent / "link"
    link.symlink_to(root)
    with pytest.raises(UnsafeInboxError, match="canonical"):
        fs.open_inbox(link, create=True)
    with pytest.raises(UnsafeInboxError, match="canonical"):
        fs.open_inbox(Path("relative"), create=True)


def test_an_inbox_owned_by_another_user_is_refused(root: Path) -> None:
    with pytest.raises(UnsafeInboxError):
        fs.open_inbox(root, create=True, uid=os.geteuid() + 1)


# -- submitting: first request wins -------------------------------------------------------


def test_submit_publishes_one_private_canonical_request(root: Path, inbox: int) -> None:
    submission = fs.submit(inbox, EXECUTION, "alice", now=at(0))
    assert submission.created
    path = box(root) / FINAL
    info = path.lstat()
    assert stat.S_ISREG(info.st_mode)
    assert (info.st_mode & 0o777, info.st_nlink) == (0o600, 1)
    assert sorted(os.listdir(box(root))) == [FINAL]  # the temporary name is gone
    raw = path.read_bytes()
    assert raw.decode() == fs.encode(json.loads(raw))
    assert fs.read_cancel_request(inbox, EXECUTION).request == submission.request


def test_submit_never_overwrites_a_standing_request(root: Path, inbox: int) -> None:
    first = fs.submit(inbox, EXECUTION, "alice", now=at(0))
    before = snapshot(root)
    second = fs.submit(inbox, EXECUTION, "bob", now=at(5))
    assert not second.created
    assert second.request == first.request
    assert snapshot(root) == before


def test_submit_leaves_an_invalid_entry_alone(root: Path, inbox: int) -> None:
    _symlink(root)
    before = snapshot(root)
    with pytest.raises(InvalidEntryError, match="symlink"):
        fs.submit(inbox, EXECUTION, "alice", now=at(0))
    assert snapshot(root) == before


def test_submit_refuses_a_bad_actor_before_writing(root: Path, inbox: int) -> None:
    with pytest.raises(ValueError, match="--as"):
        fs.submit(inbox, EXECUTION, "Alice Example", now=at(0))
    assert os.listdir(box(root)) == []


@pytest.mark.parametrize("code", [errno.EPERM, errno.EOPNOTSUPP, errno.EXDEV, errno.EMLINK])
def test_a_filesystem_without_hard_links_is_unsafe(
    root: Path, inbox: int, monkeypatch: pytest.MonkeyPatch, code: int
) -> None:
    def no_link(*args: object, **kwargs: object) -> None:
        raise OSError(code, os.strerror(code))

    monkeypatch.setattr(os, "link", no_link)
    with pytest.raises(UnsafeInboxError, match="hard links"):
        fs.submit(inbox, EXECUTION, "alice", now=at(0))
    assert os.listdir(box(root)) == []  # never a rename fallback, no temporary left


def test_a_request_taken_while_submitting_is_retried_not_misreported(
    root: Path, inbox: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A standing request vanishes between EEXIST and the read (the coordinator took
    it): the submitter tries again instead of reporting an invalid entry."""
    fs.submit(inbox, EXECUTION, "alice", now=at(0))
    real = fs.read_cancel_request
    calls: list[int] = []

    def taken_first(fd: int, execution_id: str, *, uid: int | None = None) -> fs.Reading:
        calls.append(1)
        if len(calls) == 1:
            os.unlink(FINAL, dir_fd=fd)
            return fs.Reading()
        return real(fd, execution_id, uid=uid)

    monkeypatch.setattr(fs, "read_cancel_request", taken_first)
    submission = fs.submit(inbox, EXECUTION, "bob", now=at(1))
    assert submission.created
    assert submission.request.actor == "bob"


def _submit_in_child(root: str, actor: str, start: object, results: object) -> None:
    fd = fs.open_inbox(Path(root), create=True)
    start.wait()  # type: ignore[attr-defined]
    submission = fs.submit(fd, EXECUTION, actor, now=at(0))
    results.put((actor, submission.created, submission.request.request_id))  # type: ignore[attr-defined]


@pytest.mark.parametrize("round_", range(5))
def test_concurrent_submitters_publish_exactly_one_request(root: Path, round_: int) -> None:
    count = 16
    start = _FORK.Barrier(count)
    results = _FORK.Queue()
    actors = [f"user{n}" for n in range(count)]
    children = [_FORK.Process(target=_submit_in_child, args=(str(root), a, start, results))
                for a in actors]  # fmt: skip
    for child in children:
        child.start()
    for child in children:
        child.join(30)
        assert child.exitcode == 0
    outcomes = [results.get(timeout=5) for _ in actors]
    winners = [o for o in outcomes if o[1]]
    assert len(winners) == 1
    winner_actor, _, winner_id = winners[0]
    assert {o[2] for o in outcomes} == {winner_id}  # every loser saw the winner's request
    assert sorted(os.listdir(box(root))) == [FINAL]  # no temporary name left behind
    stored = json.loads((box(root) / FINAL).read_bytes())
    assert (stored["request"], stored["actor"]) == (winner_id, winner_actor)


STEPS = ["created", "written", "synced", "linked", "unlinked"]


def _crash_at(root: str, step: str) -> None:
    def crash(name: str) -> None:
        if name == step:
            os._exit(9)  # the process dies here: no cleanup, no exception handler

    fs._step = crash
    fd = fs.open_inbox(Path(root), create=True)
    fs.submit(fd, EXECUTION, "alice", now=at(0))
    os._exit(0)


@pytest.mark.parametrize("step", STEPS)
def test_a_submitter_crash_at_any_step_never_leaves_a_half_request(
    root: Path, inbox: int, step: str
) -> None:
    child = _FORK.Process(target=_crash_at, args=(str(root), step))
    child.start()
    child.join(30)
    assert child.exitcode == 9
    reading = fs.read_cancel_request(inbox, EXECUTION)
    assert reading.problem is None  # never a torn or partial request at the final name
    published = STEPS.index(step) >= STEPS.index("linked")
    assert (reading.request is not None) == published
    _, temporary = fs.leftovers(inbox, EXECUTION)
    assert bool(temporary) == (step != "unlinked")  # an inert temporary file, at most
    retry = fs.submit(inbox, EXECUTION, "bob", now=at(1))
    assert retry.created == (not published)
    if published:
        assert reading.request is not None
        assert retry.request.request_id == reading.request.request_id
