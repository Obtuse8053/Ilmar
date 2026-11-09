# SPDX-License-Identifier: Apache-2.0
"""``ilmar cancel`` (ADR-0025 §1).

The command only publishes a request in the coordinator's private inbox
(``<coordinator_root>/requests/cancel/<execution-id>.json``). It takes no
instance lock and never writes the execution store: it opens the store
read-only to refuse an unknown or finished execution early, and to report an
accepted request. The worker that holds the execution's lease applies the
request and records ``execution.cancel_requested``; the first request for an
execution wins and is never overwritten.

Exit codes:

======  ===================================================================
0       a request was published, or one is already pending or accepted
3       configuration or usage problem; unknown or finished execution;
        an unsafe inbox or an invalid entry in it
======  ===================================================================
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol, TextIO, runtime_checkable

from ilmar.adapters.registry import event_reader
from ilmar.config import load_config
from ilmar.core import cancel_inbox
from ilmar.core.layout import WorkspaceLayout
from ilmar.domain.errors import ConfigError, FencingError
from ilmar.domain.events import Event, EventType
from ilmar.domain.execution import Execution

OK, CONFIGURATION = 0, 3


@runtime_checkable
class CancelReader(Protocol):
    """A store opened read-only; it never migrates and never writes."""

    def get(self, execution_id: str) -> Execution | None: ...

    def events(
        self, *, after_seq: int = 0, limit: int | None = None, execution_id: str | None = None
    ) -> Sequence[Event]: ...

    def close(self) -> None: ...


def add_parser(commands: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    cancel = commands.add_parser(
        "cancel",
        help="ask the coordinator to cancel an execution (needs no instance lock)",
        description="Publish a cancellation request. The coordinator applies it.",
    )
    cancel.add_argument("execution", metavar="EXECUTION_ID")
    cancel.add_argument("--config", required=True, help="the configuration file")
    cancel.add_argument(
        "--as",
        dest="actor",
        required=True,
        metavar="NAME",
        help="who asks, recorded with the request (a short lower-case label)",
    )


def run(
    args: argparse.Namespace,
    *,
    out: TextIO = sys.stdout,
    err: TextIO = sys.stderr,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
    uid: int | None = None,
) -> int:
    """Run ``ilmar cancel`` for parsed ``args``."""
    execution_id: str = args.execution
    try:
        config = load_config(Path(args.config))
        open_store = event_reader(config)
        layout = WorkspaceLayout.under(config.state_dir)
    except (ConfigError, ValueError) as error:
        print(f"error: {error}", file=err)
        return CONFIGURATION
    try:
        opened = open_store()
    except (sqlite3.Error, RuntimeError, OSError, FencingError) as error:
        print(f"error: cannot read the execution store: {error}", file=err)
        return CONFIGURATION
    if not isinstance(opened, CancelReader):  # pragma: no cover - every reader is one
        print("error: the execution store cannot be read here", file=err)
        return CONFIGURATION
    try:
        execution = opened.get(execution_id)
        accepted = _accepted(opened.events(execution_id=execution_id))
    except (sqlite3.Error, RuntimeError, FencingError) as error:
        print(f"error: reading the execution store failed: {error}", file=err)
        return CONFIGURATION
    finally:
        opened.close()
    if execution is None:
        print(f"error: no execution {execution_id}", file=err)
        return CONFIGURATION
    if accepted is not None:
        print(f"{execution_id}: cancellation already accepted ({accepted})", file=out)
        return OK
    if execution.state.is_terminal:
        print(f"error: {execution_id} has already finished ({execution.state.value})", file=err)
        return CONFIGURATION
    return _submit(layout, execution_id, args.actor, out=out, err=err, now=now(), uid=uid)


def _submit(
    layout: WorkspaceLayout,
    execution_id: str,
    actor: str,
    *,
    out: TextIO,
    err: TextIO,
    now: datetime,
    uid: int | None,
) -> int:
    try:
        inbox = cancel_inbox.open_inbox(layout.coordinator_root, create=True, uid=uid)
    except FileNotFoundError:
        print(f"error: {layout.coordinator_root} does not exist", file=err)
        return CONFIGURATION
    except cancel_inbox.UnsafeInboxError as error:
        print(f"error: {error}", file=err)
        return CONFIGURATION
    try:
        submission = cancel_inbox.submit(inbox, execution_id, actor, now=now, uid=uid)
    except (cancel_inbox.UnsafeInboxError, cancel_inbox.InvalidEntryError, ValueError) as error:
        print(f"error: {error}", file=err)
        return CONFIGURATION
    finally:
        os.close(inbox)
    request = submission.request
    if submission.created:
        print(
            f"{execution_id}: cancellation requested ({request.request_id}); the coordinator "
            "applies it on its next pass",
            file=out,
        )
    else:
        print(
            f"{execution_id}: a request is already pending ({request.request_id}, by "
            f"{request.actor}); this one was not published",
            file=out,
        )
    return OK


def _accepted(events: Sequence[Event]) -> str | None:
    for event in events:
        if (
            event.type is EventType.EXECUTION_CANCEL_REQUESTED
            and event.data.get("decision") == "accepted"
        ):
            return str(event.data.get("request"))
    return None
