# ADR-0045: Narrow event reads, filtered before decoding

- Status: Proposed
- Date: 2026-11-16
- Sources: ADR-0024 (the event log), ADR-0039, ADR-0041 · Findings: none new
- Issue: #147, found by the fake-provider soak (#148)

## Context and problem

Some per-pass reads decoded the **whole event log** to find a few events:

- the trial's usage (`cli_loop.trial_use`), on every check of the loop;
- an instance-scoped operation's intent (`Coordinator._intent_event`);
- unreviewed spend (`ilmar resume` and `ilmar budget`).

The in-memory store also decoded every event to answer a per-execution
read. Pass cost therefore grew with history, without bound, which no
unattended mode can accept. In the soak, 30 restarts took 116 s, then 150 s,
then 273 s, as the history grew.

## Considered options

1. **Cache counts in the loop process.** Fast, but a restart rebuilds the
   cache with a full scan, and a cache is one more thing that can drift from
   the log.
2. **New derived tables (usage, intents) kept in the same transaction.** Fast,
   but it changes the persistence format and needs a migration.
3. **Optional filters on `events()`, applied before decoding (chosen).** The
   SQLite table already has `type` and an `(execution_id, seq)` index, so
   this needs no migration. It is additive: every existing call keeps its
   meaning.

## Decision

- **`ExecutionStore.events()` gains two optional filters:**
  - `types`: only events of those types;
  - `instance_scoped`: only events naming no execution.

  It cannot be combined with `execution_id`.
- **Filtering happens before decoding.** SQLite filters in its query. The
  in-memory store keeps each line's execution and type, and a per-execution
  index. A narrow read decodes only the events it returns.
- **The callers read narrowly:**
  - trial usage: the counted types, after the trial's own record;
  - an intent: the operation's own execution, or the instance-scoped events;
  - unreviewed spend: its two types.
- **Unchanged:**
  - the hash chain is verified over the whole log (`ilmar events verify`,
    recovery), never through a filter;
  - the stored format, every event and every existing result.

## Consequences

- Good: per-pass reads cost the same whatever the history's length. A test
  shows it on both stores, with ten times the history.
- Good: no migration, and old databases work unchanged.
- Bad: SQLite's type filter has no index, so it scans rows without decoding
  them. If that ever shows in a profile, an index on `(type, seq)` is a small,
  separate migration.

## Tests required

- **Contract:** every combination of the filters equals the whole log
  filtered, on every store.
- **Cost:** decodes for each narrow read stay flat while the history grows
  tenfold; a full read still decodes everything.
- **Callers:** trial usage asks only for its own types after its record.

## Revisit when

A per-pass read appears that cannot be expressed with these filters.
