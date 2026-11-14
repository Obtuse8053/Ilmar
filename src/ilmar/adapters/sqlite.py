# SPDX-License-Identifier: Apache-2.0
"""Durable ``ExecutionStore`` backed by SQLite (standard-library ``sqlite3``).

Design notes (ADR-0005):

* One row per execution plus an append-only ``transitions`` table.
* A partial unique index enforces at most one non-terminal execution per task,
  so duplicate work is prevented by the database even if two coordinators race.
* Updates use the execution ``version`` for optimistic concurrency.
* Timestamps are stored as fixed-width UTC ISO-8601 strings so that text order
  equals time order.
* The schema version lives in ``PRAGMA user_version``. Version 2 (#9) adds
  the candidate revisions and an ``evidence`` table. Triggers make
  ``transitions`` and ``evidence`` append-only, and each evidence row carries
  the digest of its record, checked when read.
* Version 3 (#11a, ADR-0020) adds the owner (fencing epoch), the operation
  journal, the resource ledger and an append-only log of their changes.
  Triggers keep the epoch monotonic, final operations and released or
  quarantined resources immutable, and identities unchangeable, even against
  writes that bypass this class.
* Version 4 (#17, ADR-0024) adds the append-only ``events`` table. Every write
  appends its events in its own transaction (``ilmar.ports.event_log``).

Opening and migrating are separate (ADR-0024 §8):

* Opening for writing creates a brand-new, empty database at version 3, but
  never migrates an existing one.
* Every migration of an existing database, the events table and its baseline
  included, runs in :meth:`SQLiteExecutionStore.claim_instance`, which is
  called only under the instance lock, in the same transaction as the claim.
  A crash anywhere in it leaves the file as it was.
* :meth:`SQLiteExecutionStore.open_read_only` never migrates and never writes.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Collection, Iterator, Mapping, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from types import TracebackType
from typing import Self

from ilmar.domain.budget import (
    BudgetTotals,
    Charge,
    ChargeKind,
    Suspension,
    SuspensionReason,
    utc_day,
    utc_month,
)
from ilmar.domain.errors import (
    ConcurrencyConflictError,
    DuplicateExecutionError,
    EvidenceIntegrityError,
    ExecutionNotFoundError,
    FencingError,
    LedgerIntegrityError,
)
from ilmar.domain.event_history import StoredState
from ilmar.domain.events import CorruptEventError, Event, EventContext
from ilmar.domain.execution import Execution, ExecutionState, Transition
from ilmar.domain.gates import GateEvidence, GateRecord, GateRecorded, active_gate
from ilmar.domain.ledger import (
    Commit,
    Fence,
    Operation,
    OperationKind,
    OperationStatus,
    Resource,
    ResourceKind,
    ResourceStatus,
)
from ilmar.domain.tasks import TaskRef
from ilmar.domain.verification import EvidenceRecord
from ilmar.ports import event_log
from ilmar.ports.execution_store import check_apply, check_commit, check_create, check_fence

SCHEMA_VERSION = 9
"""The version this code writes. A database is at it once it has been claimed."""
NEW_DATABASE_VERSION = 3
"""A brand-new database is created at this version: no events yet, since the
coverage marker needs its owner (ADR-0024 §8)."""
COVERED_VERSION = 4
"""The first version with event coverage. Later migrations need no baseline."""

SCHEMA_V1 = """
CREATE TABLE executions (
    id                 TEXT PRIMARY KEY,
    task_provider      TEXT NOT NULL,
    task_key           TEXT NOT NULL,
    state              TEXT NOT NULL,
    is_terminal        INTEGER NOT NULL CHECK (is_terminal IN (0, 1)),
    attempt            INTEGER NOT NULL CHECK (attempt >= 1),
    version            INTEGER NOT NULL CHECK (version >= 1),
    created_at         TEXT NOT NULL,
    updated_at         TEXT NOT NULL,
    branch             TEXT,
    change_request_url TEXT,
    last_error         TEXT
) STRICT;

CREATE UNIQUE INDEX executions_one_active_per_task
    ON executions (task_provider, task_key) WHERE is_terminal = 0;

CREATE INDEX executions_by_state ON executions (state, created_at, id);

CREATE TABLE transitions (
    seq          INTEGER PRIMARY KEY AUTOINCREMENT,
    execution_id TEXT NOT NULL REFERENCES executions (id),
    from_state   TEXT,
    to_state     TEXT NOT NULL,
    at           TEXT NOT NULL,
    reason       TEXT NOT NULL
) STRICT;

CREATE INDEX transitions_by_execution ON transitions (execution_id, seq);
"""

# Each entry upgrades the schema from its key to key + 1.
MIGRATIONS: dict[int, str] = {
    1: """
ALTER TABLE executions ADD COLUMN base_revision TEXT;
ALTER TABLE executions ADD COLUMN candidate_revision TEXT;

CREATE TABLE evidence (
    seq          INTEGER PRIMARY KEY AUTOINCREMENT,
    execution_id TEXT NOT NULL REFERENCES executions (id),
    attempt      INTEGER NOT NULL CHECK (attempt >= 1),
    recorded_at  TEXT NOT NULL,
    digest       TEXT NOT NULL,
    record       TEXT NOT NULL
) STRICT;

CREATE INDEX evidence_by_execution ON evidence (execution_id, seq);

CREATE TRIGGER evidence_is_append_only_update BEFORE UPDATE ON evidence
BEGIN SELECT RAISE(ABORT, 'evidence is append-only'); END;
CREATE TRIGGER evidence_is_append_only_delete BEFORE DELETE ON evidence
BEGIN SELECT RAISE(ABORT, 'evidence is append-only'); END;
CREATE TRIGGER transitions_are_append_only_update BEFORE UPDATE ON transitions
BEGIN SELECT RAISE(ABORT, 'history is append-only'); END;
CREATE TRIGGER transitions_are_append_only_delete BEFORE DELETE ON transitions
BEGIN SELECT RAISE(ABORT, 'history is append-only'); END;
""",
    2: """
CREATE TABLE owner (
    id          INTEGER PRIMARY KEY CHECK (id = 1),
    instance_id TEXT NOT NULL,
    epoch       INTEGER NOT NULL CHECK (epoch >= 1),
    claimed_at  TEXT NOT NULL
) STRICT;

CREATE TABLE owner_history (
    seq         INTEGER PRIMARY KEY AUTOINCREMENT,
    instance_id TEXT NOT NULL,
    epoch       INTEGER NOT NULL,
    claimed_at  TEXT NOT NULL
) STRICT;

CREATE TABLE operations (
    key          TEXT PRIMARY KEY,
    execution_id TEXT NOT NULL REFERENCES executions (id),
    attempt      INTEGER NOT NULL CHECK (attempt >= 1),
    kind         TEXT NOT NULL,
    status       TEXT NOT NULL
                 CHECK (status IN ('intended', 'succeeded', 'failed', 'ambiguous')),
    epoch        INTEGER NOT NULL CHECK (epoch >= 1),
    version      INTEGER NOT NULL CHECK (version >= 1),
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL,
    intent       TEXT NOT NULL,
    result       TEXT NOT NULL,
    note         TEXT NOT NULL
) STRICT;

CREATE INDEX operations_by_execution ON operations (execution_id, created_at);
CREATE INDEX operations_unfinished ON operations (created_at)
    WHERE status IN ('intended', 'ambiguous');

