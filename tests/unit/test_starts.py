# SPDX-License-Identifier: Apache-2.0
"""Durable restart-attempt accounting and the supervisor unit (ADR-0033).

Wiring into ``ilmar run --loop`` (and the exit 78 with or without a store)
comes with #91; these tests pin the log, the limits and the unit file.
"""

from __future__ import annotations

import configparser
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from ilmar.core.starts import (
    StartLimits,
    StartRecord,
    account_start,
    breach,
    breach_if_started,
    read_starts,
    record_start,
    start_log_path,
)
from ilmar.core.suspension import resume_blocker
from ilmar.domain.budget import BudgetTotals, Suspension, SuspensionReason
from ilmar.domain.errors import StartLogError

T0 = datetime(2026, 11, 12, 10, 0, tzinfo=UTC)
LIMITS = StartLimits(min_interval_seconds=60, per_hour=3, per_day=5)
UNIT = Path(__file__).resolve().parents[2] / "deploy" / "systemd" / "ilmar.service"


def at(minutes: float) -> datetime:
    return T0 + timedelta(minutes=minutes)


def start(root: Path, minutes: float, limits: StartLimits = LIMITS) -> str | None:
    return account_start(root, now=at(minutes), boot_id="boot-1", limits=limits)


# -- the log --------------------------------------------------------------------------


