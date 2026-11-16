# SPDX-License-Identifier: Apache-2.0
"""``ilmar gate record|revoke|show`` (ADR-0030 §2, ADR-0040, #91).

A gate record is a person's statement that a mode's gate passed, bound to its
evidence. ``record`` and ``revoke`` take the instance lock and write one
fenced commit as the person named by ``--as``; the store refuses any other
writer. They open only the execution store: no tracker, forge, container
runtime or secret. ``show`` only reads.

``record`` checks the form of every field, and what it can on the host:

* the stream fixture is read from ``--fixture`` (a path inside this checkout,
  under ``tests/fixtures/``): it must say ``"provenance": "recorded"`` and be
  for the same ``--agent-version``; its SHA-256 is recorded;
* ``--repository-id`` must equal ``[autonomy] repository_id`` when the
  configuration sets one.

It does **not** verify that the live run passed, that Gate 1 passed, or what
the evidence PR and ``--evidence-commit`` contain. The person recording the
gate verifies those by hand, before recording; the record names them.

Exit codes: 0 done; 3 configuration problem, refused input or the instance in
use.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import sys
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TextIO, cast

from ilmar.adapters.registry import event_reader, store_writer
from ilmar.config import IlmarConfig, load_config
from ilmar.core.ownership import Ownership
from ilmar.domain.errors import ConfigError, FencingError, LedgerIntegrityError
from ilmar.domain.events import Actor, ActorKind, EventContext
from ilmar.domain.gates import (
    GATED_MODES,
    TRIAL_MAX_DURATION,
    GateChange,
    GateEvidence,
    GateRecorded,
    GateRevoked,
    TrialAuthorized,
    TrialRevoked,
    TrialTerms,
)
from ilmar.domain.ledger import Commit
from ilmar.ports.execution_store import ExecutionStore

OK, CONFIGURATION = 0, 3
_FIXTURE_LIMIT = 4 * 1024 * 1024


def add_parser(commands: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    gate = commands.add_parser("gate", help="record, revoke or show gate records (ADR-0030)")
    actions = gate.add_subparsers(dest="gate_command", required=True)
    record = actions.add_parser("record", help="record that a mode's gate passed")
    revoke = actions.add_parser("revoke", help="revoke a mode's gate record")
    show = actions.add_parser("show", help="list every gate record and trial authorization")
    trial = actions.add_parser(
        "trial", help="authorize a bounded, expiring loop trial for the Gate 1 run (ADR-0041)"
    )
    end = actions.add_parser("end-trial", help="revoke the trial authorization")
    for parser in (record, revoke, show, trial, end):
        parser.add_argument("--config", required=True, help="the configuration file")
    for parser in (trial, end):
        parser.add_argument(
            "--as",
            dest="actor",
            required=True,
            metavar="NAME",
            help="who, recorded with the event (a short lower-case label)",
        )
    trial.add_argument(
        "--repository-id",
        required=True,
        type=int,
        help="the disposable repository's immutable GitHub ID",
    )
    trial.add_argument("--repository", required=True, help="its owner/name today")
    trial.add_argument("--agent-version", required=True, help="the exact x.y.z to run")
    trial.add_argument("--max-executions", required=True, type=int, help="at most 20")
    trial.add_argument(
        "--max-spend-usd",
        required=True,
        type=float,
        help="reservations and charges under the trial, in USD",
    )
    trial.add_argument("--hours", required=True, type=float, help="until expiry; at most 72")
    trial.add_argument("--purpose", required=True, help="the Gate 1 issue or comment URL")
    trial.add_argument("--disposable", action="store_true", required=True,
                       help="you confirm the repository is dedicated and disposable, with "
                       "no credentials in its content, history or workflows")  # fmt: skip
    for parser in (record, revoke):
        parser.add_argument(
            "--as",
            dest="actor",
            required=True,
            metavar="NAME",
            help="who, recorded with the event (a short lower-case label)",
        )
        parser.add_argument("--mode", required=True, choices=GATED_MODES)
    record.add_argument(
        "--repository-id",
        required=True,
        type=int,
        help="the approved target's immutable GitHub repository ID",
    )
    record.add_argument("--repository", required=True, help="its owner/name today")
    record.add_argument(
        "--agent-version", required=True, help="the exact x.y.z the evidence is for"
    )
    record.add_argument("--evidence", required=True, help="the merged PR with the gate evidence")
    record.add_argument("--evidence-commit", required=True,
                        help="the full commit SHA you reviewed that evidence at")  # fmt: skip
    record.add_argument("--live-test", required=True, help="the passed pinned-version live run")
    record.add_argument(
        "--fixture",
        required=True,
        type=Path,
        help="the reviewed recorded stream fixture, under tests/fixtures/",
    )
    record.add_argument("--gate1", required=True, help="the Gate 1 record (#93)")  # fmt: skip


def run(
    args: argparse.Namespace,
    *,
    out: TextIO = sys.stdout,
    err: TextIO = sys.stderr,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
    checkout: Path | None = None,
) -> int:
    """``checkout`` is the directory ``--fixture`` is relative to (the current one)."""
    try:
        config = load_config(Path(args.config))
    except ConfigError as exc:
        print(f"error: {exc}", file=err)
        return CONFIGURATION
    if args.gate_command == "show":
        return _show(config, out=out, err=err)
    try:
        actor = Actor(ActorKind.HUMAN, args.actor)
    except ValueError as exc:
        print(f"error: --as: {exc}", file=err)
        return CONFIGURATION
    at = now()
    gate: GateChange
    if args.gate_command == "trial":
        try:
            terms = _terms(args, config, at)
        except ValueError as exc:
            print(f"error: {exc}", file=err)
            return CONFIGURATION
        gate = TrialAuthorized(terms, at)
    elif args.gate_command == "end-trial":
        gate = TrialRevoked(at)
    elif args.gate_command == "record":
        try:
            evidence = _evidence(args, root=checkout or Path.cwd())
        except ValueError as exc:
            print(f"error: {exc}", file=err)
            return CONFIGURATION
        expected = config.autonomy.repository_id
        if expected is not None and expected != evidence.repository_id:
            print(f"error: --repository-id {evidence.repository_id} is not the configured "
                  f"[autonomy] repository_id {expected}", file=err)  # fmt: skip
            return CONFIGURATION
        gate = GateRecorded(evidence, at)
    else:
        gate = GateRevoked(args.mode, at)
    try:
        layout, open_store = store_writer(config)
    except ConfigError as exc:
        print(f"error: {exc}", file=err)
        return CONFIGURATION
    config.state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    store = cast(ExecutionStore, open_store())
    try:
        try:
            ownership = Ownership.take(
                layout=layout, store=store, instance_id=config.instance_id, now=at
            )
        except (FencingError, OSError) as exc:
            print(f"error: cannot own this instance (is a coordinator running?): {exc}", file=err)
            return CONFIGURATION
        with ownership:
            context = EventContext(
                at=at, instance=config.instance_id, epoch=ownership.fence.epoch, actor=actor
            )
            try:
                store.commit(ownership.fence, Commit(gate=gate), context=context)
            except LedgerIntegrityError as exc:
                print(f"error: {exc}", file=err)
                return CONFIGURATION
    finally:
        close = getattr(store, "close", None)
        if callable(close):
            close()
    if isinstance(gate, TrialAuthorized):
        t = gate.terms
        print(f"authorized: trial for repository {t.repository_id} ({t.repository}), agent "
              f"{t.agent_version}, at most {t.max_executions} execution(s) and "
              f"{t.max_spend_micros / 1_000_000:.2f} USD, until {t.expires_at.isoformat()}, "
              f"by {args.actor}. Only `ilmar run --loop --trial` accepts it; it is not a gate "
              "record and authorizes nothing else.", file=out)  # fmt: skip
    elif isinstance(gate, TrialRevoked):
        print("revoked: the trial authorization; the trial loop stops at the next check",
              file=out)  # fmt: skip
    elif isinstance(gate, GateRecorded):
        print(f"recorded: the {args.mode} gate, for repository {gate.evidence.repository_id} "
              f"and agent {gate.evidence.agent_version}, by {args.actor}. Ilmar checked the "
              "form and the fixture only; the live run and Gate 1 are as you verified them.",
              file=out)  # fmt: skip
    else:
        print(f"revoked: the {args.mode} gate record; the mode is off at the next check",
              file=out)  # fmt: skip
    return OK


def _evidence(args: argparse.Namespace, *, root: Path) -> GateEvidence:
    """The record's evidence, with the fixture checked and digested here."""
    relative = Path(args.fixture)
    if relative.is_absolute():
        raise ValueError("--fixture must be a path inside this checkout, under tests/fixtures/")
    path = (root / relative).resolve()
    if not path.is_relative_to((root / "tests" / "fixtures").resolve()):
        raise ValueError("--fixture must be under tests/fixtures/ in this checkout")
    try:
        content = path.read_bytes()[: _FIXTURE_LIMIT + 1]
    except OSError as exc:
        raise ValueError(f"--fixture cannot be read: {exc.strerror}") from None
    if len(content) > _FIXTURE_LIMIT:
        raise ValueError("--fixture is too large")
    try:
        fixture = json.loads(content)
    except ValueError:
        raise ValueError("--fixture is not JSON") from None
    if not isinstance(fixture, dict) or fixture.get("provenance") != "recorded":
        raise ValueError('--fixture must be a recorded fixture ("provenance": "recorded")')
    if fixture.get("claude_code_version") != args.agent_version:
        raise ValueError("--fixture is for another agent version than --agent-version")
    return GateEvidence(
        mode=args.mode, repository_id=args.repository_id, repository=args.repository,
        agent_version=args.agent_version, evidence=args.evidence,
        evidence_commit=args.evidence_commit, live_test=args.live_test,
        fixture=relative.as_posix(), fixture_sha256=hashlib.sha256(content).hexdigest(),
        gate1=args.gate1,
    )  # fmt: skip