CREATE TABLE resources (
    key          TEXT PRIMARY KEY,
    execution_id TEXT NOT NULL REFERENCES executions (id),
    attempt      INTEGER NOT NULL CHECK (attempt >= 1),
    kind         TEXT NOT NULL,
    name         TEXT NOT NULL,
    owner_token  TEXT NOT NULL,
    status       TEXT NOT NULL CHECK (
                 status IN ('intended', 'active', 'releasing', 'released', 'quarantined')),
    version      INTEGER NOT NULL CHECK (version >= 1),
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL,
    identity     TEXT,
    note         TEXT NOT NULL
) STRICT;

CREATE INDEX resources_by_execution ON resources (execution_id, created_at);
CREATE INDEX resources_by_status ON resources (status, created_at);

CREATE TABLE ledger_events (
    seq     INTEGER PRIMARY KEY AUTOINCREMENT,
    subject TEXT NOT NULL CHECK (subject IN ('operation', 'resource')),
    key     TEXT NOT NULL,
    status  TEXT NOT NULL,
    version INTEGER NOT NULL,
    epoch   INTEGER NOT NULL,
    at      TEXT NOT NULL,
    note    TEXT NOT NULL
) STRICT;

CREATE TRIGGER owner_cannot_be_removed BEFORE DELETE ON owner
BEGIN SELECT RAISE(ABORT, 'the owner record cannot be removed'); END;
CREATE TRIGGER owner_epoch_only_grows BEFORE UPDATE ON owner WHEN NEW.epoch <= OLD.epoch
BEGIN SELECT RAISE(ABORT, 'the fencing epoch only grows'); END;
CREATE TRIGGER owner_history_append_only_update BEFORE UPDATE ON owner_history
BEGIN SELECT RAISE(ABORT, 'owner history is append-only'); END;
CREATE TRIGGER owner_history_append_only_delete BEFORE DELETE ON owner_history
BEGIN SELECT RAISE(ABORT, 'owner history is append-only'); END;
CREATE TRIGGER ledger_events_append_only_update BEFORE UPDATE ON ledger_events
BEGIN SELECT RAISE(ABORT, 'the ledger log is append-only'); END;
CREATE TRIGGER ledger_events_append_only_delete BEFORE DELETE ON ledger_events
BEGIN SELECT RAISE(ABORT, 'the ledger log is append-only'); END;

CREATE TRIGGER operations_cannot_be_removed BEFORE DELETE ON operations
BEGIN SELECT RAISE(ABORT, 'operations cannot be removed'); END;
CREATE TRIGGER operations_final_are_immutable BEFORE UPDATE ON operations
WHEN OLD.status IN ('succeeded', 'failed')
BEGIN SELECT RAISE(ABORT, 'a final operation cannot change'); END;
CREATE TRIGGER operations_identity_is_immutable BEFORE UPDATE ON operations
WHEN NEW.key IS NOT OLD.key OR NEW.kind IS NOT OLD.kind
  OR NEW.execution_id IS NOT OLD.execution_id OR NEW.attempt IS NOT OLD.attempt
  OR NEW.intent IS NOT OLD.intent OR NEW.created_at IS NOT OLD.created_at
  OR NEW.version <> OLD.version + 1
BEGIN SELECT RAISE(ABORT, 'an operation keeps its identity and intent'); END;

CREATE TRIGGER resources_cannot_be_removed BEFORE DELETE ON resources
BEGIN SELECT RAISE(ABORT, 'resources cannot be removed from the ledger'); END;
CREATE TRIGGER resources_final_are_immutable BEFORE UPDATE ON resources
WHEN OLD.status IN ('released', 'quarantined')
BEGIN SELECT RAISE(ABORT, 'a released or quarantined resource cannot change'); END;
CREATE TRIGGER resources_identity_is_immutable BEFORE UPDATE ON resources
WHEN NEW.key IS NOT OLD.key OR NEW.kind IS NOT OLD.kind OR NEW.name IS NOT OLD.name
  OR NEW.execution_id IS NOT OLD.execution_id OR NEW.attempt IS NOT OLD.attempt
  OR NEW.owner_token IS NOT OLD.owner_token OR NEW.created_at IS NOT OLD.created_at
  OR (OLD.identity IS NOT NULL AND NEW.identity IS NOT OLD.identity)
  OR NEW.version <> OLD.version + 1
BEGIN SELECT RAISE(ABORT, 'a resource keeps its owner and identity'); END;
""",
    # ADR-0024: the event log. The baseline is written by claim_instance in the
    # same transaction as this script.
    3: """
CREATE TABLE events (
    seq          INTEGER PRIMARY KEY AUTOINCREMENT CHECK (seq >= 1),
    execution_id TEXT,
    type         TEXT NOT NULL,
    line         TEXT NOT NULL
) STRICT;

CREATE INDEX events_by_execution ON events (execution_id, seq);

CREATE TRIGGER events_are_append_only_update BEFORE UPDATE ON events
BEGIN SELECT RAISE(ABORT, 'events are append-only'); END;
CREATE TRIGGER events_are_append_only_delete BEFORE DELETE ON events
BEGIN SELECT RAISE(ABORT, 'events are append-only'); END;
""",
    # ADR-0026 §5: an instance-scoped operation names no execution. Only the
    # closed set of instance-scoped kinds may leave execution_id empty. SQLite
    # cannot relax NOT NULL in place, so the table is rebuilt with its rows,
    # indexes and triggers unchanged. Its rows are not changed, so no event.
    4: """
DROP TRIGGER operations_cannot_be_removed;
DROP TRIGGER operations_final_are_immutable;
DROP TRIGGER operations_identity_is_immutable;
DROP INDEX operations_by_execution;
DROP INDEX operations_unfinished;
ALTER TABLE operations RENAME TO operations_before;

CREATE TABLE operations (
    key          TEXT PRIMARY KEY,
    execution_id TEXT REFERENCES executions (id),
    attempt      INTEGER NOT NULL CHECK (attempt >= 1),
    kind         TEXT NOT NULL,
    status       TEXT NOT NULL
                 CHECK (status IN ('intended', 'succeeded', 'failed', 'ambiguous')),
    epoch        INTEGER NOT NULL CHECK (epoch >= 1),
    version      INTEGER NOT NULL CHECK (version >= 1),
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL,
    intent       TEXT NOT NULL,
    result       TEXT NOT NULL,
    note         TEXT NOT NULL,
    CHECK (execution_id IS NOT NULL OR (kind = 'release-orphan-claim' AND attempt = 1))
) STRICT;

INSERT INTO operations (key, execution_id, attempt, kind, status, epoch, version,
                        created_at, updated_at, intent, result, note)
    SELECT key, execution_id, attempt, kind, status, epoch, version,
           created_at, updated_at, intent, result, note
    FROM operations_before ORDER BY rowid;

DROP TABLE operations_before;

CREATE INDEX operations_by_execution ON operations (execution_id, created_at);
CREATE INDEX operations_unfinished ON operations (created_at)
    WHERE status IN ('intended', 'ambiguous');

CREATE TRIGGER operations_cannot_be_removed BEFORE DELETE ON operations
BEGIN SELECT RAISE(ABORT, 'operations cannot be removed'); END;
CREATE TRIGGER operations_final_are_immutable BEFORE UPDATE ON operations
WHEN OLD.status IN ('succeeded', 'failed')
BEGIN SELECT RAISE(ABORT, 'a final operation cannot change'); END;
CREATE TRIGGER operations_identity_is_immutable BEFORE UPDATE ON operations
WHEN NEW.key IS NOT OLD.key OR NEW.kind IS NOT OLD.kind
  OR NEW.execution_id IS NOT OLD.execution_id OR NEW.attempt IS NOT OLD.attempt
  OR NEW.intent IS NOT OLD.intent OR NEW.created_at IS NOT OLD.created_at
  OR NEW.version <> OLD.version + 1