def test_a_start_is_appended_and_fsynced_before_anything_else(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    synced: list[str] = []
    real = os.fsync

    def fsync(fd: int) -> None:
        synced.append("dir" if os.path.isdir(f"/proc/self/fd/{fd}") else "file")
        real(fd)

    monkeypatch.setattr(os, "fsync", fsync)
    records = record_start(start_log_path(tmp_path), StartRecord(at(0), "boot-1"))
    assert records == (StartRecord(at(0), "boot-1"),)
    assert synced == ["file", "dir"]
    assert (tmp_path / "starts.log").stat().st_mode & 0o777 == 0o600


def test_a_fault_after_the_write_keeps_the_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failure at any later step (here the compaction) leaves the record."""
    path = start_log_path(tmp_path)
    record_start(path, StartRecord(at(-3 * 24 * 60), "old"))  # makes compaction run

    def fail(*_args: object) -> None:
        raise OSError("disk gone")

    monkeypatch.setattr(os, "replace", fail)
    with pytest.raises(StartLogError):
        start(tmp_path, 0)
    assert [r.at for r in read_starts(path)] == [at(-3 * 24 * 60), at(0)]


@pytest.mark.parametrize(
    "content",
    [
        b'{"at":"2026-11-12T10:00:00+00:00","boot":"b"}',  # torn: no newline
        b"not json\n",
        b'{"at":"2026-11-12T10:00:00","boot":"b"}\n',  # naive time
        b'{"at":"2026-11-12T10:00:00+00:00","boot":"b","x":1}\n',
        b'{"at":"2026-11-12T10:00:00+00:00","boot":""}\n',
        b'{"at":5,"boot":"b"}\n',
        b"[]\n",
    ],
)
def test_a_malformed_log_fails_closed(tmp_path: Path, content: bytes) -> None:
    (tmp_path / "starts.log").write_bytes(content)
    with pytest.raises(StartLogError):
        start(tmp_path, 0)


def test_an_unwritable_log_fails_closed(tmp_path: Path) -> None:
    (tmp_path / "starts.log").mkdir()  # a directory where the file should be
    with pytest.raises(StartLogError):
        start(tmp_path, 0)


def test_a_symlinked_log_is_refused(tmp_path: Path) -> None:
    target = tmp_path / "elsewhere"
    target.write_text("")
    (tmp_path / "starts.log").symlink_to(target)
    with pytest.raises(StartLogError):
        start(tmp_path, 0)
    assert target.read_text() == ""


# -- the limits -----------------------------------------------------------------------


def test_ordinary_restarts_are_allowed(tmp_path: Path) -> None:
    assert start(tmp_path, 0) is None
    assert start(tmp_path, 30) is None
    assert start(tmp_path, 90) is None


def test_a_start_within_the_minimum_interval_is_a_loop(tmp_path: Path) -> None:
    assert start(tmp_path, 0) is None
    found = start(tmp_path, 0.5)
    assert found is not None
    assert "within 60 seconds" in found


def test_the_hourly_limit(tmp_path: Path) -> None:
    for minutes in (0, 10, 20):
        assert start(tmp_path, minutes) is None
    found = start(tmp_path, 30)
    assert found == "4 starts in the last hour (at most 3)"
    assert start(tmp_path, 75) is None  # 0 and 10 have left the hour; 20, 30 and 75 remain


def test_the_daily_limit_counts_the_utc_day(tmp_path: Path) -> None:
    for minutes in (0, 70, 140, 210, 280):
        assert start(tmp_path, minutes) is None
    found = start(tmp_path, 350)
    assert found == "6 starts this UTC day (at most 5)"
    midnight = (datetime(2026, 11, 13, tzinfo=UTC) - T0).total_seconds() / 60
    assert start(tmp_path, midnight + 1) is None  # a new UTC day


def test_a_clock_that_went_backwards_only_makes_it_stricter(tmp_path: Path) -> None:
    assert start(tmp_path, 600) is None  # recorded at a later time
    found = start(tmp_path, 0)  # the clock stepped back ten hours
    assert found is not None
    assert "within 60 seconds" in found  # a future record is "too recent"
    # Even with no minimum interval, a future record is never "long ago".
    records = (StartRecord(at(600), "b"), StartRecord(at(0), "b"))
    zero_gap = StartLimits(min_interval_seconds=0, per_hour=9, per_day=9)
    assert breach(records, now=at(0), limits=zero_gap) is not None


def test_old_records_are_compacted_away_and_future_ones_kept(tmp_path: Path) -> None:
    path = start_log_path(tmp_path)
    record_start(path, StartRecord(at(-3 * 24 * 60), "old"))
    record_start(path, StartRecord(at(24 * 60), "future"))
    loose = StartLimits(min_interval_seconds=0, per_hour=9, per_day=9)
    assert start(tmp_path, 0, loose) is not None  # the future record is "too recent"
    assert [r.boot_id for r in read_starts(path)] == ["future", "boot-1"]
    assert not (tmp_path / "starts.log.new").exists()


# -- resuming -------------------------------------------------------------------------


def test_resume_lifts_a_restart_loop_only_without_a_breach(tmp_path: Path) -> None:
    suspension = Suspension(SuspensionReason.RESTART_LOOP, at(0), "")
    for minutes in (0, 10, 20, 30):
        start(tmp_path, minutes)

    def blocker(minutes: float) -> str | None:
        found = breach_if_started(
            read_starts(start_log_path(tmp_path)), now=at(minutes), limits=LIMITS
        )
        return resume_blocker(
            suspension, now=at(minutes), totals=BudgetTotals(0, 0), limits=None,
            latest_update=None, provider_cap_raised=False, kill_switch_present=False,
            restart_breach=found,
        )  # fmt: skip

    still = blocker(31)
    assert still is not None
    assert still.startswith("the start log still shows")
    assert blocker(95) is None
    assert len(read_starts(start_log_path(tmp_path))) == 4  # resuming writes no record


# -- the unit file --------------------------------------------------------------------


def unit() -> configparser.ConfigParser:
    parser = configparser.ConfigParser(strict=False, interpolation=None)
    parser.optionxform = str  # type: ignore[assignment,method-assign]
    parser.read(UNIT)
    return parser


def test_the_unit_never_restarts_a_suspension_or_a_configuration_error() -> None:
    service = unit()["Service"]
    assert service["Restart"] == "on-failure"
    assert set(service["RestartPreventExitStatus"].split()) == {"3", "78"}
    assert "--loop" in service["ExecStart"].split()
    assert "RestartMaxDelaySec" in service
    assert "RestartSteps" in service


def test_the_unit_bounds_restarts_by_itself() -> None:
    section = unit()["Unit"]
    assert section["StartLimitIntervalSec"] == "1d"
    assert 1 <= int(section["StartLimitBurst"]) <= 20


def test_the_unit_holds_no_secret() -> None:
    text = UNIT.read_text()
    assert "Environment=" not in text.replace("EnvironmentFile=", "")
    assert "TOKEN" not in text
    assert "KEY" not in text
