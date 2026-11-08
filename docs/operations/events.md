# Reading and verifying the event log

ADR-0024 defines the event log: every change to the execution store is
recorded, in the same transaction, as an event. Two commands read it. Both
are **strictly read-only**: they open the store in SQLite's read-only mode,
which never migrates, never writes and never creates a file. They take no
instance lock, so they can run while a coordinator is working.

Name the store with `--config ilmar.toml` (only its execution store is read;
nothing else is planned, created or needs to be installed) or with
`--db path/to/executions.sqlite3`.

## `ilmar events`: export

```sh
ilmar events --config ilmar.toml                       # every event, JSON lines
ilmar events --config ilmar.toml --after 1200          # after a consumer's cursor
ilmar events --config ilmar.toml --follow              # keep printing new events
ilmar events --db executions.sqlite3 --format text --execution x18b2c4d6e8f0
ilmar events --db executions.sqlite3 --type operation.failed --reason push-rejected
```

- **Output:** each line is the event exactly as stored, in `seq` order.
  `--format text` prints one readable line per event instead.
- **Filters:** they combine, and options given more than once are alternatives.
  - `--execution ID`;
  - `--type` with an exact type (`operation.reconciled`) or a family
    (`operation`);
  - `--reason CODE`;
  - `--after SEQ` and `--limit N`.
- **Export is at-least-once.** A consumer keeps its own cursor (the last `seq`
  it processed) and deduplicates by `(instance, seq)`. The exporter changes
  nothing, so a crashed export is simply run again with `--after`.
- **A corrupt event** stops the export at that `seq`. Everything before it has
  been printed. Run `verify` to see what is wrong.

## `ilmar events verify`: invariant I11

```sh
ilmar events verify --config ilmar.toml
ilmar events verify --db executions.sqlite3 --format json
ilmar events verify --db executions.sqlite3 --failures-only
```

It folds every entity from its first event and compares the result with the
stored rows. Each finding is in exactly one of two classes:

- **Historical (information).** These are data from before event coverage
  started, bounded by the migration baseline. For example: "6 transitions
  and 2 evidence records before coverage".
- **Integrity failure.** The rows and the events disagree:
  - a fold mismatch;
  - an entity with no events;
  - counts that do not add up;
  - an event for nothing;
  - a misplaced baseline;
  - a `seq` gap;
  - a corrupt line.

  Since events are append-only, a failure means the database was changed
  outside Ilmar. Stop coordinators and investigate before any further run.

`--format json` prints one object with `store`, `covered`, `events`,
`exit_code`, `result` (`ok`, `integrity-failure` or `no-coverage`),
`historical` and `failures`. Each finding has `check`, `subject` and `detail`.

## Exit codes

| Code | `ilmar events` | `ilmar events verify` |
| --- | --- | --- |
| 0 | events printed | no integrity failure (historical findings are fine) |
| 1 | a corrupt event | an integrity failure |
| 2 | no event coverage yet | no event coverage yet |
| 3 | configuration or usage problem, or the store cannot be read | the same |

"No event coverage" means the store has no event log yet, for example a store
from before ADR-0024 that no coordinator has claimed since. Coverage starts at
the next `ilmar run --once`, which migrates the store under the instance lock.
Neither command changes anything.