BEGIN SELECT RAISE(ABORT, 'an operation keeps its identity and intent'); END;
""",
    # ADR-0027 §1: a second instance-scoped kind, clear-stray-label. The same
    # rebuild as schema 5, with the check widened to the two kinds; rows,
    # indexes and triggers unchanged, so no event.
    5: """
DROP TRIGGER operations_cannot_be_removed;
DROP TRIGGER operations_final_are_immutable;
DROP TRIGGER operations_identity_is_immutable;
DROP INDEX operations_by_execution;
DROP INDEX operations_unfinished;
ALTER TABLE operations RENAME TO operations_before;

CREATE TABLE operations (
    key          TEXT PRIMARY KEY,
    execution_id TEXT REFERENCES executions (id),
    attempt      INTEGER NOT NULL CHECK (attempt >= 1),
    kind         TEXT NOT NULL,
    status       TEXT NOT NULL
                 CHECK (status IN ('intended', 'succeeded', 'failed', 'ambiguous')),
    epoch        INTEGER NOT NULL CHECK (epoch >= 1),
    version      INTEGER NOT NULL CHECK (version >= 1),
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL,
    intent       TEXT NOT NULL,
    result       TEXT NOT NULL,
    note         TEXT NOT NULL,
    CHECK (execution_id IS NOT NULL OR
           (kind IN ('release-orphan-claim', 'clear-stray-label') AND attempt = 1))
) STRICT;

INSERT INTO operations (key, execution_id, attempt, kind, status, epoch, version,
                        created_at, updated_at, intent, result, note)
    SELECT key, execution_id, attempt, kind, status, epoch, version,
           created_at, updated_at, intent, result, note
    FROM operations_before ORDER BY rowid;

DROP TABLE operations_before;

CREATE INDEX operations_by_execution ON operations (execution_id, created_at);
CREATE INDEX operations_unfinished ON operations (created_at)
    WHERE status IN ('intended', 'ambiguous');

CREATE TRIGGER operations_cannot_be_removed BEFORE DELETE ON operations
BEGIN SELECT RAISE(ABORT, 'operations cannot be removed'); END;
CREATE TRIGGER operations_final_are_immutable BEFORE UPDATE ON operations
WHEN OLD.status IN ('succeeded', 'failed')
BEGIN SELECT RAISE(ABORT, 'a final operation cannot change'); END;
CREATE TRIGGER operations_identity_is_immutable BEFORE UPDATE ON operations
WHEN NEW.key IS NOT OLD.key OR NEW.kind IS NOT OLD.kind
  OR NEW.execution_id IS NOT OLD.execution_id OR NEW.attempt IS NOT OLD.attempt
  OR NEW.intent IS NOT OLD.intent OR NEW.created_at IS NOT OLD.created_at
  OR NEW.version <> OLD.version + 1
BEGIN SELECT RAISE(ABORT, 'an operation keeps its identity and intent'); END;
""",
    6: """
ALTER TABLE executions ADD COLUMN retry_not_before TEXT;
""",
    # ADR-0029 §2, §3 and §8 (#18): accounting charges and the suspension.
    7: """
CREATE TABLE budget_ledger (
    seq           INTEGER PRIMARY KEY AUTOINCREMENT,
    execution_id  TEXT NOT NULL REFERENCES executions (id),
    attempt       INTEGER NOT NULL CHECK (attempt >= 1),
    kind          TEXT NOT NULL CHECK (kind IN ('reservation', 'extra')),
    amount_micros INTEGER NOT NULL CHECK (amount_micros > 0),
    at            TEXT NOT NULL,
    day           TEXT NOT NULL,
    month         TEXT NOT NULL
) STRICT;

CREATE UNIQUE INDEX budget_one_reservation_per_attempt
    ON budget_ledger (execution_id, attempt) WHERE kind = 'reservation';
CREATE INDEX budget_by_day ON budget_ledger (day);
CREATE INDEX budget_by_month ON budget_ledger (month);

CREATE TRIGGER budget_ledger_cannot_be_removed BEFORE DELETE ON budget_ledger
BEGIN SELECT RAISE(ABORT, 'charges cannot be removed'); END;
CREATE TRIGGER budget_ledger_cannot_change BEFORE UPDATE ON budget_ledger
BEGIN SELECT RAISE(ABORT, 'charges cannot change'); END;

CREATE TABLE suspension (
    id      INTEGER PRIMARY KEY CHECK (id = 1),
    reason  TEXT NOT NULL,
    since   TEXT NOT NULL,
    detail  TEXT NOT NULL
) STRICT;
""",
    # ADR-0030 §2, ADR-0040 (#91): gate records, written only by a person.
    8: """
CREATE TABLE gate_record (
    seq          INTEGER PRIMARY KEY AUTOINCREMENT,
    mode         TEXT NOT NULL CHECK (mode IN ('supervised')),
    evidence     TEXT NOT NULL,
    recorded_at  TEXT NOT NULL,
    recorded_by  TEXT NOT NULL,
    revoked_at   TEXT,
    revoked_by   TEXT,
    CHECK ((revoked_at IS NULL) = (revoked_by IS NULL))
) STRICT;

CREATE UNIQUE INDEX gate_record_one_active_per_mode
    ON gate_record (mode) WHERE revoked_at IS NULL;

CREATE TRIGGER gate_record_cannot_be_removed BEFORE DELETE ON gate_record
BEGIN SELECT RAISE(ABORT, 'gate records cannot be removed'); END;
CREATE TRIGGER gate_record_only_revoked_once BEFORE UPDATE ON gate_record
WHEN OLD.revoked_at IS NOT NULL
  OR NEW.seq IS NOT OLD.seq OR NEW.mode IS NOT OLD.mode
  OR NEW.evidence IS NOT OLD.evidence OR NEW.recorded_at IS NOT OLD.recorded_at
  OR NEW.recorded_by IS NOT OLD.recorded_by OR NEW.revoked_at IS NULL
