# SPDX-License-Identifier: Apache-2.0
"""``ilmar blocked list | release | abandon`` (ADR-0031 §3, #92 part 2).

A task whose candidate touched a protected safety control keeps Ilmar's claim
and is *blocked*. Only a maintainer resolves it:

* ``list`` shows every blocked task with what its failure recorded (read only);
* ``release TASK --as NAME`` releases the claim and lets the task be picked
  again, as a new execution;
* ``abandon TASK --as NAME`` releases the claim and keeps the task out of
  Ilmar's backlog.

``release`` and ``abandon`` take the instance lock and write one fenced commit:
the maintainer's decision and the journaled intent of the claim release. They
open only the execution store: no tracker, forge or secret. The tracker call
happens at the next ``ilmar run``, through the journal, which repeats it after
a crash or an ambiguous outcome. While a coordinator runs, the lock is held and
they report that.

Exit codes:

======  =============================================================
0       done
1       nothing blocked matches the task, or it is already resolved
3       configuration problem, or the instance is in use
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
from ilmar.core import blocked
from ilmar.core.ownership import Ownership
from ilmar.domain.errors import (
    ConcurrencyConflictError,
    ConfigError,
    FencingError,
    LedgerIntegrityError,
)
from ilmar.domain.events import Actor, ActorKind
from ilmar.ports.execution_store import ExecutionStore

OK, NOT_FOUND, CONFIGURATION = 0, 1, 3
_ERROR_LIMIT = 300


def add_parser(commands: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    parser = commands.add_parser("blocked", help="list or resolve tasks blocked for a maintainer")
    actions = parser.add_subparsers(dest="action", required=True)
    listing = actions.add_parser("list", help="show every blocked task")
    listing.add_argument("--config", required=True, help="the configuration file")
    for name, what in (
        ("release", "release the claim; the task may be picked again as a new execution"),
        ("abandon", "release the claim; Ilmar never picks the task again"),
    ):
        action = actions.add_parser(name, help=what)
        action.add_argument("task", help="the task, as provider:key or key")
        action.add_argument("--config", required=True, help="the configuration file")
        action.add_argument(
            "--as",
            dest="actor",
            required=True,
            metavar="NAME",
            help="who decides, recorded with the event (a short lower-case label)",
        )


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
    if args.action == "list":
        return _list(config, out=out, err=err)
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
            try:
                execution = blocked.find(store, args.task)
            except blocked.BlockedError as exc:
                print(f"error: {exc}", file=err)
                return NOT_FOUND
            try:
                commit, context = blocked.resolve(
                    store, execution, args.action, fence=ownership.fence, now=now(),
                    instance=config.instance_id, actor=actor,
                )  # fmt: skip
            except blocked.BlockedError as exc:
                print(f"error: {exc}", file=err)
                return NOT_FOUND
            try:
                store.commit(ownership.fence, commit, context=context)
            except (ConcurrencyConflictError, LedgerIntegrityError) as exc:
                print(f"error: {exc}", file=err)
                return CONFIGURATION
            task = f"{execution.task.provider}:{execution.task.key}"
            done = (
                "may be picked again"
                if args.action == "release"
                else "stays out of Ilmar's backlog"
            )
            print(
                f"{args.action}d {task} ({execution.id}): its claim is released at the next "
                f"'ilmar run', then the task {done}",
                file=out,
            )
            return OK
    finally:
        close = getattr(store, "close", None)
        if callable(close):
            close()


def _list(config: IlmarConfig, *, out: TextIO, err: TextIO) -> int:
    try:
        opener = event_reader(config)
    except ConfigError as exc:
        print(f"error: {exc}", file=err)
        return CONFIGURATION
    try:
        store = cast(ExecutionStore, opener())
    except (OSError, sqlite3.Error):
        print("no execution store yet: nothing is blocked", file=out)
        return OK
    try:
        found = blocked.blocked(store)
        if not found:
            print("nothing is blocked", file=out)
            return OK
        for execution in found:
            error = blocked.protected_paths(store, execution)[:_ERROR_LIMIT]
            print(
                f"{execution.task.provider}:{execution.task.key}\t{execution.id}\t"
                f"attempt {execution.attempt}\t{error}",
                file=out,
            )
        return OK
    finally:
        close = getattr(store, "close", None)
        if callable(close):
            close()
