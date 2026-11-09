# SPDX-License-Identifier: Apache-2.0
"""The cancellation request inbox (ADR-0025 §1a and §1b).

``ilmar cancel`` *submits* a request as one file in a private inbox,
``<coordinator_root>/requests/cancel/``. The coordinator's worker *applies* it
(:mod:`ilmar.core.cancellation`). This module holds every filesystem
operation on the inbox, and nothing else touches it.

* **Opening** walks from ``coordinator_root`` with ``O_DIRECTORY |
  O_NOFOLLOW`` at every level and refuses any level that is a symlink, not a
  directory, owned by another user, or open to group or others
  (:func:`open_inbox`).
* **Reading** (:func:`read_cancel_request`) is the one validation routine,
  shared by the watcher and the worker. It never writes. Anything that fails
  a check is "no request", with a reason; it never raises for a bad entry.
* **Submitting** (:func:`submit`) writes an ``O_EXCL`` temporary file and
  publishes it with ``linkat``, which fails if anything is at the final name:
  the first request wins and nothing is ever overwritten.
* **Cleanup** (:func:`take`, :func:`identify`, :func:`set_aside`) is rename
  first, verify after: POSIX has no atomic compare-and-delete, so the worker
  moves the shared name to a private name before checking what it holds, and
  deletes only what it verified there.

Trust model (ADR-0025 §1a): the inbox belongs to the account running Ilmar and
is never mountable into a sandbox. ``ilmar cancel`` processes only create
their own temporary names and link them; one worker per instance, holding the
instance lock, is the only component that renames or removes final names.
"""

from __future__ import annotations

import contextlib
import errno
import hashlib
import json
import os
import re
import secrets
import stat
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ilmar.domain.errors import IlmarError
from ilmar.domain.events import Actor, ActorKind

SCHEMA = "ilmar.cancel-request/1"
MAX_BYTES = 1024
INBOX_PARTS = ("requests", "cancel")
REJECTED = "rejected"
_KEYS = frozenset({"schema", "request", "execution", "actor", "written_at"})
_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
_REQUEST_ID = re.compile(r"^q-[0-9a-f]{32}$")
_TOKEN = re.compile(r"^[0-9a-f]{32}$")
_WRITTEN_AT = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
_READ_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC
_CREATE_FLAGS = os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW | os.O_CLOEXEC


class UnsafeInboxError(IlmarError):
    """The inbox cannot be used safely: a symlink, a foreign owner, open
    permissions, or a filesystem without hard links. Nothing was written."""


class InvalidEntryError(IlmarError):
    """Something other than a valid request occupies an execution's request name.
    It is left exactly as it is; the coordinator moves it aside."""


@dataclass(frozen=True, slots=True)
class CancelRequest:
    """A request that passed every check, with the identity it was read from."""

    request_id: str
    execution_id: str
    actor: str
    written_at: str
    digest: str
    device: int
    inode: int


@dataclass(frozen=True, slots=True)
class Reading:
    """What :func:`read_cancel_request` found: a request, nothing, or a problem."""

    request: CancelRequest | None = None
    problem: str | None = None

    @property
    def empty(self) -> bool:
        return self.request is None and self.problem is None


@dataclass(frozen=True, slots=True)
class Identity:
    """What a name held when :func:`identify` looked: never followed."""

    device: int
    inode: int
    digest: str | None  # a regular file's content digest; None for anything else


def final_name(execution_id: str) -> str:
    _check_execution_id(execution_id)
    return f"{execution_id}.json"


def temporary_name(execution_id: str, request_id: str) -> str:
    _check_execution_id(execution_id)
    if not _REQUEST_ID.match(request_id):
        raise ValueError("not a request id")
    return f".tmp-{execution_id}.{request_id}"


def applying_name(execution_id: str, token: str) -> str:
    _check_execution_id(execution_id)
    if not _TOKEN.match(token):
        raise ValueError("not a token")
    return f".applying-{execution_id}.{token}"


def new_request_id() -> str:
    return "q-" + secrets.token_hex(16)


def new_token() -> str:
    return secrets.token_hex(16)


# -- opening ------------------------------------------------------------------------------


def open_inbox(coordinator_root: Path, *, create: bool, uid: int | None = None) -> int:
    """A descriptor of ``coordinator_root/requests/cancel``, reached without following
    any symlink. ``create`` makes missing levels with mode 0700. Raises
    :class:`UnsafeInboxError` for any unsafe level, or ``FileNotFoundError`` when
    the inbox is missing and not to be created."""
    owner = os.geteuid() if uid is None else uid
    if not coordinator_root.is_absolute() or os.path.realpath(coordinator_root) != str(
        coordinator_root
    ):
        raise UnsafeInboxError(f"{coordinator_root} is not a canonical path")
    fd = _open_dir(str(coordinator_root), None, str(coordinator_root))
    try:
        _check_dir(fd, str(coordinator_root), owner, private=False)
        for name in INBOX_PARTS:
            child = _child_dir(fd, name, create=create, owner=owner)
            os.close(fd)
            fd = child
    except BaseException:
        os.close(fd)
        raise
    return fd