BEGIN SELECT RAISE(ABORT, 'a gate record is only ever revoked, once'); END;
""",
}

GATE_SCHEMA_VERSION = 9
"""The first version with ``gate_record`` (#91, ADR-0040)."""

BUDGET_SCHEMA_VERSION = 8
"""The first version with ``budget_ledger`` and ``suspension`` (#18)."""

_BASE_COLUMNS = (
    "id, task_provider, task_key, state, attempt, version, created_at, updated_at, "
    "branch, change_request_url, last_error, base_revision, candidate_revision"
)
RETRY_SCHEMA_VERSION = 7
"""The first version with ``executions.retry_not_before`` (#13)."""


def _columns(conn: sqlite3.Connection) -> str:
    """The execution columns this store has. A store never claimed stays at
    ``NEW_DATABASE_VERSION`` until its first claim migrates it, and takes only
    unfenced writes until then; none of them can be a retry wait."""
    if _version(conn) >= RETRY_SCHEMA_VERSION:
        return _BASE_COLUMNS + ", retry_not_before"
    return _BASE_COLUMNS


class SQLiteExecutionStore:
    """``ExecutionStore`` persisted in a single SQLite file.

    Use ``":memory:"`` as ``path`` for a throwaway database. The store is safe
    to share between threads; separate processes coordinate through SQLite's
    own locking. :meth:`open_read_only` opens a store that never writes.
    """

    def __init__(self, path: Path | str, *, read_only: bool = False) -> None:
        self._lock = threading.Lock()
        self._read_only = read_only
        if read_only:
            # mode=ro: SQLite refuses every write, and a missing file is an error
            # rather than a new database.
            self._conn = sqlite3.connect(
                f"file:{Path(path).resolve()}?mode=ro",
                uri=True,
                isolation_level=None,
                check_same_thread=False,
                timeout=5.0,
            )
        else:
            self._conn = sqlite3.connect(
                str(path), isolation_level=None, check_same_thread=False, timeout=5.0
            )
        try:
            self._conn.execute("PRAGMA foreign_keys = ON")
            if read_only:
                _check_supported(_version(self._conn))
            else:
                if str(path) != ":memory:":
                    self._conn.execute("PRAGMA journal_mode = WAL")
                # Every commit is on disk before it returns: an intent must be
                # durable before its side effect starts (ADR-0020). Some builds
                # default WAL databases to NORMAL, which can lose the last
                # commits on power loss.
                self._conn.execute("PRAGMA synchronous = FULL")
                self._create_if_new()
        except BaseException:
            self._conn.close()
            raise

    @classmethod
    def open_read_only(cls, path: Path | str) -> SQLiteExecutionStore:
        """A store that never migrates and never writes (``ilmar events``, ``verify``).

        The database file is left byte-for-byte unchanged. On a WAL database
        SQLite may create its empty ``-shm`` and ``-wal`` side files.
        """
        return cls(path, read_only=True)

    # -- lifecycle -------------------------------------------------------

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    # -- ExecutionStore --------------------------------------------------

    def create(
        self, execution: Execution, transition: Transition, *, fence: Fence | None = None
    ) -> None:
        if fence is not None:
            self.commit(fence, Commit(created=((execution, transition),)))
            return
        check_create(execution, transition)
        with self._transaction() as conn:
            _check_unfenced(conn)
            _insert_execution(conn, execution, transition)

    def get(self, execution_id: str) -> Execution | None:
        with self._reading() as conn:
            return _get_execution(conn, execution_id)

    def active_for_task(self, task: TaskRef) -> Execution | None:
        with self._reading() as conn:
            row = conn.execute(
                f"SELECT {_columns(conn)} FROM executions "  # noqa: S608
                "WHERE task_provider = ? AND task_key = ? AND is_terminal = 0",
                (task.provider, task.key),
            ).fetchone()
        return None if row is None else _execution_from_row(row)

    def latest_update(self) -> datetime | None:
        with self._reading() as conn:
            return _latest_update(conn)

    def gate_records(self) -> Sequence[GateRecord]:
        with self._reading() as conn:
            return _gate_records(conn)

    def suspension(self) -> Suspension | None:
        with self._reading(any_version=True) as conn:
            return _suspension(conn)

    def budget_totals(self, at: datetime) -> BudgetTotals:
        with self._reading(any_version=True) as conn:
            return _budget_totals(conn, at)

    def charges(self, execution_id: str) -> Sequence[Charge]:
        with self._reading(any_version=True) as conn:
            if _version(conn) < BUDGET_SCHEMA_VERSION:
                return ()
            rows = conn.execute(
                "SELECT execution_id, attempt, kind, amount_micros, at FROM budget_ledger "
                "WHERE execution_id = ? ORDER BY seq",
                (execution_id,),
            ).fetchall()
        return tuple(
            Charge(
                _text(e), _integer(a), ChargeKind(_text(k)), _integer(m), _decode_time(_text(at))
            )
            for e, a, k, m, at in rows
        )

    def list_in_states(self, states: Collection[ExecutionState]) -> Sequence[Execution]:
        values = sorted({state.value for state in states})
        if not values:
            return ()
        with self._reading() as conn:
            return _list_executions(conn, values)

    def apply(
        self, updated: Execution, transition: Transition, *, fence: Fence | None = None
    ) -> None:
        if fence is not None:
            self.commit(fence, Commit(applied=((updated, transition),)))
            return
        check_apply(updated, transition)
        with self._transaction() as conn:
            _check_unfenced(conn)
            _update_execution(conn, updated, transition)

    def history(self, execution_id: str) -> Sequence[Transition]:
        with self._reading() as conn:
            exists = conn.execute(
                "SELECT 1 FROM executions WHERE id = ?", (execution_id,)
            ).fetchone()
            if exists is None:
                raise ExecutionNotFoundError(execution_id)
            rows = conn.execute(
                "SELECT execution_id, from_state, to_state, at, reason FROM transitions "
                "WHERE execution_id = ? ORDER BY seq",
                (execution_id,),
            ).fetchall()
        return tuple(
            Transition(
                execution_id=row[0],
                from_state=None if row[1] is None else ExecutionState(row[1]),
                to_state=ExecutionState(row[2]),
                at=_decode_time(row[3]),
                reason=row[4],
            )
            for row in rows
        )

    def append_evidence(
        self,
        record: EvidenceRecord,
        *,
        fence: Fence | None = None,
        context: EventContext | None = None,
    ) -> None:
        with self._transaction() as conn:
            if fence is None:
                _check_unfenced(conn)
            else:
                _check_fenced(conn, fence)
            if (
                conn.execute(
                    "SELECT 1 FROM executions WHERE id = ?", (record.execution_id,)
                ).fetchone()
                is None
            ):
                raise ExecutionNotFoundError(record.execution_id)
            conn.execute(
                "INSERT INTO evidence (execution_id, attempt, recorded_at, digest, record) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    record.execution_id,
                    record.attempt,
                    _encode_time(record.recorded_at),
                    record.digest(),
                    record.to_json(),
                ),
            )
            if fence is not None:
                _append_events(conn, (event_log.evidence_event(record, fence, context),))

    def evidence(self, execution_id: str) -> Sequence[EvidenceRecord]:
        with self._reading() as conn:
            if (
                conn.execute("SELECT 1 FROM executions WHERE id = ?", (execution_id,)).fetchone()
                is None
            ):
                raise ExecutionNotFoundError(execution_id)
            rows = conn.execute(
                "SELECT seq, digest, record FROM evidence WHERE execution_id = ? ORDER BY seq",
                (execution_id,),
            ).fetchall()
        records = []
        for seq, digest, text in rows:
            record = EvidenceRecord.from_json(_text(text))
            if record.digest() != digest or record.execution_id != execution_id:
                raise EvidenceIntegrityError(f"evidence row {seq} of {execution_id} was altered")
            records.append(record)
        return tuple(records)

    # -- fencing, journal and ledger (ADR-0020) --------------------------

    def claim_instance(self, instance_id: str, *, now: datetime) -> Fence:
        Fence(instance_id, 1)  # validates the identifier
        with self._transaction(migrating=True) as conn:
            version = _version(conn)
            current = _owner(conn) if version >= NEW_DATABASE_VERSION else None
            if current is not None and current.instance_id != instance_id:
                raise FencingError(
                    f"this store belongs to instance {current.instance_id!r}, not {instance_id!r}"
                )
            if version < COVERED_VERSION:
                # The caller holds the instance lock (the port's rule), so no
                # coordinator writes between this snapshot and the first
                # covered event.
                _migrate_under_lock(conn, version, current, instance_id, now=now)
            elif version < SCHEMA_VERSION:
                # Already covered: the later migrations change no row, so the
                # event log needs no new baseline.
                _run_script(conn, "\n".join(MIGRATIONS[v] for v in range(version, SCHEMA_VERSION)))
                conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            fence = Fence(instance_id, 1 if current is None else current.epoch + 1)
            claimed = _encode_time(now)
            conn.execute(
                "INSERT INTO owner (id, instance_id, epoch, claimed_at) VALUES (1, ?, ?, ?) "
                "ON CONFLICT (id) DO UPDATE SET epoch = excluded.epoch, "
                "claimed_at = excluded.claimed_at",
                (instance_id, fence.epoch, claimed),
            )
            conn.execute(
                "INSERT INTO owner_history (instance_id, epoch, claimed_at) VALUES (?, ?, ?)",
                (instance_id, fence.epoch, claimed),
            )
            _append_events(conn, (event_log.ownership_event(current, fence, now=now),))
        return fence

    def fence(self) -> Fence | None:
        with self._reading() as conn:
            return _owner(conn)

    def commit(self, fence: Fence, changes: Commit, *, context: EventContext | None = None) -> None:
        with self._transaction() as conn:
            _check_fenced(conn, fence)
            check_commit(
                fence,
                changes,
                execution=lambda key: _get_execution(conn, key),
                operation=lambda key: _get_operation(conn, key),
                resource=lambda key: _get_resource(conn, key),
                latest_update=_latest_update(conn),
                suspension=_suspension(conn),
                totals=lambda at: _budget_totals(conn, at),
            )
            # Derived and paired against the stored rows, before any is written.
            events = event_log.commit_events(
                fence,
                changes,
                context,
                execution=lambda key: _get_execution(conn, key),
                operation=lambda key: _get_operation(conn, key),
                resource=lambda key: _get_resource(conn, key),
                suspended=_suspension(conn),
                gated=_active_gate(conn, changes),
            )
            for execution, transition in changes.created:
                _insert_execution(conn, execution, transition)
            for execution, transition in changes.applied:
                _update_execution(conn, execution, transition)
            for operation in changes.operations:
                _write_operation(conn, operation)
            for resource in changes.resources:
                _write_resource(conn, resource, fence)
            _write_accounting(conn, changes)
            _write_gate(conn, changes, context)
            _append_events(conn, events)

    def events(
        self, *, after_seq: int = 0, limit: int | None = None, execution_id: str | None = None
    ) -> Sequence[Event]:
        with self._reading(any_version=True) as conn:
            lines = _event_lines(conn, after_seq, limit, execution_id)
        return tuple(Event.from_json(line) for line in lines)

    def event_lines(
        self, *, after_seq: int = 0, limit: int | None = None
    ) -> tuple[tuple[int, str], ...]:
        """``(seq, line)`` of the stored events after ``after_seq``, in ``seq`` order,
        unparsed: an exporter prints them as they are and reports a corrupt one
        itself. No events table: nothing."""
        with self._reading(any_version=True) as conn:
            if not _has_events(conn):
                return ()
            query = "SELECT seq, line FROM events WHERE seq > ? ORDER BY seq"
            values: list[object] = [after_seq]
            if limit is not None:
                query += " LIMIT ?"
                values.append(limit)
            rows = conn.execute(query, values).fetchall()
        return tuple((_integer(seq), _text(line)) for seq, line in rows)

    def history_snapshot(self) -> tuple[tuple[str, ...], StoredState]:
        """Every stored event line and the rows I11 is checked against, read in one
        transaction (ADR-0024 §8, ``ilmar events verify``). No lines: no coverage."""
        with self._reading(any_version=True) as conn:
            conn.execute("BEGIN")
            try:
                if not _has_events(conn):
                    return (), event_log.stored_state(executions=(), operations=(),
                                                      resources=(), transitions={},
                                                      evidence={})  # fmt: skip
                return _raw_event_lines(conn), _stored_state(conn)
            finally:
                conn.execute("COMMIT")

    def operation(self, key: str) -> Operation | None:
        with self._reading() as conn:
            return _get_operation(conn, key)

    def operations(self, execution_id: str) -> Sequence[Operation]:
        with self._reading() as conn:
            rows = conn.execute(
                f"SELECT {_OPERATION_COLUMNS} FROM operations "  # noqa: S608
                "WHERE execution_id = ? ORDER BY created_at, rowid",
                (execution_id,),
            ).fetchall()
        return tuple(_operation_from_row(row) for row in rows)

    def unfinished_operations(self) -> Sequence[Operation]:
        with self._reading() as conn:
            rows = conn.execute(
                f"SELECT {_OPERATION_COLUMNS} FROM operations "  # noqa: S608
                "WHERE status IN ('intended', 'ambiguous') ORDER BY created_at, rowid"
            ).fetchall()
        return tuple(_operation_from_row(row) for row in rows)

    def instance_operations(self) -> Sequence[Operation]:
        with self._reading() as conn:
            if _version(conn) < SCHEMA_VERSION:
                return ()  # before ADR-0026's migration no operation can omit it
            rows = conn.execute(
                f"SELECT {_OPERATION_COLUMNS} FROM operations "  # noqa: S608
                "WHERE execution_id IS NULL ORDER BY created_at, rowid"
            ).fetchall()
        return tuple(_operation_from_row(row) for row in rows)

    def resource(self, key: str) -> Resource | None:
        with self._reading() as conn:
            return _get_resource(conn, key)

    def resources(
        self,
        *,
        execution_id: str | None = None,
        statuses: Collection[ResourceStatus] | None = None,
    ) -> Sequence[Resource]:
        clauses, values = [], []
        if execution_id is not None:
            clauses.append("execution_id = ?")
            values.append(execution_id)
        if statuses is not None:
            wanted = sorted({status.value for status in statuses})
            if not wanted:
                return ()
            clauses.append(f"status IN ({', '.join('?' for _ in wanted)})")
            values += wanted
        where = f"WHERE {' AND '.join(clauses)} " if clauses else ""
        with self._reading() as conn:
            rows = conn.execute(
                f"SELECT {_RESOURCE_COLUMNS} FROM resources {where}"  # noqa: S608
                "ORDER BY created_at, rowid",
                values,
            ).fetchall()
        return tuple(_resource_from_row(row) for row in rows)

    # -- internals -------------------------------------------------------

    @contextmanager
    def _reading(self, *, any_version: bool = False) -> Iterator[sqlite3.Connection]:
        with self._lock:
            if not any_version:
                _check_current(_version(self._conn))
            yield self._conn

    @contextmanager
    def _transaction(self, *, migrating: bool = False) -> Iterator[sqlite3.Connection]:
        if self._read_only:
            raise FencingError("this store was opened read-only; it never writes")
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                version = _version(self._conn)
                _check_supported(version)
                if not migrating:
                    _check_current(version)
                yield self._conn
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise
            self._conn.execute("COMMIT")

    def _create_if_new(self) -> None:
        """Give a brand-new, empty database its schema. An existing one is never
        migrated here: that happens under the instance lock (claim_instance)."""
        with self._lock:
            if _version(self._conn) != 0:
                _check_supported(_version(self._conn))
                return
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                version = _version(self._conn)  # another process may have created it
                if version == 0:
                    if self._conn.execute("SELECT 1 FROM sqlite_master LIMIT 1").fetchone():
                        raise RuntimeError("the database has tables but no Ilmar schema version")
                    script = [SCHEMA_V1] + [MIGRATIONS[v] for v in range(1, NEW_DATABASE_VERSION)]
                    _run_script(self._conn, "\n".join(script))
                    self._conn.execute(f"PRAGMA user_version = {NEW_DATABASE_VERSION}")
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise
            self._conn.execute("COMMIT")


