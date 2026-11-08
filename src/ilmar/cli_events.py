# SPDX-License-Identifier: Apache-2.0
"""``ilmar events`` and ``ilmar events verify`` (ADR-0024 §6 and §8).

Both are strictly read-only. They open the store with SQLite's read-only mode,
which never migrates, never writes and never creates a file, and they take no
instance lock, so they can run beside a coordinator. Nothing else is planned or
created: no container runtime, agent or secret is needed to read events.

``ilmar events`` exports stored events as JSON lines in ``seq`` order, exactly
as stored, so a consumer can deduplicate by ``(instance, seq)``. Export is
at-least-once: the exporter changes nothing, so a crashed export is re-run
from the consumer's last ``seq``. ``--format text`` is for people.

``ilmar events verify`` folds every entity from its first event and compares
the result with the stored rows (invariant I11). Findings are either
historical (outside event coverage by design, bounded by the baseline) or
integrity failures, never both.

Exit codes:

======  ===================================================================
0       events printed; or ``verify`` found no integrity failure
1       a corrupt event (``events``); an integrity failure (``verify``)
2       no event coverage: the store has no events table yet
3       configuration or usage problem; the store cannot be opened
======  ===================================================================
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, TextIO, runtime_checkable

from ilmar.adapters.registry import event_reader
from ilmar.config import load_config
from ilmar.domain.errors import ConfigError, FencingError
from ilmar.domain.event_history import Finding, Report, StoredState, verify_lines
from ilmar.domain.events import (
    REASON_CODES,
    CorruptEventError,
    Event,
    EventType,
)


@runtime_checkable
class EventReader(Protocol):
    """A store opened read-only (``SQLiteExecutionStore.open_read_only``): it never
    migrates and never writes (ADR-0024 §8)."""

    def event_lines(
        self, *, after_seq: int = 0, limit: int | None = None
    ) -> Sequence[tuple[int, str]]:
        """``(seq, line)`` of the stored events after ``after_seq``, unparsed."""
        ...

    def history_snapshot(self) -> tuple[Sequence[str], StoredState]:
        """Every event line and the rows I11 is checked against, in one snapshot."""
        ...

    def close(self) -> None: ...


OK, CORRUPT, NO_COVERAGE, CONFIGURATION = 0, 1, 2, 3
_INTEGRITY = 1
_BATCH = 500
FAMILIES = frozenset(t.value.split(".")[0] for t in EventType)


def add_parser(commands: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    events = commands.add_parser(
        "events",
        help="print or verify the event log (read-only, needs no instance lock)",
        description="Print stored events as JSON lines in seq order, or with `verify`, "
        "check that they rebuild the stored rows. Read-only: never migrates, writes or "
        "takes the instance lock. Exit codes: 0 ok; 1 corrupt event or integrity failure; "
        "2 no event coverage; 3 configuration, usage or unreadable store.",
    )
    events.add_argument(
        "action",
        nargs="?",
        choices=("verify",),
        help="verify: fold the events and compare with the stored rows",
    )
    which = events.add_mutually_exclusive_group(required=True)
    which.add_argument(
        "--config", metavar="PATH", help="the configuration file; its execution store is read"
    )
    which.add_argument("--db", metavar="PATH", help="an execution store file, read directly")
    events.add_argument(
        "--format",
        choices=("jsonl", "text", "json"),
        help="events: jsonl (default, each event exactly as stored) or text; "
        "verify: text (default) or json",
    )
    listing = events.add_argument_group("filters (events only)")
    listing.add_argument(
        "--after",
        type=_non_negative,
        default=0,
        metavar="SEQ",
        help="only events with a seq greater than SEQ (a consumer's cursor)",
    )
    listing.add_argument(
        "--limit", type=_positive, metavar="N", help="stop after N matching events"
    )
    listing.add_argument(
        "--execution",
        action="append",
        metavar="ID",
        help="only events of this execution (repeatable)",
    )
    listing.add_argument(
        "--type",
        action="append",
        dest="types",
        metavar="TYPE",
        help="an event type such as operation.failed, or a family such as operation (repeatable)",
    )
    listing.add_argument(
        "--reason",
        action="append",
        metavar="CODE",
        help="only events with this reason code (repeatable)",
    )
    listing.add_argument(
        "--follow", action="store_true", help="keep printing new events until interrupted"
    )
    listing.add_argument(
        "--interval",
        type=_positive_float,
        default=1.0,
        metavar="SECONDS",
        help="how often --follow looks for new events (default 1)",
    )
    events.add_argument("--failures-only", action="store_true",
                        help="verify: count historical findings without listing them")  # fmt: skip


def run(args: argparse.Namespace, *, out: TextIO = sys.stdout, err: TextIO = sys.stderr,
        sleep: Callable[[float], None] = time.sleep) -> int:  # fmt: skip
    """Run ``ilmar events`` or ``ilmar events verify`` for parsed ``args``."""
    try:
        _check_combination(args)
        types = _types(args.types) if args.types else None
        reasons = _reasons(args.reason) if args.reason else None
        where, open_store = _store(args)
    except ConfigError as exc:
        print(f"error: {exc}", file=err)
        return CONFIGURATION
    try:
        opened = open_store()
    except (sqlite3.Error, RuntimeError, OSError, FencingError) as exc:
        print(f"error: cannot read the execution store at {where}: {exc}", file=err)
        return CONFIGURATION
    if not isinstance(opened, EventReader):  # pragma: no cover - every reader opener is one
        print(f"error: {where} cannot be read as an event log", file=err)
        return CONFIGURATION
    store = opened
    try:
        if args.action == "verify":
            return _verify(store, where, args, out)
        wanted = Filter(after=args.after, executions=frozenset(args.execution or ()),
                        types=types, reasons=reasons)  # fmt: skip
        return _export(store, wanted, args, out=out, err=err, sleep=sleep)
    except (sqlite3.Error, FencingError) as exc:
        print(f"error: reading {where} failed: {exc}", file=err)
        return CONFIGURATION
    finally:
        store.close()


# -- export -----------------------------------------------------------------------------


@dataclass(frozen=True)
class Filter:
    after: int = 0
    executions: frozenset[str] = frozenset()
    types: frozenset[str] | None = None
    reasons: frozenset[str] | None = None

    def matches(self, event: Event) -> bool:
        if self.executions and event.execution not in self.executions:
            return False
        if self.types is not None and not (
            event.type.value in self.types or event.type.value.split(".")[0] in self.types
        ):
            return False
        return self.reasons is None or event.reason in self.reasons


def _export(store: EventReader, wanted: Filter, args: argparse.Namespace, *, out: TextIO,
            err: TextIO, sleep: Callable[[float], None]) -> int:  # fmt: skip
    cursor, printed = wanted.after, 0
    if not store.event_lines(limit=1):
        print("error: no event coverage: this store has no event log yet. It starts when a "
              "coordinator next claims the store; nothing was changed.", file=err)  # fmt: skip
        return NO_COVERAGE
    try:
        while True:
            batch = store.event_lines(after_seq=cursor, limit=_BATCH)
            for seq, line in batch:
                try:
                    event = _decode(seq, line)
                except CorruptEventError as exc:
                    out.flush()
                    print(f"error: the event at seq {seq} is corrupt ({exc}); events after "
                          "it are not printed. Run `ilmar events verify`.", file=err)  # fmt: skip
                    return CORRUPT
                cursor = seq
                if not wanted.matches(event):
                    continue
                out.write((_text(event) if args.format == "text" else line) + "\n")
                printed += 1
                if args.limit is not None and printed >= args.limit:
                    out.flush()
                    return OK
            if len(batch) == _BATCH:
                continue
            out.flush()
            if not args.follow:
                return OK
            sleep(args.interval)
    except KeyboardInterrupt:  # how --follow ends
        out.flush()
        return OK


def _decode(seq: int, line: str) -> Event:
    event = Event.from_json(line)
    if event.seq != seq:
        raise CorruptEventError(f"the line holds seq {event.seq}")
    return event


def _text(event: Event) -> str:
    subject = event.execution or event.operation or str(event.data.get("ref", "")) or "-"
    details = [f"{key}={event.data[key]}" for key in ("kind", "from", "to", "state", "status",
               "observed", "duration_ms") if key in event.data]  # fmt: skip
    if event.operation and event.execution:
        details.insert(0, f"operation={event.operation}")
    if "ref" in event.data and event.execution:
        details.insert(0, f"resource={event.data['ref']}")
    if event.reason:
        details.append(f"reason={event.reason}")
    if event.flags:
        details.append("flags=" + ",".join(sorted(event.flags)))
    return f"{event.seq:>7}  {event.at}  {event.type.value:<26} {subject}  {' '.join(details)}"


# -- verify -----------------------------------------------------------------------------


def _verify(store: EventReader, where: str, args: argparse.Namespace, out: TextIO) -> int:
    lines, stored = store.history_snapshot()
    report = verify_lines(list(lines), stored)
    if args.format == "json":
        out.write(json.dumps(_report_json(where, report, len(lines)), sort_keys=True) + "\n")
    else:
        out.write("".join(line + "\n" for line in _report_text(where, report, len(lines),
                                                                 args.failures_only)))  # fmt: skip
    return report.exit_code


def _report_json(where: str, report: Report, events: int) -> dict[str, object]:
    def rows(findings: Iterable[Finding]) -> list[dict[str, str]]:
        return [{"check": f.check, "subject": f.subject, "detail": f.detail} for f in findings]

    return {
        "store": where,
        "covered": report.covered,
        "events": events,
        "exit_code": report.exit_code,
        "result": _result(report),
        "historical": rows(report.historical),
        "failures": rows(report.failures),
    }


def _report_text(where: str, report: Report, events: int, failures_only: bool) -> list[str]:
    lines = [f"store: {where}"]
    if not report.covered:
        return [*lines, "result: no event coverage (the store has no event log yet; nothing "
                "was changed)"]  # fmt: skip
    lines.append(f"events: {events}")
    lines.append(f"historical findings (information): {len(report.historical)}")
    if not failures_only:
        lines += [f"  {f.subject}: {f.detail} [{f.check}]" for f in report.historical]
    lines.append(f"integrity failures: {len(report.failures)}")
    lines += [f"  FAIL {f.check} {f.subject}: {f.detail}" for f in report.failures]
    lines.append(f"result: {_result(report)}")
    return lines


def _result(report: Report) -> str:
    if not report.covered:
        return "no-coverage"
    return "integrity-failure" if report.exit_code == _INTEGRITY else "ok"


# -- arguments --------------------------------------------------------------------------


def _check_combination(args: argparse.Namespace) -> None:
    if args.action == "verify":
        listing = [name for name, given in (
            ("--after", args.after != 0), ("--limit", args.limit is not None),
            ("--execution", bool(args.execution)), ("--type", bool(args.types)),
            ("--reason", bool(args.reason)), ("--follow", args.follow),
        ) if given]  # fmt: skip
        if listing:
            raise ConfigError(f"verify always checks every event; {', '.join(listing)} "
                              "only filters the listing")  # fmt: skip
        if args.format == "jsonl":
            raise ConfigError("verify prints text or json")
        args.format = args.format or "text"
    else:
        if args.format == "json":
            raise ConfigError("events prints jsonl or text")
        if args.failures_only:
            raise ConfigError("--failures-only is for verify")
        args.format = args.format or "jsonl"


def _store(args: argparse.Namespace) -> tuple[str, Callable[[], object]]:
    if args.db is not None:
        path = Path(args.db)
        if not path.is_file():
            raise ConfigError(f"no execution store at {path}")
        from ilmar.adapters.sqlite import SQLiteExecutionStore

        return str(path), lambda: SQLiteExecutionStore.open_read_only(path)
    open_store = event_reader(load_config(Path(args.config)))
    return f"the execution store of {args.config}", open_store


def _types(values: Sequence[str]) -> frozenset[str]:
    known = {t.value for t in EventType} | FAMILIES
    if unknown := sorted(set(values) - known):
        raise ConfigError(f"unknown event type or family: {', '.join(unknown)}")
    return frozenset(values)


def _reasons(values: Sequence[str]) -> frozenset[str]:
    if unknown := sorted(set(values) - set(REASON_CODES)):
        raise ConfigError(f"unknown reason code: {', '.join(unknown)}")
    return frozenset(values)


def _non_negative(text: str) -> int:
    value = int(text)
    if value < 0:
        raise argparse.ArgumentTypeError("must be 0 or more")
    return value


def _positive(text: str) -> int:
    value = int(text)
    if value < 1:
        raise argparse.ArgumentTypeError("must be 1 or more")
    return value


def _positive_float(text: str) -> float:
    value = float(text)
    if not value > 0:
        raise argparse.ArgumentTypeError("must be more than 0")
    return value
