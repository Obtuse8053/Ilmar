# ADR-0024: A structured event log, written in the same commit as the change it describes

- Status: Proposed
- Date: 2026-11-08
- Sources: none external · Findings: F-009
- Implements: #17 (schema only; implementation follows approval). Prepares
  #16, #12, #13, #18, #59. Builds on ADR-0003, ADR-0005, ADR-0010, ADR-0012,
  ADR-0020, ADR-0021, ADR-0022

## Context and problem

#17 asks for machine-readable events for every transition and provider call:
JSON lines carrying the execution ID, with a stable reason code. Several
upcoming features read these events or write their own:
- cancellation (#16);
- reconciliation with the tracker, including the release of orphaned claims
  (#12);
- backoff and stall detection (#13);
- budgets (#18);
- human resolution of quarantines (#59).

What exists today:

- **Durable records in the store** (SQLite, ADR-0005):
  - `transitions`: append-only, one row per state change, with a free-text
    `reason`;
  - `evidence`: append-only (ADR-0010);
  - `owner_history`;
  - the journal `operations` and the ledger `resources` (ADR-0020). These are
    **updated in place**: a row's status moves from `intended` to
    `succeeded`, or from `active` to `quarantined`, and the earlier status
    is gone. So the history of what Ilmar did to the outside world is not
    kept anywhere.
- **Every durable change goes through `ExecutionStore.commit(fence,
  Commit)`:** one fenced transaction. A process that has lost ownership
  cannot write (ADR-0022, invariant I7).
- **Python `logging`:** five calls, for humans (four in the Podman adapter,
  one in the coordinator). These are not structured, not persisted, and
  carry no IDs.
- **Failure text is free-form:** `last_error`, transition reasons and
  `WorkerResult.detail` are sentences. Some include untrusted text: the agent
  failure reason quotes up to 200 characters of the agent's own summary
  (`worker.py`), and adapter errors quote the last line of a tool's stderr.

A second, separate log written next to these records would duplicate them.
Written by a second write path, it would also disagree with them after a
crash. Events must not become a new side effect inside the fenced effect
path either: they must never decide whether an effect happens.

## Considered options

1. **Structured Python logging only:** a JSON formatter on the existing
   logger. Simple. But nothing is durable: lines are lost on a crash or when
   no one collects stderr, so they cannot drive budgets, reconciliation or
   an audit of a quarantine resolution.
2. **A JSONL file appended by the core, next to the database.** These are
   two writes that are not atomic. After a crash, events would be missing
   for committed changes, or present for changes that never committed. It
   would also need fsync policy, rotation and file locking in the core.
3. **Derive events from the existing tables with no new storage.**
   Operations and resources are updated in place, so their history cannot be
   reconstructed.
4. **An append-only `events` table written in the same `commit` as the
   change it describes, with JSON lines as a read-only projection of it,**
   plus non-durable diagnostics for high-volume reads. This is the
   transactional outbox pattern.
5. **An external telemetry stack** (an OpenTelemetry SDK, a log service).
   The core may have no runtime dependencies (ADR-0003). An optional exporter
   adapter could map option 4's events to such a stack later.

## Decision

Chosen option: 4. Events are part of the durable state. They are committed
atomically with the change they record, are never written by a separate
path, and are exported as JSON lines by a read-only command.

### 1. Two classes of event

| | Durable events | Diagnostic events |
| --- | --- | --- |
| What | Everything that changes state or touches the outside world: execution transitions, every journaled operation and its outcome, ledger status changes, evidence, ownership, and the M3 events below | High-volume, read-only observations: provider read calls (`list_eligible`, `get`, `find`, `load_candidate`, branch heads), pass start and end, agent heartbeats |
| Where | The store's `events` table, in the **same transaction** as the change | The process's log stream (stderr), as JSON lines when enabled. Never persisted by Ilmar |
| Guarantee | Exactly the committed changes: no event without its change, and no change without its event | Best effort; lost on a crash |
| May drive decisions | Yes (budgets, reconciliation, audit) | **Never** |

### 2. Where durable events come from

- **`Commit` gains `events: tuple[Event, ...]`.** `ExecutionStore.commit`
  appends them in the same transaction, under the same fence. A fenced-out
  process therefore cannot write events either (I7).
- **The core builds events when it builds a commit,** from data it already
  has:
  - the transition: from, to, reason code;
  - the operation: kind, step, status, duration;
  - the resource: kind, name, from, to.

  No event needs an extra read, write or commit. No event is ever written
  inside an effect closure. **No adapter writes events:** adapters have no
  store access. They report outcomes, and the core records them.
- **The journal emits events from its existing commits:**
  - `operation.intended` with the intent commit;
  - `operation.succeeded`, `operation.failed` or `operation.ambiguous` with
    the outcome commit;
  - `operation.reconciled` when recovery settles one (ADR-0022).

  So an effect's events bracket it exactly, across crashes.
- **Ledger and transition events** are written by the commit that changes
  the row. A test enforces this: every commit that changes an execution,
  operation or resource also carries the matching events, so none can be
  forgotten.

### 3. The envelope (format `ilmar.event/1`)

One JSON object per line, UTF-8, keys sorted, no insignificant whitespace,
newline-terminated:

```json
{"schema":"ilmar.event/1","seq":1234,"at":"2026-11-08T11:02:03.456789Z",
 "instance":"ilmar-main","epoch":3,"pass":"p-6f1c2a","actor":{"kind":"coordinator"},
 "type":"operation.failed","execution":"x18b127e5c46c","attempt":1,
 "task":"github:Obtuse8053/ilmar-disposable#6","operation":"start-container:x18b127e5c46c:1:",
 "reason":"resource-ownership-conflict","message":"the sandbox is not owned by this execution",
 "data":{"kind":"start-container","duration_ms":412,"error_class":"OwnershipConflictError"}}
```

(Wrapped here for reading; on disk it is one line.)

| Field | Required | Meaning |
| --- | --- | --- |
| `schema` | yes | `ilmar.event/1`. Consumers must check it (see 8). |
| `seq` | yes | Position in this store's single total order: an integer that only ever increases, assigned by the store when it commits. |
| `at` | yes | RFC 3339, UTC, microseconds, from the process's `NonDecreasingClock` (ADR-0022). It is for humans and correlation only: **no decision compares it with the current time.** |
| `instance` | yes | The Ilmar instance ID (ADR-0013). |
| `epoch` | yes | The ownership epoch of the writing process, so events from before and after a takeover are distinguishable (ADR-0020). |
| `pass` | yes for coordinator events | A random ID per `run_once` pass. It correlates everything one pass did, recovery included. |
| `actor` | yes | `{"kind": "coordinator"}`, or `{"kind": "human", "ref": "…"}` for a human command. `ref` is a label the human supplies, never an account credential. #59 decides what a resolution records. |
| `type` | yes | `noun.verb`, from the catalogue below. |
| `execution`, `attempt` | when the event concerns an execution | |
| `task` | when known | `provider:key`, never the task's title or body. |
| `operation` | for operation events | The journal key: correlates intent, outcome and reconciliation. |
| `run` | for verification events | The verification run number (ADR-0022, evidence format 2). |
| `reason` | for failures, refusals and decisions | A **stable reason code** (section 4). |
| `message` | optional | One sentence written by Ilmar, at most 300 characters (see 7). |
| `data` | optional | A type-specific object, at most 4 KiB serialised. |

**Durations** (`duration_ms`) are measured with `time.monotonic` around the
effect, and recorded as a value. Like every deadline, they are never derived
from two `at` values (ADR-0022).

### 4. Stable reason codes

- **Form:** lower-case, hyphenated, fixed forever once released. A code is
  never reused with another meaning. Codes are added, never renamed.
  Consumers treat an unknown code as "other" for its event type.
- **Where they live:** one registry module in `ilmar.domain`, which maps
  each code to a description and to the error classes or decisions that
  produce it.
- **Tests:**
  - every class in `NEVER_TRANSIENT` and every `ClaimOutcome` has a code;
  - every failure or refusal event carries a known code;
  - every code in the registry is produced somewhere;
  - specifically: an ownership conflict on start, on removal and on a
    directory each yields `resource-ownership-conflict` (moved from #15).

The initial set:

| Code | Produced by |
| --- | --- |
| `resource-ownership-conflict` | `OwnershipConflictError`: owner labels, ledger identity or a directory that does not match (ADR-0012 rule 3) |
| `containment-unconfirmed` | `ContainmentError`: removal of a sandbox part could not be confirmed (ADR-0014, ADR-0015) |
| `policy-violation` | `PolicyViolationError` (for example a cross-origin redirect) |
| `candidate-rejected` | `CandidateRejectedError`, or a candidate that no longer matches its freeze |
| `ledger-integrity` | `LedgerIntegrityError` |
| `fenced-out` | `FencingError`: this process lost ownership |
| `branch-moved` | A remote head that is neither the expected head nor the candidate (ADR-0022 decision 5) |
| `push-rejected` | `NonFastForwardError` or `LeaseConflictError` while publishing |
| `evidence-policy-mismatch` | Evidence recorded under another verification policy (ADR-0022) |
| `interrupted` | A step abandoned by recovery (`ABANDONED`) |
| `agent-failed`, `agent-timed-out` | The agent's recorded outcome |
| `verification-failed` | A verification run that did not pass |
| `attempts-exhausted` | No attempt left |
| `transient-provider-error` | A retried `ProviderError` |
| `not-claimed-other`, `not-claimed-ineligible` | `ClaimOutcome.CLAIMED_BY_OTHER` and `NOT_ELIGIBLE` (the latter also when a human claim label is present, ADR-0013) |
| `change-request-merged`, `change-request-closed` | Task completion (ADR-0021) |
| `unexpected-error` | An exception caught at an execution's boundary (ADR-0022) |

### 5. Event catalogue (version 1)

**Durable, from M2 behaviour:**
- `execution.created`, and `execution.transitioned` (`data`: `from`, `to`);
- `operation.intended`, `operation.succeeded`, `operation.failed`,
  `operation.ambiguous`, `operation.reconciled`. Their `data` holds `kind`,
  `step`, `duration_ms`, `error_class`, and for reconciliation `observed`;
- `resource.recorded`, `resource.changed` (`data`: `kind`, `name`, `from`,
  `to`), and `resource.quarantined` (always with a `reason`);
- `evidence.recorded` (`data`: `run`, `digest`, `outcome`, `policy_digest`);
- `ownership.acquired` (`data`: `previous_instance` and `previous_epoch`, when one existed). There is no "lost" event: a process that has lost ownership is fenced and cannot write, so the new owner records the takeover.

**Reserved for M3:** each issue defines its event's `data`, within this
envelope.
- **#16:**
  - `execution.cancel_requested` (`actor`, reason `cancel-requested` or
    `task-ineligible`);
  - `execution.cancelled`.
- **#12:**
  - `task.reconciled` (`data`: the tracker state observed and the decision
    taken);
  - `claim.released` (reason `orphaned-claim` or `task-completed`);
  - `claim.relabelled`.
- **#13:**
  - `retry.scheduled` (`data`: the backoff delay in seconds). How a wait
    survives a restart is #13's decision. ADR-0022 forbids deciding from
    persisted wall-clock timestamps, so `at` is never compared with the
    current time;
  - `agent.stalled` (reason `agent-stalled`, `data`: the silence
    measured with the monotonic clock).
- **#18:**
  - `budget.charged` (`data`: `kind`: `agent_seconds`,
    `verification_seconds` or later `cost`, and its `amount`);
  - `budget.exceeded` (reason `budget-exceeded`).

  Charges are summed from durable events, so a budget survives restarts and
  counts every attempt.
- **#59:** `quarantine.resolved` (actor `human`, `data`: what was observed).
  It is a new append-only record. ADR-0020's rule that quarantined rows never
  change stays intact.

**Diagnostic:** `provider.read`, `pass.started`, `pass.finished` and
`agent.heartbeat`. `agent.heartbeat` is a liveness signal for humans. #13
decides stalls in process, and records only the decision, durably.

### 6. Ordering, crashes and restarts

- **Order:** `seq` gives one total order per store, so per instance.
  - Within an execution, `seq` order is causal order, because one lease
    owns it (ADR-0012 level 2).
  - Across instances there is no global order. Consumers order by
    `(instance, seq)`.
- **Atomicity:** an event and its change commit together or not at all.
  - A crash between an intent commit and its outcome leaves
    `operation.intended` with no outcome. That is exactly the journal's
    state, and recovery's `operation.reconciled` completes it.
  - Nothing is re-emitted on restart: recovery writes events only for what
    it changes now.
- **Fencing:** a takeover shows as the new owner's `ownership.acquired`, with
  a higher `epoch` and the previous one in `data`. A fenced-out process
  writes nothing (I7). Its last events are the last ones it committed
  before losing ownership.
- **Export is at-least-once:** `ilmar events --after SEQ [--follow]`
  prints JSON lines in `seq` order. It is read-only and needs no instance
  lock (ADR-0012).
  - A consumer keeps its own cursor and deduplicates by
    `(instance, seq)`.
  - The exporter changes nothing, so a crashed export is simply re-run.
  - No push sink or network exporter is in the core.

### 7. Redaction and untrusted text

- **Excluded from events:**
  - credentials (ADR-0006, ADR-0023);
  - environment values;
  - command arguments carrying values;
  - task titles and bodies;
  - agent output and summaries, which are untrusted (ADR-0010);
  - workspace file contents;
  - raw tool stderr.
- **Agent summaries:** the agent's summary stays in the attempt and evidence
  records, for #14. The event carries `agent-failed` and the evidence digest.
  Implementing this ADR therefore removes the agent summary quote from the
  failure reason it puts into events. `last_error` keeps its current text
  until #14 decides.
- **`message`** is written by Ilmar from fixed text and identifiers only: no
  interpolated provider stderr or agent text.
- **Defence in depth:** before a commit, every string field is checked
  against credential shapes, the same patterns the live harness uses
  (`github_pat_`, `gh?_`, `sk-ant-`, `x-access-token:`, and their URL and
  base64 forms).
  - A match replaces the field with `"<redacted>"`, and sets
    `"redacted": true` on the event.
  - It never fails the commit, because events must not block state changes.
  - A test asserts the replacement, and a live-run check asserts that no
    event was ever redacted.
- **Size limits:** `message` is capped at 300 characters and `data` at
  4 KiB. Anything longer is truncated and marked with `"truncated": true`,
  never dropped.

### 8. Compatibility and versioning

- **Within `ilmar.event/1`, changes are only additive:**
  - new optional fields;
  - new event types;
  - new reason codes;
  - new `data` keys.

  Consumers must ignore unknown fields and types.
- **A breaking change gets a new `schema` value** (`ilmar.event/2`): a
  removal, a rename, a type change, or a changed meaning. It needs an ADR,
  and the exporter can still emit the old version during a transition.
- **Store migration:**
  - one migration adds the `events` table, append-only, with triggers that
    refuse `UPDATE` and `DELETE` as for `evidence`;
  - an index on `(execution_id, seq)`;
  - a first `ownership.acquired` event.
- **No backfill:** events start at the migration. Earlier history remains in
  `transitions`, `operations` and `evidence`, and the exporter can label
  that boundary.
- **Existing records keep their meaning:** `transitions`, `last_error` and
  `WorkerResult.detail` stay as they are. Events carry the reason code;
  human-readable text stays where it is.
- **Retention:** events are never deleted in M3. Growth is linear in work
  done, a few rows per operation.

### 9. Port and contract changes (for the implementation PR)

- `ilmar.domain`:
  - `Event` (frozen dataclass), `EventType`;
  - the reason-code registry;
  - `Commit.events`.
- `ExecutionStore`:
  - `commit` appends `Commit.events` in the same transaction;
  - a new read method, `events(*, after_seq=0, limit=…) -> Sequence[Event]`.

  The contract suite covers:
  - atomicity with the rest of the commit;
  - refusal under a stale fence;
  - append-only behaviour;
  - `seq` order;
  - round-trip of every field.
- `ilmar events` (CLI), read-only.
- **Architecture tests:**
  - events are constructed only in `ilmar.core` and `ilmar.domain`;
  - no adapter imports the event types for writing;
  - no effect closure builds or writes an event.

## Consequences

- **Good:** budgets, reconciliation, cancellation, stall decisions and
  quarantine resolutions get a durable, ordered, crash-consistent history,
  with stable codes to act on and audit. They come from one write path
  that already exists, under the same fencing.
- **Good:** the history of operations and resources, today lost to in-place
  updates, is kept.
- **Good:** no new runtime dependency, service or file format in the core.
- **Bad:** every commit writes a few more rows, and the database grows with
  work done. Acceptable for M3; see "Revisit when".
- **Bad:** `Commit` and `ExecutionStore` change: a port change, with
  contract tests for every store adapter.
- **Bad:** some data exists twice. A transition is in `transitions` and in
  its event; an operation's final status is in `operations` and in its last
  event. The tables stay authoritative for current state, and events for
  history. A test checks that they agree.
- **Bad:** reason codes add a mapping to maintain. Tests catch an error class
  with no code.

## Revisit when

- The `events` table grows to a size where retention or compaction is
  needed.
- Several coordinators share a store or run on several hosts: `seq` is then
  no longer one order.
- An external telemetry exporter is wanted: add it as an optional adapter
  over `events()`.
- #14, #16, #12, #13, #18 or #59 needs a field that does not fit
  section 3's envelope.