def _version(conn: sqlite3.Connection) -> int:
    (version,) = conn.execute("PRAGMA user_version").fetchone()
    return _integer(version)


def _check_supported(version: int) -> None:
    if version > SCHEMA_VERSION:
        raise RuntimeError(
            f"database schema version {version} is newer than supported "
            f"version {SCHEMA_VERSION}; upgrade Ilmar"
        )


def _check_current(version: int) -> None:
    if version < NEW_DATABASE_VERSION:
        raise FencingError(
            f"the store is at schema version {version}; claim it under the instance lock "
            "to migrate it before use"
        )


def _check_unfenced(conn: sqlite3.Connection) -> None:
    """An unfenced write: only on a store never owned, which has no events yet."""
    check_fence(_owner(conn), None)
    if _version(conn) >= COVERED_VERSION:  # pragma: no cover - a covered store is owned
        raise FencingError("a store with event coverage takes only fenced writes")


def _check_fenced(conn: sqlite3.Connection, fence: Fence) -> None:
    check_fence(_owner(conn), fence)
    if _version(conn) < COVERED_VERSION:
        raise FencingError(
            "this store has no event coverage yet; only claim_instance, under the "
            "instance lock, may start it"
        )
    if _version(conn) < SCHEMA_VERSION:
        raise FencingError("the store is not migrated yet; claim it under the instance lock")


