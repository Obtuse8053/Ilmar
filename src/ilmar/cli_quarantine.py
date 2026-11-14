# SPDX-License-Identifier: Apache-2.0
"""``ilmar quarantine list|resolve`` (ADR-0042, #59): a person resolves a quarantine.

``list`` reads. ``resolve`` checks what it can now, then records one fenced,
append-only ``resource.resolved`` event as the person named by ``--as``:

* a sandbox: no part of it (container, sidecar, network) may remain, and a
  part with another owner is reported, never touched. This command removes
  nothing;
* a directory: it is absent, or still has its recorded identity;
* anything else is refused.

The row stays quarantined, and automation still never touches it. Once every
quarantined resource of an execution is resolved, the next pass finishes the
execution as usual. Never run by the coordinator or any schedule.

Exit codes: 0 done; 1 refused (something remains, or it does not match);
3 configuration problem, bad input or the instance in use.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TextIO, cast

from ilmar.adapters.registry import runtime_reader, store_writer
from ilmar.config import load_config
from ilmar.core.ownership import Ownership
from ilmar.core.quarantine import resolved_keys
from ilmar.core.safe_fs import directory_identity
from ilmar.domain.errors import (
    ConfigError,
    FencingError,
    LedgerIntegrityError,
    ProviderError,
)
from ilmar.domain.events import (
    RESOLUTION_ACTIONS,
    Actor,
    ActorKind,
    Event,
    EventContext,
    EventType,
)
from ilmar.domain.ledger import Commit, Resource, ResourceKind, ResourceStatus
from ilmar.ports.execution_store import ExecutionStore

OK, REFUSED, CONFIGURATION = 0, 1, 3
_DIRECTORIES = frozenset({ResourceKind.WORKSPACE, ResourceKind.VERIFICATION_CHECKOUT})


def add_parser(commands: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    parser = commands.add_parser("quarantine", help="list or resolve quarantined resources")
    actions = parser.add_subparsers(dest="quarantine_command", required=True)
    listing = actions.add_parser("list", help="every quarantined resource")
    resolve = actions.add_parser("resolve", help="record a person's verified resolution")
    for action in (listing, resolve):
        action.add_argument("--config", required=True, help="the configuration file")
    resolve.add_argument("--execution", required=True)
    resolve.add_argument("--resource", required=True, help="the resource key, from `list`")
    resolve.add_argument(
        "--as",
        dest="actor",
        required=True,
        metavar="NAME",
        help="who resolves (a short lower-case label)",
    )
    resolve.add_argument("--action", required=True, choices=RESOLUTION_ACTIONS,
                         help="what you did before resolving")  # fmt: skip


def run(
    args: argparse.Namespace,
    *,
    out: TextIO = sys.stdout,
    err: TextIO = sys.stderr,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
    runtime: Any = None,
) -> int:
    """``runtime`` replaces the configured container runtime (tests)."""
    try:
        config = load_config(Path(args.config))
        layout, open_store = store_writer(config)
    except ConfigError as exc:
        print(f"error: {exc}", file=err)
        return CONFIGURATION
    config.state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    store = cast(ExecutionStore, open_store())
    try:
        if args.quarantine_command == "list":
            return _list(store, out)
        try:
            actor = Actor(ActorKind.HUMAN, args.actor)
        except ValueError as exc:
            print(f"error: --as: {exc}", file=err)
            return CONFIGURATION
        row = store.resource(args.resource)
        if row is None or row.status is not ResourceStatus.QUARANTINED:
            print(f"error: {args.resource} is not a quarantined resource", file=err)
            return CONFIGURATION
        if row.execution_id != args.execution:
            print(f"error: {args.resource} belongs to execution {row.execution_id}", file=err)
            return CONFIGURATION
        if row.key in resolved_keys(store, row.execution_id):
            print(f"already resolved: {row.key}", file=out)
            return OK
        try:
            observed = _verify(row, config, runtime)
        except ProviderError as exc:
            print(f"error: cannot check the runtime, so nothing was recorded: {exc}", file=err)
            return CONFIGURATION
        if isinstance(observed, list):
            for line in observed:
                print(f"remains: {line}", file=err)
            print("refused: nothing was recorded", file=err)
            return REFUSED
        try:
            ownership = Ownership.take(layout=layout, store=store,
                                       instance_id=config.instance_id, now=now())  # fmt: skip
        except (FencingError, OSError) as exc:
            print(f"error: cannot own this instance (is a coordinator running?): {exc}", file=err)
            return CONFIGURATION
        with ownership:
            at = now()
            try:
                event = Event.create(
                    EventType.RESOURCE_RESOLVED, at=at, instance=config.instance_id,
                    epoch=ownership.fence.epoch, actor=actor, execution=row.execution_id,
                    attempt=row.attempt, reason="quarantine-resolved",
                    data={"resource": row.key, "observed": observed, "action": args.action},
                )  # fmt: skip
                if event.flags:
                    raise LedgerIntegrityError("the resource key is not acceptable in the log")
                context = EventContext(at=at, instance=config.instance_id,
                                       epoch=ownership.fence.epoch, actor=actor)  # fmt: skip
                store.commit(ownership.fence, Commit(extra_events=(event,)), context=context)
            except (LedgerIntegrityError, ValueError) as exc:
                print(f"error: {exc}", file=err)
                return CONFIGURATION
        print(f"resolved: {row.key} ({observed}); it stays quarantined", file=out)
        return OK
    finally:
        close = getattr(store, "close", None)
        if callable(close):
            close()


def _verify(row: Resource, config: Any, runtime: Any) -> str | list[str]:
    """What was observed, or what remains (a refusal)."""
    if row.kind is ResourceKind.CONTAINER:
        engine: Any = runtime if runtime is not None else runtime_reader(config)()
        parts = engine.sandbox_parts(row.name)
        remaining = []
        for part in parts:
            ours = part.owner is not None and part.owner.execution_id == row.execution_id
            whose = "this execution's" if ours else "ANOTHER OWNER'S (never touched)"
            remaining.append(f"{part.kind} {part.name}, {whose}")
        return remaining or "absent"
    if row.kind in _DIRECTORIES:
        path = Path(row.name)
        if not path.exists() and not path.is_symlink():
            return "absent"
        try:
            found = directory_identity(path, root=path.parent)
        except OSError as exc:
            return [f"{path} cannot be read: {exc.strerror or type(exc).__name__}"]
        if found is not None and row.identity == f"{found.device}:{found.inode}":
            return "identity-matches"
        return [f"{path} exists with another identity than recorded"]
    return [f"a {row.kind.value} cannot be checked from here (ADR-0042 §2)"]


def _list(store: ExecutionStore, out: TextIO) -> int:
    rows = store.resources(statuses={ResourceStatus.QUARANTINED})
    if not rows:
        print("no quarantined resources", file=out)
    for row in rows:
        done = row.key in resolved_keys(store, row.execution_id)
        print(f"{row.key} execution={row.execution_id} kind={row.kind.value} "
              f"{'resolved' if done else 'unresolved'}", file=out)  # fmt: skip
    return OK
