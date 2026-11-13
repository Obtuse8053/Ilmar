# SPDX-License-Identifier: Apache-2.0
"""``ilmar suspend``, ``ilmar resume`` and ``ilmar budget`` (ADR-0029 §3, #18).

``suspend`` and ``resume`` take the instance lock and write the durable
suspension record with its event, in one fenced commit. They open only the
execution store: no tracker, forge, container runtime or secret. While a
coordinator runs, the lock is held and they report that. ``budget`` only reads.

Exit codes:

======  =============================================================
0       done (or, for ``budget``, nothing suspended)
3       configuration problem, or the instance is in use
78      ``resume`` refused: the cause is not gone; ``budget``: suspended
======  =============================================================
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import TextIO, cast

from ilmar.adapters.registry import event_reader, store_writer
from ilmar.config import IlmarConfig, load_config
from ilmar.core import kill_switch
from ilmar.core.ownership import Ownership
from ilmar.core.starts import StartLimits, breach_if_started, read_starts, start_log_path
from ilmar.core.suspension import resume_blocker
from ilmar.domain.budget import Suspension, SuspensionReason, micros_to_usd
from ilmar.domain.errors import ConfigError, FencingError, LedgerIntegrityError, StartLogError
from ilmar.domain.events import Actor, ActorKind, EventContext
from ilmar.domain.ledger import Commit
from ilmar.ports.execution_store import ExecutionStore

OK, CONFIGURATION, SUSPENDED = 0, 3, 78
_DETAIL_LIMIT = 200


def add_parsers(commands: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    suspend = commands.add_parser("suspend", help="stop all new work until 'ilmar resume'")
    suspend.add_argument("--config", required=True, help="the configuration file")
    suspend.add_argument("--note", default="", help="a short note recorded with it")
    suspend.add_argument(
        "--as",
        dest="actor",
        required=True,
        metavar="NAME",
        help="who suspends, recorded with the event (a short lower-case label)",
    )
    resume = commands.add_parser("resume", help="lift a suspension whose cause is gone")
    resume.add_argument("--config", required=True, help="the configuration file")
    resume.add_argument(
        "--as",
        dest="actor",
        required=True,
        metavar="NAME",
        help="who resumes, recorded with the event (a short lower-case label)",
    )
    resume.add_argument(
        "--provider-cap-resolved",
        action="store_true",
        help="for a provider-cap suspension: you confirm the provider's cause is resolved",
    )
    resume.add_argument(
        "--check-fault-resolved",
        action="store_true",
        help="for a suspension-check-failed suspension: you confirm its cause is fixed; "
        "the check must also succeed now",
    )
    budget = commands.add_parser("budget", help="show charges, allowances and any suspension")
    budget.add_argument("--config", required=True, help="the configuration file")


def run(
    args: argparse.Namespace,
    *,
    out: TextIO = sys.stdout,
    err: TextIO = sys.stderr,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> int:
    try:
        config = load_config(Path(args.config))
    except ConfigError as exc:
        print(f"error: {exc}", file=err)
        return CONFIGURATION
    if args.command == "budget":
        return _status(config, out=out, err=err, now=now())
    try:
        actor = Actor(ActorKind.HUMAN, args.actor)
    except ValueError as exc:
        print(f"error: --as: {exc}", file=err)
        return CONFIGURATION
    try:
        layout, open_store = store_writer(config)
    except ConfigError as exc:
        print(f"error: {exc}", file=err)
        return CONFIGURATION
    config.state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    store = cast(ExecutionStore, open_store())  # the execution_store port, checked by plan
    try:
        try:
            ownership = Ownership.take(
                layout=layout, store=store, instance_id=config.instance_id, now=now()
            )
        except (FencingError, OSError) as exc:
            print(f"error: cannot own this instance (is a coordinator running?): {exc}", file=err)
            return CONFIGURATION
        with ownership:
            at = now()
            context = EventContext(
                at=at, instance=config.instance_id, epoch=ownership.fence.epoch, actor=actor
            )
            current = store.suspension()
            if args.command == "suspend":
                if current is not None:
                    print(
                        f"already suspended: {current.reason.value} since {current.since}", file=out
                    )
                    return OK
                note = args.note[:_DETAIL_LIMIT]
                if not note.isprintable():
                    print("error: --note must be printable text", file=err)
                    return CONFIGURATION
                commit = Commit(suspend=Suspension(SuspensionReason.OPERATOR, at, note))
                store.commit(ownership.fence, commit, context=context)
                print("suspended: no new work starts until 'ilmar resume'", file=out)
                return OK
            if current is None:
                print("not suspended", file=out)
                return OK
            limits = config.budget.limits if config.budget else None
            restart_breach = None
            if current.reason is SuspensionReason.RESTART_LOOP:
                try:
                    restart_breach = breach_if_started(
                        read_starts(start_log_path(layout.coordinator_root)),
                        now=at,
                        limits=StartLimits(),
                    )
                except StartLogError as exc:
                    restart_breach = f"an unusable start log ({exc})"
            blocker = resume_blocker(
                current,
                now=at,
                totals=store.budget_totals(at),
                limits=limits,
                latest_update=store.latest_update(),
                provider_cap_raised=args.provider_cap_resolved,
                check_fault_resolved=args.check_fault_resolved,
                kill_switch_present=kill_switch.checker(
                    config.budget.kill_switch_file if config.budget else None
                )(),
                restart_breach=restart_breach,
            )
            if blocker is not None:
                print(f"still suspended ({current.reason.value}): {blocker}", file=err)
                return SUSPENDED
            try:
                store.commit(ownership.fence, Commit(resume=at), context=context)
            except LedgerIntegrityError as exc:
                print(f"error: {exc}", file=err)
                return CONFIGURATION
            print(f"resumed: the {current.reason.value} suspension is lifted", file=out)
            return OK
    finally:
        close = getattr(store, "close", None)
        if callable(close):
            close()


def _status(config: IlmarConfig, *, out: TextIO, err: TextIO, now: datetime) -> int:
    try:
        opener = event_reader(config)
    except ConfigError as exc:
        print(f"error: {exc}", file=err)
        return CONFIGURATION
    try:
        store = opener()
    except (OSError, sqlite3.Error):
        print("no execution store yet: nothing has been charged, and nothing is suspended",
              file=out)  # fmt: skip
        return OK
    try:
        suspension = store.suspension()  # type: ignore[attr-defined]
        totals = store.budget_totals(now)  # type: ignore[attr-defined]
    finally:
        store.close()  # type: ignore[attr-defined]
    if config.budget is None:
        print("budget: none configured (no reservations are charged)", file=out)
    else:
        limits = config.budget.limits
        print(f"today (UTC): {micros_to_usd(totals.day_micros)} of "
              f"{micros_to_usd(limits.daily_micros)} USD reserved", file=out)  # fmt: skip
        print(f"this month: {micros_to_usd(totals.month_micros)} of "
              f"{micros_to_usd(limits.monthly_micros)} USD reserved", file=out)  # fmt: skip
        print(f"reservation per attempt: {micros_to_usd(limits.reserve_micros)} USD", file=out)
    if suspension is None:
        print("not suspended", file=out)
        return OK
    note = f" ({suspension.detail})" if suspension.detail else ""
    print(f"suspended: {suspension.reason.value} since {suspension.since.isoformat()}{note}",
          file=out)  # fmt: skip
    return SUSPENDED