def _run_script(conn: sqlite3.Connection, script: str) -> None:
    """Run ``script`` statement by statement inside the caller's transaction
    (``executescript`` would commit it)."""
    statement = ""
    for line in script.splitlines(keepends=True):
        statement += line
        if sqlite3.complete_statement(statement):
            conn.execute(statement)
            statement = ""
    if statement.strip():
        raise ValueError("the schema script ends inside a statement")


def _migrate_under_lock(
    conn: sqlite3.Connection, version: int, owner: Fence | None, claimant: str, *, now: datetime
) -> None:
    """Every migration of an existing database, then the event baseline, in the
    caller's transaction (ADR-0024 §8). Raises, and so rolls everything back, if
    the baseline does not match the stored rows exactly."""
    script = [MIGRATIONS[v] for v in range(max(version, 1), SCHEMA_VERSION)]
    if version == 0:  # pragma: no cover - a new database is created when opened
        script.insert(0, SCHEMA_V1)
    _run_script(conn, "\n".join(script))
    events = event_log.baseline(now=now, owner=owner, claimant=claimant,
                                from_schema_version=version, state=_stored_state(conn))  # fmt: skip
    _append_events(conn, events)
    conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")


def _has_events(conn: sqlite3.Connection) -> bool:
    return (
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'events'"
        ).fetchone()
        is not None
    )


def _next_seq(conn: sqlite3.Connection) -> int:
    (highest,) = conn.execute("SELECT COALESCE(MAX(seq), 0) FROM events").fetchone()
    row = conn.execute("SELECT seq FROM sqlite_sequence WHERE name = 'events'").fetchone()
    issued = 0 if row is None else _integer(row[0])
    return max(_integer(highest), issued) + 1  # a seq is never reused, even after tampering


def _append_events(conn: sqlite3.Connection, events: Sequence[Event]) -> None:
    for event in event_log.with_seqs(events, _next_seq(conn)):
        conn.execute(
            "INSERT INTO events (seq, execution_id, type, line) VALUES (?, ?, ?, ?)",
            (event.seq, event.execution, event.type.value, event.to_json()),
        )


def _raw_event_lines(conn: sqlite3.Connection) -> tuple[str, ...]:
    """The stored lines in row order. A line is the record of its event, its own
    ``seq`` included: a reordered or renumbered row shows as a ``seq`` gap."""
    return tuple(_text(line) for (line,) in conn.execute("SELECT line FROM events ORDER BY seq"))


def _event_lines(
    conn: sqlite3.Connection, after_seq: int, limit: int | None, execution_id: str | None = None
) -> list[str]:
    if not _has_events(conn):
        return []
    query = "SELECT seq, line FROM events WHERE seq > ?"
    values: list[object] = [after_seq]
    if execution_id is not None:  # the events_by_execution index
        query += " AND execution_id = ?"
        values.append(execution_id)
    query += " ORDER BY seq"
    if limit is not None:
        query += " LIMIT ?"
        values.append(limit)
    lines = []
    for seq, line in conn.execute(query, values).fetchall():
        event = Event.from_json(_text(line))
        if event.seq != seq:
            raise CorruptEventError(f"event row {seq} holds seq {event.seq}")
        lines.append(_text(line))
    return lines


def _latest_update(conn: sqlite3.Connection) -> datetime | None:
    """Every ``updated_at`` is stored as UTC with microseconds, so the text order
    is the time order."""
    (value,) = conn.execute("SELECT MAX(updated_at) FROM executions").fetchone()
    return None if value is None else _decode_time(_text(value))


def _list_executions(conn: sqlite3.Connection, states: Sequence[str]) -> tuple[Execution, ...]:
    placeholders = ", ".join("?" for _ in states)
    rows = conn.execute(
        f"SELECT {_columns(conn)} FROM executions "  # noqa: S608
        f"WHERE state IN ({placeholders}) ORDER BY created_at, id",
        list(states),
    ).fetchall()
    return tuple(_execution_from_row(row) for row in rows)


def _stored_state(conn: sqlite3.Connection) -> StoredState:
    """Every row I11 covers, oldest first (the baseline's order)."""
    executions = _list_executions(conn, sorted(s.value for s in ExecutionState))
    operations = [
        _operation_from_row(row)
        for row in conn.execute(
            f"SELECT {_OPERATION_COLUMNS} FROM operations ORDER BY created_at, rowid"  # noqa: S608
        ).fetchall()
    ]
    resources = [
        _resource_from_row(row)
        for row in conn.execute(
            f"SELECT {_RESOURCE_COLUMNS} FROM resources ORDER BY created_at, rowid"  # noqa: S608
        ).fetchall()
    ]

    def counts(table: str) -> dict[str, int]:
        return {
            _text(execution_id): _integer(n)
            for execution_id, n in conn.execute(
                f"SELECT execution_id, COUNT(*) FROM {table} GROUP BY execution_id"  # noqa: S608
            ).fetchall()
        }

    return event_log.stored_state(executions=executions, operations=operations,
                                  resources=resources, transitions=counts("transitions"),
                                  evidence=counts("evidence"))  # fmt: skip


_OPERATION_COLUMNS = (
    "key, execution_id, attempt, kind, status, epoch, version, created_at, updated_at, "
    "intent, result, note"
)
_RESOURCE_COLUMNS = (
    "key, execution_id, attempt, kind, name, owner_token, status, version, created_at, "
    "updated_at, identity, note"
)


def _owner(conn: sqlite3.Connection) -> Fence | None:
    row = conn.execute("SELECT instance_id, epoch FROM owner WHERE id = 1").fetchone()
    return None if row is None else Fence(_text(row[0]), _integer(row[1]))


def _get_execution(conn: sqlite3.Connection, execution_id: str) -> Execution | None:
    row = conn.execute(
        f"SELECT {_columns(conn)} FROM executions WHERE id = ?",  # noqa: S608
        (execution_id,),
    ).fetchone()
    return None if row is None else _execution_from_row(row)