def _child_dir(parent: int, name: str, *, create: bool, owner: int) -> int:
    if create:
        with contextlib.suppress(FileExistsError):
            os.mkdir(name, 0o700, dir_fd=parent)
    fd = _open_dir(name, parent, name)
    try:
        _check_dir(fd, name, owner, private=True)
    except BaseException:
        os.close(fd)
        raise
    return fd


def _open_dir(name: str, parent: int | None, shown: str) -> int:
    try:
        return os.open(name, _DIR_FLAGS, dir_fd=parent)
    except OSError as error:
        if error.errno in (errno.ELOOP, errno.ENOTDIR):
            raise UnsafeInboxError(f"{shown} is a symlink or not a directory") from None
        raise


def _check_dir(fd: int, shown: str, owner: int, *, private: bool) -> None:
    info = os.fstat(fd)
    if info.st_uid != owner:
        raise UnsafeInboxError(f"{shown} belongs to another user")
    # The inbox levels must be closed to everyone else; above them, nobody else
    # may be able to replace them.
    forbidden = 0o077 if private else 0o022
    if info.st_mode & forbidden:
        raise UnsafeInboxError(f"{shown} is open to other users (mode {info.st_mode & 0o777:o})")


# -- validation (ADR-0025 §1b): the only reader --------------------------------------------


def read_cancel_request(inbox: int, execution_id: str, *, uid: int | None = None) -> Reading:
    """Whether a valid request exists **for** ``execution_id``. Never writes, never
    raises for a bad entry: anything that fails a check is "no request" with a
    problem."""
    return read_entry(inbox, final_name(execution_id), execution_id, inbox=inbox, uid=uid)


def read_entry(
    directory: int, name: str, execution_id: str, *, inbox: int, uid: int | None = None
) -> Reading:
    """Validate the entry ``name`` in ``directory`` (the inbox, or ``rejected/``) as a
    request for ``execution_id``. A second hard link is allowed only when it is the
    request's own temporary name in ``inbox``."""
    owner = os.geteuid() if uid is None else uid
    try:
        fd = os.open(name, _READ_FLAGS, dir_fd=directory)
    except FileNotFoundError:
        return Reading()
    except OSError as error:
        if error.errno == errno.ELOOP:
            return Reading(problem="a symlink at the request name")
        return Reading(problem=f"cannot open the request ({_code(error)})")
    try:
        info = os.fstat(fd)
        problem = _check_file(info, owner)
        if problem is not None:
            return Reading(problem=problem)
        raw = _read_bounded(fd)
    except OSError as error:
        return Reading(problem=f"cannot read the request ({_code(error)})")
    finally:
        os.close(fd)
    if raw is None:
        return Reading(problem="the request is larger than 1 KiB")
    fields, problem = _parse(raw, execution_id)
    if fields is None:
        return Reading(problem=problem)
    if info.st_nlink == 2:
        # The only second link allowed is the request's own temporary name, left
        # by a submitter that stopped between linkat and unlink (ADR-0025 §1a).
        try:
            companion = os.stat(temporary_name(execution_id, fields["request"]), dir_fd=inbox,
                                follow_symlinks=False)  # fmt: skip
        except FileNotFoundError:
            return Reading(problem="a hard link to the request from elsewhere")
        if (companion.st_dev, companion.st_ino) != (info.st_dev, info.st_ino):
            return Reading(problem="a hard link to the request from elsewhere")
    return Reading(
        CancelRequest(
            request_id=fields["request"],
            execution_id=execution_id,
            actor=fields["actor"],
            written_at=fields["written_at"],
            digest=_digest(raw),
            device=info.st_dev,
            inode=info.st_ino,
        )
    )


def _code(error: OSError) -> str:
    return errno.errorcode.get(error.errno or 0, "?")


def _check_file(info: os.stat_result, owner: int) -> str | None:
    if not stat.S_ISREG(info.st_mode):
        return "not a regular file"
    if info.st_uid != owner:
        return "owned by another user"
    if info.st_mode & 0o077:
        return f"open to other users (mode {info.st_mode & 0o777:o})"
    if info.st_size > MAX_BYTES:
        return "the request is larger than 1 KiB"
    if info.st_nlink not in (1, 2):
        return "a hard link to the request from elsewhere"
    return None