def _terms(args: argparse.Namespace, config: IlmarConfig, at: datetime) -> TrialTerms:
    """The trial's terms, checked against the configuration (ADR-0041)."""
    if config.autonomy.mode != "supervised":
        raise ValueError('a trial needs [autonomy] mode = "supervised" (all of Mode 1\'s rules)')
    if config.autonomy.repository_id != args.repository_id:
        raise ValueError(f"--repository-id {args.repository_id} is not the configured "
                         f"[autonomy] repository_id {config.autonomy.repository_id}")  # fmt: skip
    if config.budget is None:
        raise ValueError("a trial needs a [budget]")
    if not (math.isfinite(args.max_spend_usd) and math.isfinite(args.hours)):
        raise ValueError("--max-spend-usd and --hours must be finite")
    spend = math.ceil(args.max_spend_usd * 1_000_000)
    if spend > config.budget.provider_cap_attested_micros:
        raise ValueError("--max-spend-usd may not exceed the attested provider cap")
    if spend < config.budget.limits.reserve_micros:
        raise ValueError("--max-spend-usd is below one attempt's reservation")
    if not 0 < args.hours <= TRIAL_MAX_DURATION.total_seconds() / 3600:
        raise ValueError("--hours must be more than 0 and at most 72")
    return TrialTerms(
        repository_id=args.repository_id, repository=args.repository,
        agent_version=args.agent_version, max_executions=args.max_executions,
        max_spend_micros=spend, starts_at=at, expires_at=at + timedelta(hours=args.hours),
        purpose=args.purpose,
    )  # fmt: skip