def _insert_execution(
    conn: sqlite3.Connection, execution: Execution, transition: Transition
) -> None:
    try:
        row = _execution_row(execution, retry=_version(conn) >= RETRY_SCHEMA_VERSION)
        placeholders = ", ".join("?" for _ in range(len(row) + 1))
        conn.execute(
            f"INSERT INTO executions ({_columns(conn)}, is_terminal) "  # noqa: S608
            f"VALUES ({placeholders})",
            (*row, int(execution.state.is_terminal)),
        )
    except sqlite3.IntegrityError as exc:
        raise DuplicateExecutionError(
            f"execution {execution.id} or an active execution for "
            f"task {execution.task} already exists"
        ) from exc
    _insert_transition(conn, transition)


def _update_execution(conn: sqlite3.Connection, updated: Execution, transition: Transition) -> None:
    current = _get_execution(conn, updated.id)
    if current is None:
        raise ExecutionNotFoundError(updated.id)
    if current.version != updated.version - 1 or current.state is not transition.from_state:
        raise ConcurrencyConflictError(
            f"execution {updated.id} is at version {current.version}, "
            f"update expected {updated.version - 1}"
        )
    if current.task != updated.task or current.created_at != updated.created_at:
        raise ValueError("task and created_at are immutable")
    try:
        retry = _version(conn) >= RETRY_SCHEMA_VERSION
        _require_retry_column(updated, retry=retry)
        conn.execute(
            _UPDATE_EXECUTION_V7 if retry else _UPDATE_EXECUTION,
            (
                updated.state.value,
                int(updated.state.is_terminal),
                updated.attempt,
                updated.version,
                _encode_time(updated.updated_at),
                updated.branch,
                updated.change_request_url,
                updated.last_error,
                updated.base_revision,
                updated.candidate_revision,
                *((_optional_time(updated.retry_not_before),) if retry else ()),
                updated.id,
            ),
        )
    except sqlite3.IntegrityError as exc:  # pragma: no cover - defensive
        raise DuplicateExecutionError(str(exc)) from exc
    _insert_transition(conn, transition)


def _fields(values: Mapping[str, str]) -> str:
    return json.dumps(dict(values), sort_keys=True, ensure_ascii=False)


def _write_operation(conn: sqlite3.Connection, operation: Operation) -> None:
    row = (
        operation.status.value,
        operation.epoch,
        operation.version,
        _encode_time(operation.updated_at),
        _fields(operation.result),
        operation.note,
    )
    if operation.version == 1:
        conn.execute(
            f"INSERT INTO operations ({_OPERATION_COLUMNS}) "  # noqa: S608
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                operation.key,
                operation.execution_id,
                operation.attempt,
                operation.kind.value,
                operation.status.value,
                operation.epoch,
                operation.version,
                _encode_time(operation.created_at),
                _encode_time(operation.updated_at),
                _fields(operation.intent),
                _fields(operation.result),
                operation.note,
            ),
        )
    else:
        changed = conn.execute(
            "UPDATE operations SET status = ?, epoch = ?, version = ?, updated_at = ?, "
            "result = ?, note = ? WHERE key = ? AND version = ?",
            (*row, operation.key, operation.version - 1),
        ).rowcount
        if changed != 1:  # pragma: no cover - check_commit already compared versions
            raise ConcurrencyConflictError(f"operation {operation.key} changed concurrently")
    conn.execute(
        "INSERT INTO ledger_events (subject, key, status, version, epoch, at, note) "
        "VALUES ('operation', ?, ?, ?, ?, ?, ?)",
        (
            operation.key,
            operation.status.value,
            operation.version,
            operation.epoch,
            _encode_time(operation.updated_at),
            operation.note,
        ),
    )


def _write_resource(conn: sqlite3.Connection, resource: Resource, fence: Fence) -> None:
    if resource.version == 1:
        conn.execute(
            f"INSERT INTO resources ({_RESOURCE_COLUMNS}) "  # noqa: S608
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                resource.key,
                resource.execution_id,
                resource.attempt,
                resource.kind.value,
                resource.name,
                resource.owner_token,
                resource.status.value,
                resource.version,
                _encode_time(resource.created_at),
                _encode_time(resource.updated_at),
                resource.identity,
                resource.note,
            ),
        )
    else:
        changed = conn.execute(
            "UPDATE resources SET status = ?, version = ?, updated_at = ?, identity = ?, "
            "note = ? WHERE key = ? AND version = ?",
            (
                resource.status.value,
                resource.version,
                _encode_time(resource.updated_at),
                resource.identity,
                resource.note,
                resource.key,
                resource.version - 1,
            ),
        ).rowcount
        if changed != 1:  # pragma: no cover - check_commit already compared versions
            raise ConcurrencyConflictError(f"resource {resource.key} changed concurrently")
    conn.execute(
        "INSERT INTO ledger_events (subject, key, status, version, epoch, at, note) "
        "VALUES ('resource', ?, ?, ?, ?, ?, ?)",
        (
            resource.key,
            resource.status.value,
            resource.version,
            fence.epoch,
            _encode_time(resource.updated_at),
            resource.note,
        ),
    )


def _get_operation(conn: sqlite3.Connection, key: str) -> Operation | None:
    row = conn.execute(
        f"SELECT {_OPERATION_COLUMNS} FROM operations WHERE key = ?",  # noqa: S608
        (key,),
    ).fetchone()
    return None if row is None else _operation_from_row(row)


def _get_resource(conn: sqlite3.Connection, key: str) -> Resource | None:
    row = conn.execute(
        f"SELECT {_RESOURCE_COLUMNS} FROM resources WHERE key = ?",  # noqa: S608
        (key,),
    ).fetchone()
    return None if row is None else _resource_from_row(row)


def _operation_from_row(row: tuple[object, ...]) -> Operation:
    (
        key,
        execution_id,
        attempt,
        kind,
        status,
        epoch,
        version,
        created,
        updated,
        intent,
        result,
        note,
    ) = row
    return Operation(
        key=_text(key),
        execution_id=None if execution_id is None else _text(execution_id),
        attempt=_integer(attempt),
        kind=OperationKind(_text(kind)),
        status=OperationStatus(_text(status)),
        epoch=_integer(epoch),
        version=_integer(version),
        created_at=_decode_time(_text(created)),
        updated_at=_decode_time(_text(updated)),
        intent=json.loads(_text(intent)),
        result=json.loads(_text(result)),
        note=_text(note),
    )


def _resource_from_row(row: tuple[object, ...]) -> Resource:
    (
        key,
        execution_id,
        attempt,
        kind,
        name,
        owner,
        status,
        version,
        created,
        updated,
        identity,
        note,
    ) = row
    return Resource(
        key=_text(key),
        execution_id=_text(execution_id),
        attempt=_integer(attempt),
        kind=ResourceKind(_text(kind)),
        name=_text(name),
        owner_token=_text(owner),
        status=ResourceStatus(_text(status)),
        version=_integer(version),
        created_at=_decode_time(_text(created)),
        updated_at=_decode_time(_text(updated)),
        identity=_optional_text(identity),
        note=_text(note),
    )


def _encode_time(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="microseconds")


def _decode_time(value: str) -> datetime:
    return datetime.fromisoformat(value)


