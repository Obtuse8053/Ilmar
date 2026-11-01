# ADR-0005: SQLite execution store with database-enforced single active execution

- Status: Proposed
- Date: 2026-11-01

## Context

MANIFEST §3 selects SQLite for execution persistence, and §5 step 2 requires
claiming a task without duplicate execution. The tracker-side claim (for
example a label or assignee on GitHub) can race and cannot be trusted alone. The
local store must also refuse to start a second execution for a task that already
has one in flight.

## Decision

`ilmar.adapters.sqlite.SQLiteExecutionStore` uses the standard-library
`sqlite3` module:

- An `executions` table, one row per execution, and an append-only
  `transitions` table. Both are `STRICT` tables.
- A **partial unique index** on `(task_provider, task_key) WHERE is_terminal = 0`.
  The database itself guarantees at most one active execution per task, even
  across processes. `is_terminal` is stored explicitly so the index does not
  depend on state names.
- Writes run in `BEGIN IMMEDIATE` transactions. The execution row and its
  transition are committed together.
- `apply` checks the stored version and state before updating (ADR-0004) and
  raises `ConcurrencyConflictError` on a mismatch.
- Timestamps are stored as UTC ISO-8601 strings with fixed microsecond
  precision, so text order equals time order. Domain timestamps must be
  timezone-aware.
- WAL journal mode for file databases. Foreign keys on.
- The schema version is kept in `PRAGMA user_version`. A database with a newer
  version than the code supports is refused rather than misread. Migrations
  will be added as numbered steps in `_migrate`.

`ilmar.adapters.memory.InMemoryExecutionStore` implements the same contract for
tests and dry runs.

## Consequences

- No server to run. The state is a single file under `state_dir`.
- Single-host operation is the design point. Multiple coordinators on one host
  are safe; several hosts sharing a network filesystem are not supported.
- SQLite 3.37 or newer is required for `STRICT` tables. This is satisfied by the
  sqlite bundled with supported Python builds.
- The contract suite runs against both the on-disk and in-memory SQLite modes
  and the in-memory store, including a concurrent-create race test.

## Prior art

Standard SQLite features. No external code consulted.