def _show(config: IlmarConfig, *, out: TextIO, err: TextIO) -> int:
    try:
        opener = event_reader(config)
    except ConfigError as exc:
        print(f"error: {exc}", file=err)
        return CONFIGURATION
    try:
        store = opener()
    except OSError:
        print("no execution store yet: no gate records", file=out)
        return OK
    try:
        records = store.gate_records()  # type: ignore[attr-defined]
        trials = store.trial_records()  # type: ignore[attr-defined]
    finally:
        store.close()  # type: ignore[attr-defined]
    for t in trials:
        state = (f"revoked {t.revoked_at.isoformat()} by {t.revoked_by}"
                 if t.revoked_at is not None else "unrevoked")  # fmt: skip
        print(f"trial #{t.seq} {state}: repository {t.terms.repository_id} "
              f"({t.terms.repository}), agent {t.terms.agent_version}, "
              f"{t.terms.max_executions} execution(s), {t.terms.max_spend_micros} micro-USD, "
              f"{t.terms.starts_at.isoformat()} to {t.terms.expires_at.isoformat()}, "
              f"by {t.authorized_by}; for {t.terms.purpose}", file=out)  # fmt: skip
    if not records:
        print("no gate records", file=out)
    for r in records:
        state = "active" if r.active else f"revoked {r.revoked_at.isoformat()} by {r.revoked_by}"
        e = r.evidence
        print(f"#{r.seq} {e.mode} {state}: repository {e.repository_id} ({e.repository}), "
              f"agent {e.agent_version}, recorded {r.recorded_at.isoformat()} by "
              f"{r.recorded_by}; evidence {e.evidence} at {e.evidence_commit}; "
              f"live test {e.live_test}; "
              f"fixture {e.fixture} sha256:{e.fixture_sha256}; gate 1 {e.gate1}",
              file=out)  # fmt: skip
    return OK