_UPDATE_EXECUTION = (
    "UPDATE executions SET state = ?, is_terminal = ?, attempt = ?, version = ?, "
    "updated_at = ?, branch = ?, change_request_url = ?, last_error = ?, "
    "base_revision = ?, candidate_revision = ? WHERE id = ?"
)
_UPDATE_EXECUTION_V7 = (
    "UPDATE executions SET state = ?, is_terminal = ?, attempt = ?, version = ?, "
    "updated_at = ?, branch = ?, change_request_url = ?, last_error = ?, "
    "base_revision = ?, candidate_revision = ?, retry_not_before = ? WHERE id = ?"
)


def _require_retry_column(execution: Execution, *, retry: bool) -> None:
    if execution.retry_not_before is not None and not retry:
        raise ValueError("a retry wait needs schema 7: claim the store to migrate it")


def _execution_row(execution: Execution, *, retry: bool) -> tuple[object, ...]:
    _require_retry_column(execution, retry=retry)
    base = (
        execution.id,
        execution.task.provider,
        execution.task.key,
        execution.state.value,
        execution.attempt,
        execution.version,
        _encode_time(execution.created_at),
        _encode_time(execution.updated_at),
        execution.branch,
        execution.change_request_url,
        execution.last_error,
        execution.base_revision,
        execution.candidate_revision,
    )
    return (*base, _optional_time(execution.retry_not_before)) if retry else base


def _execution_from_row(row: tuple[object, ...]) -> Execution:
    (
        id_,
        provider,
        key,
        state,
        attempt,
        version,
        created_at,
        updated_at,
        branch,
        change_request_url,
        last_error,
        base_revision,
        candidate_revision,
        *rest,
    ) = row
    retry_not_before = rest[0] if rest else None
    return Execution(
        id=_text(id_),
        task=TaskRef(provider=_text(provider), key=_text(key)),
        state=ExecutionState(_text(state)),
        attempt=_integer(attempt),
        version=_integer(version),
        created_at=_decode_time(_text(created_at)),
        updated_at=_decode_time(_text(updated_at)),
        branch=_optional_text(branch),
        change_request_url=_optional_text(change_request_url),
        last_error=_optional_text(last_error),
        base_revision=_optional_text(base_revision),
        candidate_revision=_optional_text(candidate_revision),
        retry_not_before=(
            None if retry_not_before is None else _decode_time(_text(retry_not_before))
        ),
    )


def _optional_time(value: datetime | None) -> str | None:
    return None if value is None else _encode_time(value)


def _text(value: object) -> str:
    if isinstance(value, str):
        return value
    raise TypeError(f"expected text column, got {type(value).__name__}")


def _integer(value: object) -> int:
    if isinstance(value, int):
        return value
    raise TypeError(f"expected integer column, got {type(value).__name__}")


def _optional_text(value: object) -> str | None:
    return None if value is None else _text(value)


def _insert_transition(conn: sqlite3.Connection, transition: Transition) -> None:
    conn.execute(
        "INSERT INTO transitions (execution_id, from_state, to_state, at, reason) "
        "VALUES (?, ?, ?, ?, ?)",
        (
            transition.execution_id,
            None if transition.from_state is None else transition.from_state.value,
            transition.to_state.value,
            _encode_time(transition.at),
            transition.reason,
        ),
    )


# -- gate records (ADR-0040) -----------------------------------------------------------


def _gate_records(conn: sqlite3.Connection) -> tuple[GateRecord, ...]:
    if _version(conn) < GATE_SCHEMA_VERSION:
        return ()
    rows = conn.execute(
        "SELECT seq, mode, evidence, recorded_at, recorded_by, revoked_at, revoked_by "
        "FROM gate_record ORDER BY seq"
    ).fetchall()
    records = []
    for seq, mode, evidence, recorded_at, recorded_by, revoked_at, revoked_by in rows:
        stated = GateEvidence.from_json(_text(evidence))
        if stated.mode != _text(mode):
            raise LedgerIntegrityError(f"gate record {seq} was altered")
        records.append(GateRecord(
            _integer(seq), stated, _decode_time(_text(recorded_at)), _text(recorded_by),
            None if revoked_at is None else _decode_time(_text(revoked_at)),
            None if revoked_by is None else _text(revoked_by),
        ))  # fmt: skip
    return tuple(records)


def _active_gate(conn: sqlite3.Connection, changes: Commit) -> GateRecord | None:
    mode = event_log.gate_mode(changes)
    return None if mode is None else active_gate(_gate_records(conn), mode)


def _write_gate(conn: sqlite3.Connection, changes: Commit, context: EventContext | None) -> None:
    gate = changes.gate
    if gate is None:
        return
    if _version(conn) < GATE_SCHEMA_VERSION or context is None:  # pragma: no cover - checked
        raise LedgerIntegrityError("this store cannot hold gate records yet")
    who = context.actor.ref or ""
    if isinstance(gate, GateRecorded):
        conn.execute(
            "INSERT INTO gate_record (mode, evidence, recorded_at, recorded_by) "
            "VALUES (?, ?, ?, ?)",
            (gate.evidence.mode, gate.evidence.to_json(), _encode_time(gate.at), who),
        )
        return
    conn.execute(
        "UPDATE gate_record SET revoked_at = ?, revoked_by = ? "
        "WHERE mode = ? AND revoked_at IS NULL",
        (_encode_time(gate.at), who, gate.mode),
    )


# -- accounting and suspension (ADR-0029 §2 and §3) ------------------------------------


def _suspension(conn: sqlite3.Connection) -> Suspension | None:
    if _version(conn) < BUDGET_SCHEMA_VERSION:
        return None
    row = conn.execute("SELECT reason, since, detail FROM suspension WHERE id = 1").fetchone()
    if row is None:
        return None
    reason, since, detail = row
    return Suspension(SuspensionReason(_text(reason)), _decode_time(_text(since)), _text(detail))


def _budget_totals(conn: sqlite3.Connection, at: datetime) -> BudgetTotals:
    if _version(conn) < BUDGET_SCHEMA_VERSION:
        return BudgetTotals()
    day, month = conn.execute(
        "SELECT (SELECT COALESCE(SUM(amount_micros), 0) FROM budget_ledger WHERE day = ?), "
        "(SELECT COALESCE(SUM(amount_micros), 0) FROM budget_ledger WHERE month = ?)",
        (utc_day(at), utc_month(at)),
    ).fetchone()
    return BudgetTotals(_integer(day), _integer(month))


def _write_accounting(conn: sqlite3.Connection, changes: Commit) -> None:
    if not (changes.charges or changes.suspend is not None or changes.resume is not None):
        return
    if _version(conn) < BUDGET_SCHEMA_VERSION:  # pragma: no cover - claim migrates first
        raise LedgerIntegrityError("this store predates accounting; claim it to migrate")
    for charge in changes.charges:
        conn.execute(
            "INSERT INTO budget_ledger "
            "(execution_id, attempt, kind, amount_micros, at, day, month) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (charge.execution_id, charge.attempt, charge.kind.value, charge.amount_micros,
             _encode_time(charge.at), charge.day, charge.month),
        )  # fmt: skip
    if changes.suspend is not None:
        conn.execute(
            "INSERT INTO suspension (id, reason, since, detail) VALUES (1, ?, ?, ?)",
            (changes.suspend.reason.value, _encode_time(changes.suspend.since),
             changes.suspend.detail),
        )  # fmt: skip
    if changes.resume is not None:
        conn.execute("DELETE FROM suspension WHERE id = 1")
