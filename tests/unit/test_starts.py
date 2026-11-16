# SPDX-License-Identifier: Apache-2.0
"""Durable restart-attempt accounting and the supervisor unit (ADR-0033).

Wiring into ``ilmar run --loop`` (and the exit 78 with or without a store)
comes with #91; these tests pin the log, the limits and the unit file.
"""

from __future__ import annotations

import configparser
import itertools
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from ilmar.core.coordinator import ExitCode
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
    old = [at(-3 * 24 * 60 - 60 * i) for i in range(12, 0, -1)]
    for when in old:  # more than the chain's tail: makes compaction run
        record_start(path, StartRecord(when, "old"))

    def fail(*_args: object) -> None:
        raise OSError("disk gone")

    monkeypatch.setattr(os, "replace", fail)
    with pytest.raises(StartLogError):
        start(tmp_path, 0)
    assert [r.at for r in read_starts(path)] == [*old, at(0)]


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
    assert set(service["RestartPreventExitStatus"].split()) == {"2", "3", "78"}
    assert "--loop" in service["ExecStart"].split()


def test_the_unit_bounds_restarts_by_itself() -> None:
    section = unit()["Unit"]
    assert section["StartLimitIntervalSec"] == "1d"
    assert 1 <= int(section["StartLimitBurst"]) <= 20


def directives(text: str) -> list[str]:
    """The unit's active lines: comments and blank lines dropped."""
    return [line.strip() for line in text.splitlines()
            if line.strip() and not line.lstrip().startswith(("#", ";"))]  # fmt: skip


def test_the_unit_holds_no_secret() -> None:
    text = UNIT.read_text()
    assert "Environment=" not in text.replace("EnvironmentFile=", "")
    assert "TOKEN" not in text
    assert "KEY" not in text


def test_the_unit_reads_no_plaintext_credential_file() -> None:
    """#117 review: no plaintext secrets file at rest. ``EnvironmentFile=`` and
    plaintext ``LoadCredential=``/``SetCredential=`` are refused; encrypted
    credentials are the proposed delivery."""
    active = directives(UNIT.read_text())
    keys = {line.split("=", 1)[0].strip() for line in active if "=" in line}
    assert not keys & {"EnvironmentFile", "Environment", "LoadCredential", "SetCredential",
                       "PassEnvironment"}  # fmt: skip
    assert not any("secrets.env" in line for line in active)
    assert "secrets.env" not in (UNIT.parents[2] / "docs/operations/supervisor.md").read_text()


def seconds(span: str) -> float:
    """A systemd time span in the forms the unit uses: ``90s``, ``12min``, ``1h``, ``1d``."""
    for suffix, factor in (("min", 60), ("s", 1), ("h", 3600), ("d", 86400)):
        if span.endswith(suffix) and span[: -len(suffix)].isdigit():
            return int(span[: -len(suffix)]) * factor
    raise AssertionError(f"unexpected time span {span!r}")


def systemd_delay(service: configparser.SectionProxy, restart: int) -> timedelta:
    """systemd's delay before automatic restart number ``restart`` (1-based).

    systemd 254 grows it from ``RestartSec`` to ``RestartMaxDelaySec`` over
    ``RestartSteps`` (R31). The fastest reading is assumed: geometric
    interpolation, the first restart at ``RestartSec``. Its counter never
    shrinks after healthy running (R32), so any other reading is slower."""
    first = seconds(service["RestartSec"])
    if "RestartSteps" not in service:
        return timedelta(seconds=first)
    steps = int(service["RestartSteps"])
    last = seconds(service["RestartMaxDelaySec"])
    step = min(restart - 1, steps)
    return timedelta(seconds=min(first * (last / first) ** (step / steps), last))


def test_the_unit_backs_off_exponentially() -> None:
    service = unit()["Service"]
    delays = [systemd_delay(service, k).total_seconds() for k in range(1, 9)]
    assert delays[0] == 120
    assert all(b > a for a, b in itertools.pairwise(delays[:7]))
    assert delays[-1] == 7200


