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

from ilmar.domain.errors import (
    ConcurrencyConflictError,
    DuplicateExecutionError,
    EvidenceIntegrityError,
    ExecutionNotFoundError,
    FencingError,
)
from ilmar.domain.execution import Execution, ExecutionState, Transition
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
from ilmar.ports.execution_store import check_apply, check_commit, check_create, check_fence

SCHEMA_VERSION = 3

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
}

_COLUMNS = (
    "id, task_provider, task_key, state, attempt, version, created_at, updated_at, "
    "branch, change_request_url, last_error, base_revision, candidate_revision"
)


class SQLiteExecutionStore:
    """``ExecutionStore`` persisted in a single SQLite file.

    Use ``":memory:"`` as ``path`` for a throwaway database. The store is safe
    to share between threads; separate processes coordinate through SQLite's
    own locking.
    """

    def __init__(self, path: Path | str) -> None:
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(
            str(path), isolation_level=None, check_same_thread=False, timeout=5.0
        )
        try:
            self._conn.execute("PRAGMA foreign_keys = ON")
            if str(path) != ":memory:":
                self._conn.execute("PRAGMA journal_mode = WAL")
            # Every commit is on disk before it returns: an intent must be
            # durable before its side effect starts (ADR-0020). Some builds
            # default WAL databases to NORMAL, which can lose the last commits
            # on power loss.
            self._conn.execute("PRAGMA synchronous = FULL")
            self._migrate()
        except BaseException:
            self._conn.close()
            raise

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
            check_fence(_owner(conn), None)
            _insert_execution(conn, execution, transition)

    def get(self, execution_id: str) -> Execution | None:
        with self._lock:
            row = self._conn.execute(
                f"SELECT {_COLUMNS} FROM executions WHERE id = ?",  # noqa: S608
                (execution_id,),
            ).fetchone()
        return None if row is None else _execution_from_row(row)

    def active_for_task(self, task: TaskRef) -> Execution | None:
        with self._lock:
            row = self._conn.execute(
                f"SELECT {_COLUMNS} FROM executions "  # noqa: S608
                "WHERE task_provider = ? AND task_key = ? AND is_terminal = 0",
                (task.provider, task.key),
            ).fetchone()
        return None if row is None else _execution_from_row(row)

    def list_in_states(self, states: Collection[ExecutionState]) -> Sequence[Execution]:
        values = sorted({state.value for state in states})
        if not values:
            return ()
        placeholders = ", ".join("?" for _ in values)
        with self._lock:
            rows = self._conn.execute(
                f"SELECT {_COLUMNS} FROM executions "  # noqa: S608
                f"WHERE state IN ({placeholders}) ORDER BY created_at, id",
                values,
            ).fetchall()
        return tuple(_execution_from_row(row) for row in rows)

    def apply(
        self, updated: Execution, transition: Transition, *, fence: Fence | None = None
    ) -> None:
        if fence is not None:
            self.commit(fence, Commit(applied=((updated, transition),)))
            return
        check_apply(updated, transition)
        with self._transaction() as conn:
            check_fence(_owner(conn), None)
            _update_execution(conn, updated, transition)

    def history(self, execution_id: str) -> Sequence[Transition]:
        with self._lock:
            exists = self._conn.execute(
                "SELECT 1 FROM executions WHERE id = ?", (execution_id,)
            ).fetchone()
            if exists is None:
                raise ExecutionNotFoundError(execution_id)
            rows = self._conn.execute(
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

    def append_evidence(self, record: EvidenceRecord, *, fence: Fence | None = None) -> None:
        with self._transaction() as conn:
            check_fence(_owner(conn), fence)
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

    def evidence(self, execution_id: str) -> Sequence[EvidenceRecord]:
        with self._lock:
            if (
                self._conn.execute(
                    "SELECT 1 FROM executions WHERE id = ?", (execution_id,)
                ).fetchone()
                is None
            ):
                raise ExecutionNotFoundError(execution_id)
            rows = self._conn.execute(
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
        with self._transaction() as conn:
            current = _owner(conn)
            if current is not None and current.instance_id != instance_id:
                raise FencingError(
                    f"this store belongs to instance {current.instance_id!r}, not {instance_id!r}"
                )
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
        return fence

    def fence(self) -> Fence | None:
        with self._lock:
            return _owner(self._conn)

    def commit(self, fence: Fence, changes: Commit) -> None:
        with self._transaction() as conn:
            check_fence(_owner(conn), fence)
            check_commit(
                fence,
                changes,
                execution=lambda key: _get_execution(conn, key),
                operation=lambda key: _get_operation(conn, key),
                resource=lambda key: _get_resource(conn, key),
            )
            for execution, transition in changes.created:
                _insert_execution(conn, execution, transition)
            for execution, transition in changes.applied:
                _update_execution(conn, execution, transition)
            for operation in changes.operations:
                _write_operation(conn, operation)
            for resource in changes.resources:
                _write_resource(conn, resource, fence)

    def operation(self, key: str) -> Operation | None:
        with self._lock:
            return _get_operation(self._conn, key)

    def operations(self, execution_id: str) -> Sequence[Operation]:
        with self._lock:
            rows = self._conn.execute(
                f"SELECT {_OPERATION_COLUMNS} FROM operations "  # noqa: S608
                "WHERE execution_id = ? ORDER BY created_at, rowid",
                (execution_id,),
            ).fetchall()
        return tuple(_operation_from_row(row) for row in rows)

    def unfinished_operations(self) -> Sequence[Operation]:
        with self._lock:
            rows = self._conn.execute(
                f"SELECT {_OPERATION_COLUMNS} FROM operations "  # noqa: S608
                "WHERE status IN ('intended', 'ambiguous') ORDER BY created_at, rowid"
            ).fetchall()
        return tuple(_operation_from_row(row) for row in rows)

    def resource(self, key: str) -> Resource | None:
        with self._lock:
            return _get_resource(self._conn, key)

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
        with self._lock:
            rows = self._conn.execute(
                f"SELECT {_RESOURCE_COLUMNS} FROM resources {where}"  # noqa: S608
                "ORDER BY created_at, rowid",
                values,
            ).fetchall()
        return tuple(_resource_from_row(row) for row in rows)

    # -- internals -------------------------------------------------------

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                yield self._conn
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise
            self._conn.execute("COMMIT")

    def _migrate(self) -> None:
        with self._lock:
            (version,) = self._conn.execute("PRAGMA user_version").fetchone()
            if version == SCHEMA_VERSION:
                return
            if version > SCHEMA_VERSION:
                raise RuntimeError(
                    f"database schema version {version} is newer than supported "
                    f"version {SCHEMA_VERSION}; upgrade Ilmar"
                )
            # Brand-new database: create version 1, then apply every migration.
            # Everything happens in one transaction, so a failure leaves the
            # file exactly as it was.
            script = [] if version else [SCHEMA_V1]
            script += [MIGRATIONS[v] for v in range(max(version, 1), SCHEMA_VERSION)]
            body = "\n".join(script)
            try:
                self._conn.executescript(
                    f"BEGIN;\n{body}\nPRAGMA user_version = {SCHEMA_VERSION};\nCOMMIT;"
                )
            except sqlite3.Error:
                if self._conn.in_transaction:
                    self._conn.execute("ROLLBACK")
                raise


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
        f"SELECT {_COLUMNS} FROM executions WHERE id = ?",  # noqa: S608
        (execution_id,),
    ).fetchone()
    return None if row is None else _execution_from_row(row)


def _insert_execution(
    conn: sqlite3.Connection, execution: Execution, transition: Transition
) -> None:
    try:
        conn.execute(
            f"INSERT INTO executions ({_COLUMNS}, is_terminal) "  # noqa: S608
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (*_execution_row(execution), int(execution.state.is_terminal)),
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
        conn.execute(
            "UPDATE executions SET state = ?, is_terminal = ?, attempt = ?, version = ?, "
            "updated_at = ?, branch = ?, change_request_url = ?, last_error = ?, "
            "base_revision = ?, candidate_revision = ? WHERE id = ?",
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
        execution_id=_text(execution_id),
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


def _execution_row(execution: Execution) -> tuple[object, ...]:
    return (
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
    ) = row
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
    )


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
