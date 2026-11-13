# SPDX-License-Identifier: Apache-2.0
"""Durable restart-attempt accounting for loop mode (ADR-0033 §2-§4).

``record_start`` is the first thing ``ilmar run --loop`` does: before the
store is opened, ownership is claimed or anything is contacted, it appends
one record to ``<state_dir>/coordinator/starts.log`` and fsyncs the file and
its directory. :func:`breach` then decides, from the log alone, whether this
start is part of a restart loop.

Ownership claims are not used: a process can crash before it claims, or never
reach the store. A record, once written, survives every later crash.

Fail closed: a log that cannot be read, written or parsed raises
:class:`StartLogError`, and the caller exits 78 with nothing started.
"""

from __future__ import annotations

import json
import os
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from ilmar.domain.errors import StartLogError

START_LOG_NAME = "starts.log"
HOUR = timedelta(hours=1)
DAY = timedelta(days=1)
_MAX_LINE = 512
_APPEND = os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
_READ = os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)


@dataclass(frozen=True, slots=True)
class StartLimits:
    """Conservative defaults: one start a minute, six an hour, twenty a UTC day."""

    min_interval_seconds: float = 60.0
    per_hour: int = 6
    per_day: int = 20

    def __post_init__(self) -> None:
        if self.min_interval_seconds < 0:
            raise ValueError("min_interval_seconds must not be negative")
        if self.per_hour < 1 or self.per_day < 1:
            raise ValueError("per_hour and per_day must be at least 1")

    @property
    def longest_window(self) -> timedelta:
        return max(DAY, timedelta(seconds=self.min_interval_seconds))


@dataclass(frozen=True, slots=True)
class StartRecord:
    at: datetime
    boot_id: str

    def __post_init__(self) -> None:
        if self.at.tzinfo is None:
            raise ValueError("a start record's time must be timezone-aware")
        if not self.boot_id or len(self.boot_id) > 64 or not self.boot_id.isprintable():
            raise ValueError("a boot id is 1 to 64 printable characters")

    def line(self) -> bytes:
        text = json.dumps(
            {"at": self.at.astimezone(UTC).isoformat(), "boot": self.boot_id},
            sort_keys=True,
            separators=(",", ":"),
        )
        return (text + "\n").encode()


def start_log_path(coordinator_root: Path) -> Path:
    """``<state_dir>/coordinator/starts.log`` for the default layout."""
    return coordinator_root / START_LOG_NAME


def record_start(path: Path, record: StartRecord) -> tuple[StartRecord, ...]:
    """Append ``record`` durably, then return every record in the log, it included.

    The append is one ``write`` on an ``O_APPEND`` descriptor, followed by an
    fsync of the file and of its directory. Nothing else may run before this
    returns.
    """
    directory = path.parent
    try:
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = os.open(path, _APPEND, 0o600)
        try:
            data = record.line()
            if os.write(fd, data) != len(data):
                raise StartLogError(f"{path}: the start record was written only in part")
            os.fsync(fd)
        finally:
            os.close(fd)
        _fsync_directory(directory)
    except OSError as error:
        raise StartLogError(f"{path}: the start record cannot be written: {error}") from error
    return read_starts(path)


def read_starts(path: Path) -> tuple[StartRecord, ...]:
    """Every record, in file order. Any malformed line fails closed."""
    try:
        fd = os.open(path, _READ)
    except FileNotFoundError:
        return ()
    except OSError as error:
        raise StartLogError(f"{path}: the start log cannot be read: {error}") from error
    try:
        with os.fdopen(fd, "rb") as handle:
            content = handle.read()
    except OSError as error:
        raise StartLogError(f"{path}: the start log cannot be read: {error}") from error
    if content and not content.endswith(b"\n"):
        raise StartLogError(f"{path}: the last start record is incomplete")
    records: list[StartRecord] = []
    for number, raw in enumerate(content.splitlines(), start=1):
        records.append(_parse(raw, path, number))
    return tuple(records)


def breach(records: Sequence[StartRecord], *, now: datetime, limits: StartLimits) -> str | None:
    """Why this start is part of a restart loop, or ``None``.

    ``records`` ends with this start's own record. A record dated after ``now``
    counts as inside every window and as too recent (ADR-0028): a clock that
    went backwards can only make this stricter.
    """
    if not records:
        raise StartLogError("the start log does not hold this start's own record")
    *earlier, _own = records
    gap = timedelta(seconds=limits.min_interval_seconds)
    if earlier and any(now - r.at < gap for r in earlier):
        return f"started again within {limits.min_interval_seconds:g} seconds of an earlier start"
    in_hour = sum(1 for r in records if now - r.at < HOUR)
    if in_hour > limits.per_hour:
        return f"{in_hour} starts in the last hour (at most {limits.per_hour})"
    today = now.astimezone(UTC).date()
    in_day = sum(1 for r in records if r.at > now or r.at.astimezone(UTC).date() == today)
    if in_day > limits.per_day:
        return f"{in_day} starts this UTC day (at most {limits.per_day})"
    return None


def breach_if_started(
    records: Sequence[StartRecord], *, now: datetime, limits: StartLimits
) -> str | None:
    """Whether a start at ``now`` would breach: for ``ilmar resume`` (ADR-0033 §3),
    which writes no record."""
    return breach((*records, StartRecord(now, "resume-check")), now=now, limits=limits)


def compact(path: Path, records: Sequence[StartRecord], *, now: datetime, keep: timedelta) -> None:
    """Rewrite the log without records older than ``keep``, atomically.

    Future-dated records are always kept. The new file is written, fsynced and
    renamed over the old one, then the directory is fsynced.
    """
    kept = [r for r in records if now - r.at < keep]
    if len(kept) == len(records):
        return
    temporary = path.with_name(path.name + ".new")
    try:
        temporary.unlink(missing_ok=True)
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        try:
            data = b"".join(r.line() for r in kept)
            if data and os.write(fd, data) != len(data):
                raise StartLogError(f"{temporary}: written only in part")
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    except OSError as error:
        raise StartLogError(f"{path}: the start log cannot be compacted: {error}") from error


# -- internals ----------------------------------------------------------------------


def _parse(raw: bytes, path: Path, number: int) -> StartRecord:
    where = f"{path}: start record {number}"
    if len(raw) > _MAX_LINE:
        raise StartLogError(f"{where} is too long")
    try:
        data = json.loads(raw)
        if not isinstance(data, dict) or set(data) != {"at", "boot"}:
            raise ValueError("unexpected fields")
        at = datetime.fromisoformat(data["at"])
        return StartRecord(at=at, boot_id=data["boot"])
    except (ValueError, TypeError) as error:
        raise StartLogError(f"{where} is malformed: {error}") from error


def _fsync_directory(directory: Path) -> None:
    fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def account_start(
    coordinator_root: Path, *, now: datetime, boot_id: str, limits: StartLimits
) -> str | None:
    """The first step of ``ilmar run --loop``: record this start durably, then
    decide from the log alone. Returns the breach, or ``None`` to go on.

    Raises :class:`StartLogError` (exit 78) when the log is unusable. Records
    older than the longest window are dropped afterwards; that never affects the
    decision, which is already made.
    """
    path = start_log_path(coordinator_root)
    records = record_start(path, StartRecord(now, boot_id))
    found = breach(records, now=now, limits=limits)
    compact(path, records, now=now, keep=limits.longest_window)
    return found