def test_every_restart_systemd_makes_is_one_the_start_log_accepts(tmp_path: Path) -> None:
    """#117 review and ADR-0043 §4: the unit's own timing stays inside the start
    log's default limits, backoff included, so a permitted restart is never
    itself a restart loop.

    Worst case: every run fails the instant it starts, so systemd restarts it
    after exactly its delay for that restart, until ``StartLimitBurst`` starts
    in ``StartLimitIntervalSec``. Each of those starts must be accepted.
    """
    parsed = unit()
    service, section = parsed["Service"], parsed["Unit"]
    burst = int(section["StartLimitBurst"])
    window = timedelta(seconds=seconds(section["StartLimitIntervalSec"]))
    limits = StartLimits()  # the defaults loop mode uses
    for restart in range(1, burst + 1):
        assert systemd_delay(service, restart) >= limits.required_gap(restart)
    for first in (T0, datetime(2026, 11, 12, 23, 30, tzinfo=UTC)):  # also across midnight
        root = tmp_path / first.strftime("%H%M")
        now = first
        starts: list[datetime] = []
        while True:
            if sum(1 for s in starts if now - s < window) >= burst:
                break  # systemd refuses further starts within its window
            starts.append(now)
            found = account_start(root, now=now, boot_id="boot-1", limits=limits)
            assert found is None, f"start {len(starts)} at {now}: {found}"
            now += systemd_delay(service, len(starts))
        assert len(starts) == burst
        assert burst <= limits.per_day


def test_without_restart_steps_a_fixed_delay_must_be_the_backoff_cap(tmp_path: Path) -> None:
    """ADR-0043 §4: on systemd older than 254, only a fixed ``RestartSec`` of at
    least ``backoff_max_seconds`` (1 h) is never faster than the log. ADR-0033's
    12 minutes is suspended at the sixth start of a chain: safe, but not slower."""
    limits = StartLimits()
    assert all(timedelta(hours=1) >= limits.required_gap(n) for n in range(1, 30))
    root, now = tmp_path, T0
    for number in range(1, 6):
        assert account_start(root, now=now, boot_id="b", limits=limits) is None, number
        now += timedelta(minutes=12)
    found = account_start(root, now=now, boot_id="b", limits=limits)
    assert found is not None
    assert "backoff" in found


def test_a_restart_inside_the_minimum_interval_would_be_refused(tmp_path: Path) -> None:
    """Why the old ``RestartSec=30s`` was wrong: the first restart breached."""
    assert start(tmp_path, 0, StartLimits()) is None
    assert start(tmp_path, 0.5, StartLimits()) is not None


# -- backoff and cooldown (ADR-0043 §1) -----------------------------------------------

WIDE = StartLimits(per_hour=6, per_day=20)  # the defaults


def test_each_restart_of_a_chain_waits_twice_as_long(tmp_path: Path) -> None:
    """Gaps of 1, 2, 4, 8, 16 and 32 minutes are just enough."""
    minute = 0.0
    assert start(tmp_path, minute, WIDE) is None
    for gap in (1, 2, 4, 8, 16, 32):
        minute += gap
        assert start(tmp_path, minute, WIDE) is None, gap


@pytest.mark.parametrize("earlier", [1, 2, 3, 4, 5, 6])
def test_a_restart_one_second_early_is_a_loop(tmp_path: Path, earlier: int) -> None:
    minute = 0.0
    assert start(tmp_path, minute, WIDE) is None
    for n in range(1, earlier):
        minute += 2 ** (n - 1)
        assert start(tmp_path, minute, WIDE) is None
    found = start(tmp_path, minute + 2 ** (earlier - 1) - 1 / 60, WIDE)
    assert found is not None
    # The first restart's gap is the minimum interval, which reports itself.
    expected = "within 60 seconds" if earlier == 1 else f"restart {earlier} of a chain"
    assert expected in found


