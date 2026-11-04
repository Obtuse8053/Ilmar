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
"""

from __future__ import annotations

import sqlite3
import threading
from collections.abc import Collection, Iterator, Sequence
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
)
from ilmar.domain.execution import Execution, ExecutionState, Transition
from ilmar.domain.tasks import TaskRef
from ilmar.domain.verification import EvidenceRecord
from ilmar.ports.execution_store import check_apply, check_create

SCHEMA_VERSION = 2

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

    def create(self, execution: Execution, transition: Transition) -> None:
        check_create(execution, transition)
        with self._transaction() as conn:
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

    def apply(self, updated: Execution, transition: Transition) -> None:
        check_apply(updated, transition)
        with self._transaction() as conn:
            row = conn.execute(
                f"SELECT {_COLUMNS} FROM executions WHERE id = ?",  # noqa: S608
                (updated.id,),
            ).fetchone()
            if row is None:
                raise ExecutionNotFoundError(updated.id)
            current = _execution_from_row(row)
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

    def append_evidence(self, record: EvidenceRecord) -> None:
        with self._transaction() as conn:
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
