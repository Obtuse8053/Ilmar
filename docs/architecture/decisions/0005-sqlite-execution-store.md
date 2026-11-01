# ADR-0005: SQLite execution store with database-enforced single active execution

- Status: Proposed
- Date: 2026-11-01
- Sources: [R1] · Findings: F-001

## Context and problem

MANIFEST §3 selects SQLite. §5 step 2 requires claiming a task without
duplicate execution. Tracker-side claims (labels, assignees) can race, so the
local store must also refuse a second in-flight execution for the same task.

## Considered options

1. No durable store (R1's in-memory scheduler state).
2. JSON files per execution.
3. SQLite through the standard-library `sqlite3`.
4. A server database such as PostgreSQL.

## Decision

Chosen option: 3. Option 1 loses recovery. Option 2 cannot enforce uniqueness
atomically. Option 4 violates §2.5.

- `STRICT` tables `executions` and `transitions` (the latter append-only).
- A partial unique index on `(task_provider, task_key) WHERE is_terminal = 0`
  allows at most one active execution per task, enforced by the database
  across processes.
- `BEGIN IMMEDIATE` transactions write the record and its transition together.
  `apply` checks the stored version.
- Timestamps are stored as fixed-width UTC ISO-8601 strings, so text order is
  time order.
- WAL mode. The schema version is kept in `PRAGMA user_version`, and a database
  with a newer version is refused.
- `InMemoryExecutionStore` implements the same contract for tests.

## Consequences

- Good: no server, one state file, and duplicate prevention independent of the
  tracker.
- Bad: single host only. Network filesystems are not supported. Requires
  SQLite 3.37 or newer for `STRICT`.

## Revisit when

- Coordinators need to run on more than one host.
- Schema changes such as F-002's `retry_not_before` arrive. Add the first
  numbered migration.
