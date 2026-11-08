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

### 1. Two classes of event, and which provider calls are durable

| | Durable events | Diagnostic events |
| --- | --- | --- |
| What | Everything that changes Ilmar's state or the outside world (rules below) | High-volume, read-only observations that change nothing |
| Where | The store's `events` table, in the **same transaction** as the change | The process's log stream (stderr), as JSON lines when enabled. Never persisted by Ilmar |
| Guarantee | Exactly the committed changes (section 2) | Best effort; lost on a crash |
| May drive decisions | Yes (reconciliation, audit; budgets only as section 5 allows) | **Never** |

Provider calls are classified by the port method called, not case by case:

- **Every mutating port method is durable.** These are the methods the
  architecture test already derives from `ilmar.ports`, such as
  `claim`, `release`, `post_update`, `checkout`, `freeze_candidate`,
  `publish_revision`, `upsert_change_request`, `start`, `run`, `stop`,
  `remove_owned` and `AgentRunner.run`. Each is called only inside a journal
  effect (ADR-0020), so each produces exactly the journal's
  `operation.*` events: one call, one operation, its intent and its outcome.
  A new mutating method cannot be added without a journal operation, so it
  cannot be added without events.
- **A read-only port method is diagnostic** (`provider.read`), with one
  exception: **an observation that decides a durable change is recorded in
  the event of that change, inside the same commit.** Examples:
  - the claim outcome;
  - the remote head seen when settling an interrupted push;
  - the sandbox state `remove_owned` reports;
  - the tracker state a reconciliation acts on (#12).

  So a durable decision is always explained by durable data. A read that
  decides nothing leaves no durable trace.
- **Not a provider call, but durable:** execution transitions, ledger
  changes, evidence, ownership, and the M3 events in section 5.

### 2. Atomicity, and how a missing event is prevented

The rule is **one fenced transaction**: `ExecutionStore.commit(fence,
Commit)` writes the state rows and their events together, or writes nothing.
Missing events are prevented by construction, checked inside the
transaction, and checked again against the history. Each layer covers what
the previous one cannot.

1. **The core cannot omit a mandatory event; it does not write them.**
   `Commit` derives the mandatory events from its own contents, with a pure
   function in `ilmar.domain`:
   - each created execution → `execution.created`;
   - each applied transition → `execution.transitioned`;
   - each operation row → the `operation.*` event for its new status;
   - each resource row → `resource.recorded`, `resource.changed` or
     `resource.quarantined`.

   The caller supplies only the context the rows do not hold, in a typed
   `EventContext`:
   - the pass and the actor;
   - durations;
   - the observations of section 1;
   - the reason code of a failure or refusal.

   The caller may add **extra** events, such as `evidence.recorded` or the
   M3 events, but cannot remove or replace a derived one. A failure
   transition without a reason code gets `unclassified`, so the state change
   is never blocked by a missing code. Every test path asserts that
   `unclassified` never appears, so a missing code is a test failure rather
   than lost history.
2. **The store checks the pairing inside the transaction, before
   `COMMIT`.** It has the previous version of every changed row, so it fills
   each event's `from` status from the stored row. It then verifies:
   - exactly one derived event per changed row, matching that row's key,
     new version and new status;
   - no derived event without a changed row.

   On any mismatch it rolls back and raises `LedgerIntegrityError`. Under
   ADR-0022 that blocks only the execution concerned, and no effect follows.
   This guards against a bug in the derivation, and against a store adapter
   that writes rows some other way.
3. **The history is checked against current state.** Folding the events of
   an execution, in `seq` order, must reproduce its stored state, attempt
   and version, and the status and version of each of its operations and
   resources. The fold starts from the entity's **first event**:
   - its creation event (`execution.created`, `operation.intended`,
     `resource.recorded`) for an entity created after the migration;
   - its **baseline** event for an entity that existed when the events
     table was added (section 8, "Migration baseline").

     The baseline is a snapshot of the stored row at that moment. It is
     not a history: what happened earlier is outside event coverage.
   - This is invariant **I11**. The crash-injection suite checks it after
     every crash point and every recovery (ADR-0022), and the store
     contract checks it after every commit it makes.
   - `ilmar events verify`, a read-only command, runs the same fold for an
     operator.
   - A row changed outside Ilmar, for example by hand in SQLite, shows up
     here as a mismatch. Section 8's triggers additionally refuse any edit
     to the events themselves.

Effects themselves happen between two commits, the intent and the outcome,
exactly as today. Events never decide whether an effect runs. No event is
built or written inside an effect closure. No adapter has store access, so
no adapter writes events.

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
| `actor` | yes | `{"kind": "coordinator"}`; `{"kind": "migration"}` for the baseline (section 8); or `{"kind": "human", "ref": "…"}` for a human command. `ref` is validated as section 7 requires; #59 decides what a resolution records. |
| `type` | yes | `noun.verb`, from the catalogue below. |
| `execution`, `attempt` | when the event concerns an execution | |
| `task` | when known | `provider:key`, never the task's title or body. |
| `operation` | for operation events | The journal key: correlates intent, outcome and reconciliation. |
| `run` | for verification events | The verification run number (ADR-0022, evidence format 2). |
| `reason` | for failures, refusals and decisions | A **stable reason code** (section 4). |
| `message` | optional | Rendered from a registered message template (section 7); never free text. |
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

**Durable, written once by the migration (section 8):**
- `events.coverage_started`;
- `execution.baseline`, `operation.baseline` and `resource.baseline`.

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
- **#18:** `budget.reserved`, `budget.charged`, `budget.released` and
  `budget.exceeded` (reason `budget-exceeded`) are reserved names only.
  **Summing events does not, on its own, enforce a budget safely across
  crashes:**
  - A crash during an effect leaves `operation.intended` with no outcome.
    The work it did, such as model calls during an agent run, may have been
    paid for but has no recorded amount.
  - Usage is often known only to the provider (the model API), not to
    Ilmar. `AgentResult` reports no usage today.
  - A check made before an effect races with usage accrued during it.

  #18 must therefore decide the accounting model, and ADR-0024 does not:
  - reserving before an effect, in the intent commit;
  - settling after it;
  - charging the full reservation for interrupted or ambiguous operations;
  - reconciling with provider-side usage where it is available;
  - keeping a provider-side hard cap as the backstop: the spending-capped key
    (ADR-0023), or a gateway (F-019).

  The events give it durable order and correlation, nothing more.
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

### 7. Validation first, then redaction

Events are protected in three layers, in this order. Redaction is the last
defence, not the mechanism.

1. **Schema-level allowlisting, at construction.** Every event type
   declares its `data` keys, and every field has a type with a validator.
   - **Envelope identifiers** (execution, attempt, task, operation, run,
     instance, pass) come from the domain's typed values, and are checked
     against the same patterns those values already obey (ADR-0012 naming,
     the journal key format).
   - **`data` values** must be one of:
     - an integer, or a bounded enum member;
     - a reason code from the registry;
     - an identifier matching a declared pattern: a resource name, a branch
       name, a 40-hex or 64-hex digest, or a change-request URL that is
       `https`, on the configured provider host, without user info, query
       or fragment.

     There are **no free-form strings in `data`.** An unknown key, a wrong
     type or a value outside its pattern is refused.
   - **`message`** is a template ID from a registry of fixed sentences,
     rendered with typed parameters from the same validated set. No string
     reaches an event from a provider's stderr, an exception message, the
     agent, the tracker or a workspace.
   - **Excluded entirely:** credentials (ADR-0006, ADR-0023), environment
     values, command arguments, task titles and bodies, agent output and
     summaries (ADR-0010), file contents and paths inside workspaces.
     The agent's summary stays in the attempt and evidence records, for
     #14. Implementing this ADR removes the summary quote from the event
     form of the failure reason; `last_error` keeps its text until #14
     decides.
2. **Human-supplied values are the strictest case:** `actor.ref`, and any
   reason a human types for #59.
   - `actor.ref` must match `^[a-z][a-z0-9-]{0,31}$`: a short lower-case
     label such as `maintainer` or `ops-1`. It is not an email address, a
     login or a token. It rejects `@`, `:`, `/`, `_`, upper case and
     anything longer, which excludes every credential shape listed below.
   - A human's free-text reason, if #59 keeps one, is stored in #59's own
     resolution record, not in the event. The event carries the record's
     digest.
   - A human command whose value fails validation is **refused** with a
     message. It is never redacted and written: a person is there to
     correct it.
3. **Credential-shape scan, as defence in depth.** After validation, every
   string in the serialised event is checked against credential shapes, the
   patterns the live harness uses (`github_pat_`, `gh?_`, `sk-ant-`,
   `x-access-token:`, and their URL-encoded and base64 forms). Validation
   should make a match impossible, so a match is a bug:
   - in a coordinator commit, the field is replaced by `"<redacted>"` and
     the event flagged `"redacted": true`. The commit is not blocked;
   - tests assert that the flag never appears on any test path, and the
     live run asserts that no event was ever redacted.

**Failure handling:**
- An invalid optional field, or invalid `data`, in a coordinator event:
  - the field is dropped and the event flagged `"invalid": true`;
  - the state change still commits;
  - tests assert that the flag never appears.
- An invalid **envelope identifier** can only come from a broken invariant,
  since those come from validated domain types. The commit is refused
  (`LedgerIntegrityError`) and blocks that execution alone (ADR-0022),
  rather than writing an event that cannot be attributed.
- **Size limits:** a rendered `message` is at most 300 characters, and
  `data` at most 4 KiB serialised. The templates and validators make longer
  values impossible; a test checks every template at its maximum
  parameters.

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
  - the migration baseline below, in the **same transaction**. A crash
    during the migration leaves the store exactly as it was, and the next
    open migrates again from the start.
- **No backfill.** No past event is reconstructed or invented. Earlier
  history stays where it is today, in `transitions`, `operations` and
  `evidence`.

#### Migration baseline

Executions, operations and resources that exist when the events table is
added have no creation event. Without a starting point, I11 could not be
checked for them, and could not tell old history apart from a missing
event. The migration therefore records **what exists now**, not what
happened before:

1. **`events.coverage_started`** is the first event (`seq` 1).
   - Its `data` holds the schema version migrated from, and the number of
     executions, operations and resources baselined.
   - Every later event is "covered".
2. **One baseline event per existing row**, all in `seq` order after the
   marker:
   - **`execution.baseline`:**
     - the stored state, attempt and version;
     - the number of `transitions` rows, and of `evidence` rows, the
       execution has at that moment (`transitions_before`,
       `evidence_before`).

     These counts bound exactly what is historical.
   - **`operation.baseline`:** key, kind, attempt, status and version. This
     includes `intended` and `ambiguous` operations that a crashed
     coordinator left unfinished.
   - **`resource.baseline`:** key, kind, name, attempt, status and version.
     This includes `quarantined` rows.

   Terminal executions are baselined too: some still change later, for
   example cleanup, a claim release after a merge, or #59.

   The baseline does not go through `Commit`, so the migration makes its
   own check before `COMMIT`: exactly one baseline event per existing
   execution, operation and resource, and counts equal to the marker's.
   Otherwise it rolls back.
3. **Baseline events carry `actor` `{"kind": "migration"}`.** Their `epoch`
   is the one in the store's owner row at that moment; a store never owned
   has no executions and so no baseline. They carry no `pass` and no
   `reason`. They are validated like every event (section 7). A baseline
   holds no free text: the stored `last_error` and transition reasons are
   not copied.
4. **The migration runs only under the instance lock.**
   - The store applies it only when opened for writing by a caller that
     holds the lock: the coordinator, or a mutating command, after
     `Ownership` has taken the lock. No coordinator can therefore write
     between the snapshot and the first covered event.
   - Today the store migrates every time it is opened. The implementation
     separates the two: a **read-only open never migrates and never
     writes**. `ilmar events` and `ilmar events verify` use it.
   - On a store without the events table they report "no event coverage"
     (exit 2) and change nothing.
5. **After the baseline, everything is ordinary.**
   - The next change to a baselined row is paired as in section 2: its
     `from` is the stored status, which equals the baseline's.
   - Recovery of a baselined, interrupted operation writes
     `operation.reconciled` from the baseline's `intended` or `ambiguous`
     status, exactly as for any other.

#### What `ilmar events verify` reports

`verify` is read-only. It folds every entity from its first event and
compares the result with the stored rows. Each finding is in one of two
classes, never mixed.

- **Historical (information, exit 0).** These are data outside event
  coverage by design, bounded by the baseline:
  - `transitions` and `evidence` rows that the baseline counted;
  - the creation and earlier status changes of a baselined operation or
    resource.

  They are reported per execution, for example "baselined at seq 17: 6
  transitions and 2 evidence records before coverage".
- **Integrity failure (exit 1).** Any of:
  - a stored row whose fold disagrees with it (state, attempt, version or
    status);
  - an entity with **neither** a creation nor a baseline event. Under the
    lock-held migration this cannot happen, so it is never classified as
    historical;
  - more `transitions` or `evidence` rows than the baseline count plus the
    covered events account for, or fewer;
  - an event whose entity does not exist;
  - a second `events.coverage_started`, or a baseline event after covered
    events of the same entity;
  - a `seq` gap. Since append-only triggers refuse deletion, a gap means
    the file was altered outside Ilmar.

  Each failure names the entity and the check that failed.
- **No coverage (exit 2):** the store has no events table yet.

The classification never uses timestamps. Whether a row is historical is
decided only by the baseline's explicit entities and counts. That keeps the
rule of ADR-0022: no decision from persisted wall-clock time.
- **Existing records keep their meaning:** `transitions`, `last_error` and
  `WorkerResult.detail` stay as they are. Events carry the reason code;
  human-readable text stays where it is.
- **Retention:** events are never deleted in M3. Growth is linear in work
  done, a few rows per operation.

### 9. Port and contract changes (for the implementation PR)

- **`ilmar.domain`:**
  - `Event`, `EventType`, the per-type `data` schemas and validators;
  - `EventContext`;
  - the reason-code registry and the message-template registry;
  - the pure derivation of mandatory events from a `Commit`;
  - `Commit.extra_events`;
  - the I11 fold.
- **`ExecutionStore`:**
  - `commit` derives, pairs (section 2, step 2) and appends events in the
    same transaction;
  - a new read method, `events(*, after_seq=0, limit=…) -> Sequence[Event]`.

  The contract suite covers:
  - atomicity with the rest of the commit;
  - the pairing check: an unpaired row and an unpaired event are each
    refused;
  - refusal under a stale fence;
  - append-only behaviour;
  - `seq` order;
  - round-trip of every field;
  - I11 after every commit it makes.
- **`ilmar events` and `ilmar events verify` (CLI),** both read-only.
  They use the store's read-only open, which never migrates (section 8).
- **`SQLiteExecutionStore`:** a read-only open, and a lock-held open that
  applies the events migration with its baseline in one transaction.
- **Migration baseline tests,** on a store built with today's schema
  (version before the migration). The store holds:
  - non-terminal executions in every non-terminal state;
  - one with an `intended` and one with an `ambiguous` operation, left by a
    crash;
  - active, releasing and quarantined resources;
  - a `SUCCEEDED` execution with its claim held, and a `FAILED` one.

  The tests:
  - **Migration under the lock** writes exactly one
    `events.coverage_started` and one baseline per row, with matching
    counts. I11 holds, and `verify` exits 0 with every pre-existing
    execution reported as baselined, with its historical counts.
  - **Recovery after the migration** runs on that store, with a crash
    injected at every store write and before every effect, as in the crash
    suite. After each crash and recovery, I11 holds and `verify` exits 0.
    The baselined `intended` and `ambiguous` operations are reconciled from
    their baseline status.
  - **A crash inside the migration** leaves the store unchanged: no events
    table and no partial baseline. The next open migrates once.
  - **Opening without the lock, and read-only opens, never migrate.**
    `ilmar events` and `verify` on an unmigrated store exit 2 and leave the
    database file byte-for-byte unchanged.
  - **`verify`'s classes:**
    - pre-baseline `transitions` and `evidence` are historical (exit 0);
    - each integrity failure listed in section 8 is detected (exit 1),
      each by editing the database outside Ilmar: a row changed without an
      event, an extra transition after the baseline, an execution inserted
      with no events, a removed trigger and a deleted event (a `seq` gap);
    - a historical finding is never reported as a failure, and no failure
      as historical.
- **Architecture tests:**
  - events are constructed only in `ilmar.core` and `ilmar.domain`;
  - no adapter imports the event types;
  - no effect closure builds or writes an event;
  - every mutating port method maps to a journal operation kind, and so to
    its events.
- **Tests that `unclassified`, `"redacted"` and `"invalid"` never occur,**
  across the unit, crash-injection and Podman suites.

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
  history. The pairing check and I11 (section 2) make them agree.
- **Bad:** reason codes and message templates add registries to maintain.
  Tests catch an error class with no code, and a failure with no template.
- **Bad:** typed `data` schemas make adding a field a code change, not a
  string edit. That is the point: nothing reaches an event unvalidated.
- **Not solved here:** safe budget enforcement across crashes. Section 5
  reserves it for #18.
- **Bad:** the migration writes one event per existing execution, operation
  and resource in one transaction. Its size is bounded by the store, and it
  runs once.
- **Bad:** the store's open changes. Today every open migrates; afterwards a
  read-only open never migrates, and the events migration needs the
  instance lock. Callers that open the store before taking the lock must be
  reordered.
- **Good:** history before the migration is never mistaken for corruption,
  and corruption is never excused as history. The baseline bounds the
  historical part by explicit entities and counts, not by timestamps.

## Revisit when

- The `events` table grows to a size where retention or compaction is
  needed.
- Several coordinators share a store or run on several hosts: `seq` is then
  no longer one order.
- An external telemetry exporter is wanted: add it as an optional adapter
  over `events()`.
- #14, #16, #12, #13, #18 or #59 needs a field that does not fit
  section 3's envelope.
- A second store adapter is added: it must implement the same baseline and
  the read-only open, or this ADR must say how its history starts.