def _read_bounded(fd: int) -> bytes | None:
    """Read at most ``MAX_BYTES``; ``None`` if there is more (the file grew)."""
    chunks: list[bytes] = []
    size = 0
    while True:
        chunk = os.read(fd, MAX_BYTES + 1 - size)
        if not chunk:
            return b"".join(chunks)
        chunks.append(chunk)
        size += len(chunk)
        if size > MAX_BYTES:
            return None


def _parse(raw: bytes, execution_id: str) -> tuple[dict[str, str] | None, str | None]:
    try:
        text = raw.decode("utf-8")
        value = json.loads(text, object_pairs_hook=_no_duplicates, parse_constant=_no_constant)
    except (UnicodeDecodeError, ValueError, RecursionError):
        return None, "not a well-formed request"
    if not isinstance(value, dict) or set(value) != _KEYS:
        return None, "not a request: unexpected keys"
    if not all(isinstance(v, str) for v in value.values()):
        return None, "not a request: a field is not text"
    if value["schema"] != SCHEMA:
        return None, "an unknown request schema"
    if not _REQUEST_ID.match(value["request"]):
        return None, "a malformed request id"
    if value["execution"] != execution_id:
        return None, "the request names another execution"
    if not _actor_ok(value["actor"]):
        return None, "an invalid actor label"
    if not _WRITTEN_AT.match(value["written_at"]):
        return None, "a malformed time"
    if encode(value).encode("utf-8") != raw:
        return None, "not in canonical form"
    return value, None


def _no_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    keys = [key for key, _ in pairs]
    if len(set(keys)) != len(keys):
        raise ValueError("duplicate keys")
    return dict(pairs)


def _no_constant(name: str) -> Any:
    raise ValueError(f"{name} is not allowed")


def _actor_ok(label: str) -> bool:
    try:
        Actor(ActorKind.HUMAN, label)
    except ValueError:
        return False
    return True