def test_the_gap_is_capped_at_backoff_max() -> None:
    assert WIDE.required_gap(7) == timedelta(hours=1)
    assert WIDE.required_gap(500) == timedelta(hours=1)
    assert WIDE.required_gap(0) == timedelta(0)
    assert WIDE.chain_depth == 7


def test_a_quiet_cooldown_resets_the_chain(tmp_path: Path) -> None:
    """A run that stayed up longer than the cooldown restarts after a minute."""
    minute = 0.0
    for gap in (0, 1, 2, 4, 8):
        minute += gap
        assert start(tmp_path, minute, WIDE) is None
    minute += 6 * 60  # six quiet hours
    assert start(tmp_path, minute, WIDE) is None
    assert start(tmp_path, minute + 1, WIDE) is None  # chain of one: one minute
    found = start(tmp_path, minute + 2, WIDE)  # the next needs two
    assert found is not None
    assert "restart 2 of a chain" in found


def test_a_chain_older_than_a_day_keeps_its_backoff(tmp_path: Path) -> None:
    """Compaction keeps the chain's tail, so a long chain never forgets its depth."""
    limits = StartLimits(per_day=20, cooldown_seconds=7 * 86400)
    minute = 0.0
    for _ in range(8):  # a full chain, one an hour, over the cap
        assert start(tmp_path, minute, limits) is None
        minute += 60
    minute += 2 * 24 * 60  # beyond the daily window, inside the cooldown
    assert start(tmp_path, minute, limits) is None
    assert len(read_starts(start_log_path(tmp_path))) == limits.chain_depth + 1
    found = start(tmp_path, minute + 59, limits)  # the cap still holds: one hour
    assert found is not None
    assert "backoff needs 3600" in found


def test_a_future_dated_record_continues_the_chain(tmp_path: Path) -> None:
    path = start_log_path(tmp_path)
    record_start(path, StartRecord(at(600), "clock-ahead"))
    found = start(tmp_path, 0, WIDE)
    assert found is not None  # inside every window: stricter, never looser


def test_resume_waits_for_the_backoff(tmp_path: Path) -> None:
    for minute in (0, 1, 3):
        assert start(tmp_path, minute, WIDE) is None
    records = read_starts(start_log_path(tmp_path))
    assert breach_if_started(records, now=at(6.9), limits=WIDE) is not None  # needs 4 min
    assert breach_if_started(records, now=at(7), limits=WIDE) is None


def test_a_supervisor_ignoring_its_delays_is_suspended(tmp_path: Path) -> None:
    """Crash loop: restarts every 90 seconds, faster than the backoff allows."""
    found = None
    minute = 0.0
    for _ in range(6):
        found = start(tmp_path, minute, WIDE)
        if found is not None:
            break
        minute += 1.5
    assert found is not None
    assert "backoff" in found


# -- which exits the shipped unit restarts (ADR-0033 §1, ADR-0043) ---------------------


def systemd_restarts(service: configparser.SectionProxy, code: int) -> bool:
    """``Restart=on-failure``: any non-zero exit, unless it is listed in
    ``RestartPreventExitStatus``; never a clean exit 0."""
    assert service["Restart"] == "on-failure"
    prevented = {int(c) for c in service["RestartPreventExitStatus"].split()}
    return code != 0 and code not in prevented


@pytest.mark.parametrize(
    ("code", "restarted"),
    [
        (ExitCode.SUCCESS, False),  # a clean stop
        (ExitCode.FAILURE, True),  # a pass failed: retried, bounded by the start log
        (ExitCode.BLOCKED, False),  # needs a person: never (maintainer decision)
        (ExitCode.CONFIGURATION, False),  # never (also: supervision off by default)
        (ExitCode.SUSPENDED, False),  # never: a person resumes
    ],
)
def test_the_unit_restarts_only_a_failed_pass(code: ExitCode, restarted: bool) -> None:
    assert systemd_restarts(unit()["Service"], int(code)) is restarted


def test_every_exit_code_ilmar_uses_is_covered() -> None:
    assert {int(c) for c in ExitCode} == {0, 1, 2, 3, 78}