def encode(fields: dict[str, str]) -> str:
    """The canonical form of a request: sorted keys, no spaces, ASCII only."""
    return json.dumps(fields, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _digest(raw: bytes) -> str:
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def _check_execution_id(execution_id: str) -> None:
    if not _IDENTIFIER.match(execution_id):
        raise ValueError("not an execution id")


# -- submission (ADR-0025 §1a): the first request wins --------------------------------------


@dataclass(frozen=True, slots=True)
class Submission:
    request: CancelRequest
    created: bool  # False: an earlier request stands, unchanged


def _step(name: str) -> None:
    """A point between the protocol's steps; tests replace it to crash a submitter."""


def submit(
    inbox: int,
    execution_id: str,
    actor: str,
    *,
    now: datetime,
    uid: int | None = None,
    new_id: Callable[[], str] = new_request_id,
) -> Submission:
    """Publish a request for ``execution_id`` unless one is already there.

    Raises :class:`InvalidEntryError` when something other than a valid request
    holds the name (it is left untouched), :class:`UnsafeInboxError` when the
    filesystem cannot make hard links, and ``ValueError`` for an invalid
    execution id or actor label."""
    if not _actor_ok(actor):
        raise ValueError("--as must be a short lower-case label, such as 'alice'")
    for _ in range(_ATTEMPTS):
        submission = _publish(inbox, execution_id, actor, now=now, uid=uid, new_id=new_id)
        if submission is not None:
            return submission
    raise InvalidEntryError(f"the inbox entry for {execution_id} kept changing; try again")


_ATTEMPTS = 3


def _publish(
    inbox: int,
    execution_id: str,
    actor: str,
    *,
    now: datetime,
    uid: int | None,
    new_id: Callable[[], str],
) -> Submission | None:
    """One attempt; ``None`` when nothing was published and the inbox changed under
    it (a standing entry was taken, or the temporary name was removed by the
    coordinator), so the caller tries again."""
    request_id = new_id()
    temporary = temporary_name(execution_id, request_id)
    final = final_name(execution_id)
    fields = {
        "schema": SCHEMA,
        "request": request_id,
        "execution": execution_id,
        "actor": actor,
        "written_at": now.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    data = encode(fields).encode("utf-8")
    fd = os.open(temporary, _CREATE_FLAGS, 0o600, dir_fd=inbox)
    try:
        _step("created")
        os.fchmod(fd, 0o600)  # whatever the umask
        written = 0
        while written < len(data):
            written += os.write(fd, data[written:])
        _step("written")
        os.fsync(fd)
        _step("synced")
        info = os.fstat(fd)
    finally:
        os.close(fd)
    mine = CancelRequest(
        request_id=request_id,
        execution_id=execution_id,
        actor=actor,
        written_at=fields["written_at"],
        digest=_digest(data),
        device=info.st_dev,
        inode=info.st_ino,
    )
    try:
        os.link(temporary, final, src_dir_fd=inbox, dst_dir_fd=inbox, follow_symlinks=False)
    except FileExistsError:
        _remove_quietly(inbox, temporary)
        standing = read_cancel_request(inbox, execution_id, uid=uid)
        if standing.request is not None:
            return Submission(standing.request, created=False)
        if standing.empty:
            return None
        raise InvalidEntryError(
            f"the inbox holds an invalid entry for {execution_id} ({standing.problem}); the "
            "coordinator will move it aside"
        ) from None
    except FileNotFoundError:
        # The coordinator removed the temporary name as an inert leftover (once the
        # execution finished or a request was applied): nothing was published.
        return None
    except OSError as error:
        _remove_quietly(inbox, temporary)
        if error.errno in (errno.EPERM, errno.EOPNOTSUPP, errno.EXDEV, errno.EMLINK):
            raise UnsafeInboxError("the inbox's filesystem cannot make hard links") from None
        raise
    _step("linked")
    _remove_quietly(inbox, temporary)
    _step("unlinked")
    os.fsync(inbox)
    return Submission(mine, created=True)  # the link published exactly what was written


def _remove_quietly(inbox: int, name: str) -> None:
    with contextlib.suppress(FileNotFoundError):
        os.unlink(name, dir_fd=inbox)


# -- cleanup (ADR-0025 §1a): rename first, verify after -------------------------------------


def take(inbox: int, execution_id: str, *, token: str) -> str | None:
    """Move whatever is at the request name to a private ``.applying-…`` name, or
    ``None`` if nothing is there. The name is unique by its 128 random bits."""
    private = applying_name(execution_id, token)
    try:
        os.rename(final_name(execution_id), private, src_dir_fd=inbox, dst_dir_fd=inbox)
    except FileNotFoundError:
        return None
    return private


def identify(inbox: int, name: str) -> Identity | None:
    """What ``name`` holds, never followed: device, inode, and a regular file's
    content digest. ``None`` if it is gone."""
    try:
        info = os.stat(name, dir_fd=inbox, follow_symlinks=False)
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_BYTES:
        return Identity(info.st_dev, info.st_ino, None)
    try:
        fd = os.open(name, _READ_FLAGS, dir_fd=inbox)
    except FileNotFoundError:
        return None
    except OSError:
        return Identity(info.st_dev, info.st_ino, None)
    try:
        opened = os.fstat(fd)
        raw = _read_bounded(fd)
    finally:
        os.close(fd)
    digest = None if raw is None else _digest(raw)
    return Identity(opened.st_dev, opened.st_ino, digest)


def matches(identity: Identity | None, request: CancelRequest) -> bool:
    return identity is not None and (identity.device, identity.inode, identity.digest) == (
        request.device,
        request.inode,
        request.digest,
    )


def remove(inbox: int, name: str) -> None:
    """Unlink a private name the worker chose, or a request's own temporary name."""
    _remove_quietly(inbox, name)


def set_aside(inbox: int, name: str, execution_id: str, *, token: str) -> str:
    """Rename the entry at ``name`` (never its target) to ``rejected/<execution>.<token>``."""
    if not _TOKEN.match(token):
        raise ValueError("not a token")
    with contextlib.suppress(FileExistsError):
        os.mkdir(REJECTED, 0o700, dir_fd=inbox)
    rejected = _open_dir(REJECTED, inbox, REJECTED)
    try:
        _check_dir(rejected, REJECTED, os.fstat(inbox).st_uid, private=True)
        target = f"{final_name(execution_id)[:-5]}.{token}"
        os.rename(name, target, src_dir_fd=inbox, dst_dir_fd=rejected)
    finally:
        os.close(rejected)
    return f"{REJECTED}/{target}"


def read_rejected(inbox: int, name: str, execution_id: str, *, uid: int | None = None) -> Reading:
    """Validate an entry already moved to ``rejected/``, by its name there."""
    rejected = _open_dir(REJECTED, inbox, REJECTED)
    try:
        return read_entry(rejected, name.removeprefix(f"{REJECTED}/"), execution_id,
                          inbox=inbox, uid=uid)  # fmt: skip
    finally:
        os.close(rejected)


def leftovers(inbox: int, execution_id: str) -> tuple[list[str], list[str]]:
    """The execution's ``.applying-…`` and ``.tmp-…`` names (from a listing, used only
    to find this execution's own private names; never to decide which execution a
    request is for)."""
    _check_execution_id(execution_id)
    applying, temporary = [], []
    for entry in os.listdir(inbox):
        if entry.startswith(f".applying-{execution_id}."):
            applying.append(entry)
        elif entry.startswith(f".tmp-{execution_id}."):
            temporary.append(entry)
    return sorted(applying), sorted(temporary)
