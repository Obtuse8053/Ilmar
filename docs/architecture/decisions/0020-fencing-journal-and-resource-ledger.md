# ADR-0020: Fencing, the write-ahead operation journal and the resource ledger

- Status: Proposed
- Date: 2026-11-05
- Sources: [R1], [R3] · Findings: F-004
- Implements: #11a; ADR-0012 rules 1 to 4 (fencing, write-ahead,
  idempotency, the resource ledger); ADR-0010's "what is published is exactly
  what was verified"

## Context and problem

The coordinator (#11b) performs external side effects: claim, start
containers, freeze, push, open change requests. Each can be interrupted by a
crash, and a coordinator can lose ownership while it runs. ADR-0012 decided
the principles: one owner per instance, fencing, write-ahead intent,
idempotent or checkable effects, and a resource ledger. #11a must make them
concrete and testable before any coordinator code exists:
- how ownership is held and proven;
- how a write is refused once ownership is lost;
- what is recorded before and after each effect, and in what transaction;
- how a step is identified so a replay does not act twice;
- what recovery does with each outcome it may find;
- how the frozen candidate's identity survives a crash.

## Considered options

Ownership:
1. An instance lock only (`flock`). It is lost silently if the lock file is
   deleted and recreated, after which a second process can lock the new file.
2. A fencing epoch in the store only. A second coordinator could start, take
   the epoch and run alongside the old one until the old one next writes.
3. A lease with a TTL. It needs trustworthy clocks and renewal, and is
   designed for distributed coordinators, which Ilmar does not have.
4. **Both 1 and 2.** The lock prevents a second coordinator from starting.
   The epoch makes any coordinator that lost ownership, for whatever reason,
   unable to write, and so unable to record the intent its next effect needs.

Recording effects:
1. Execution states only. A state cannot say whether a push inside it
   happened.
2. **A journal of operations keyed by an idempotency key, written in the same
   SQLite transaction as the state changes they belong to**, plus a ledger
   of resources.

Candidate integrity after a crash:
1. Store the full changed-path list. That is a second copy of git-derived
   data.
2. **Store a digest of the changed paths in the freeze operation's result.**
   Refuse to record a candidate in the execution without that freeze. #11c
   recomputes the paths from the coordinator-owned repository and must match
   the digest.

## Decision

### Ownership and fencing

- `InstanceLock.acquire(layout)` takes an exclusive, non-blocking `flock` on
  `<state>/coordinator/instance.lock`.
  - The state directory must be canonical (no symlinks), and the lock file is
    opened with `O_NOFOLLOW`.
  - The descriptor stays open for the process's life; only closing it or the
    process exiting releases the lock.
  - A second holder gets `InstanceLockedError`.
  - `verify()` checks that the file at the path is still the locked one
    (same device and inode). Removal or replacement raises `FencingError`.
- `ExecutionStore.claim_instance(instance_id, now)` returns a `Fence(instance_id,
  epoch)`.
  - The epoch is one higher than every earlier one.
  - A store that belongs to another instance id refuses the claim.
  - SQLite keeps the epoch in a single-row `owner` table. A trigger forbids
    lowering it, and `owner_history` logs every claim.
- `Ownership.take()` locks, then claims, releasing the lock if the claim
  fails.
- `Ownership.check()` verifies the lock and that the store's current fence is
  still its own.
- **Every write carries the fence once a store has been claimed.** That
  covers `create`, `apply`, `append_evidence` and `commit`. A missing or
  superseded fence raises `FencingError` and changes nothing. The check runs
  inside the write transaction, so it is serialised with claims across
  processes. Unclaimed stores (M1 behaviour, tests) accept unfenced writes.

### The operation journal

An `Operation` records one external side effect. Its fields:
- `key` (the idempotency key), `kind`, execution and attempt;
- `status`, and the `epoch` that last moved it;
- `version`;
- small, non-secret `intent` and `result` fields;
- a `note`. Errors are recorded by type name only, never by message.

**State machine of an operation:**

```
             ┌──────────── repeat (repeatable kinds only) ───────────┐
             ▼                                                       │
 (none) ─► INTENDED ──► SUCCEEDED   (final)                          │
             │   ╲──► FAILED      (final: it certainly did not happen)
             │    ╲─► AMBIGUOUS ──► SUCCEEDED | FAILED  (reconciliation)
             └───────────────────────────────────────────────────────┘
```

- **Writing outcomes.** `Journal.perform(kind, execution, effect, ...)`:
  1. commits the intent together with `before` (for example a resource
     recorded as `INTENDED`);
  2. calls `Ownership.check()`;
  3. runs the effect;
  4. records the outcome, depending on how the effect ended:
     - on success, commits `SUCCEEDED` with the result and `after(result)`
       atomically. An example of `after` is the VERIFYING transition that
       carries the candidate;
     - an exception type the caller declares definitive is `FAILED`;
     - any other exception is `AMBIGUOUS`.

     If writing the outcome fails, the operation stays `INTENDED`.
     Recovery treats `INTENDED` exactly like `AMBIGUOUS`.
- **Replay** (same key):
  - `SUCCEEDED` returns the recorded result without acting;
  - `FAILED` raises `LedgerIntegrityError`, because only a new attempt, with
    new keys, retries;
  - `INTENDED` or `AMBIGUOUS` repeats the effect only for *repeatable*
    kinds. Otherwise it raises `AmbiguousOperationError` until
    `Journal.reconcile(key, SUCCEEDED | FAILED, reason, result)` settles it
    from an observation of the external system.

**Idempotency keys and reconciliation rules.** A key is
`<kind>:<execution>:<attempt>[:<step>]`, so the same logical step always gets
the same key, and a new attempt always gets new ones. Rules for #11b and #11c:

| Kind | Step | Repeatable | Reconcile an unclear outcome by observing |
| --- | --- | --- | --- |
| `claim-task` | — | yes | the claim is idempotent (ADR-0013) |
| `release-task` | — | yes | removes only Ilmar's own label |
| `post-update` | purpose | no | the task's comments contain the update's marker → succeeded; otherwise failed (then post again under a new step) |
| `create-workspace` | — | no | the directory exists with the ledger's recorded identity → succeeded; it exists without one → quarantine; it is absent → failed |
| `checkout-base` | — | no | the destination never pre-exists (the adapter refuses that), so an unclear checkout → a new attempt |
| `start-container` | container name | no | `runtime.find(name)`: the owner labels match → succeeded with that id; missing → failed; a foreign owner → quarantine |
| `stop-container` | container name | yes | stopping a gone container is a no-op; removal is confirmed, or a `ContainmentError` quarantines |
| `freeze-candidate` | — | yes | deterministic: the same content, base and message give the same revision (port contract). The result carries revision, base and the changed-paths digest |
| `verify` | `run-<n>` | no | evidence for this attempt and candidate exists → use it; otherwise failed, then verify again with run *n+1* (a new key and a new checkout) |
| `publish-revision` | — | no | `branch_head == candidate_revision` → succeeded; the head equals the recorded expected head → failed; anything else → stop (a human must look) |
| `upsert-change-request` | — | yes | keyed by head branch on the provider side |
| `delete-workspace` | — | no | absent → succeeded; present with the recorded identity → failed (delete again); present with another identity → quarantine |

### The resource ledger

A `Resource` holds:
- the key `<kind>:<execution>:<attempt>:<name>`;
- the kind: workspace, verification checkout, container, network, branch or
  change request;
- its name and owner token;
- its status;
- once it exists, its immutable *identity* (container id,
  `device:inode`, commit id).

**State machine of a resource:**

```
INTENDED ──► ACTIVE ──► RELEASING ──► RELEASED      (final)
   │  ╲        │            │
   │   ╲──► RELEASED (never created)
   └───────────┴────────────┴──► QUARANTINED         (final; a human resolves it)
```

- A resource is recorded as `INTENDED` in the same commit as the operation
  that creates it. It becomes `ACTIVE` in the success commit, with its
  identity, and stays visible in the ledger until confirmed released.
- Its identity never changes once set. Released and quarantined entries
  never change again. Ledger rows are never deleted.
- `QUARANTINED` is final for automation. Nothing reuses, mounts or deletes a
  quarantined resource. The restart-safe quarantine and cleanup tracking
  asked for in #12/#15 are built on this.

### Atomic, fenced commits

`ExecutionStore.commit(fence, Commit)` applies, in one transaction:
- new executions and transitions of existing ones;
- operation inserts and updates;
- resource inserts and updates.

The rules are shared by every adapter (`check_fence`, `check_commit`):
- execution versions as before;
- a record at version 1 is an insert and must start `INTENDED`. Any later
  version must replace stored version − 1, otherwise
  `ConcurrencyConflictError`;
- legal status moves only;
- immutable kind, owner, intent, creation time and identity;
- operations carry the current epoch and belong to the execution's current
  attempt;
- **a candidate is recorded only with its freeze.** A transition into
  VERIFYING that sets `candidate_revision` needs a `SUCCEEDED`
  `freeze-candidate` operation for that execution and attempt. It may be
  stored already, or be in the same commit. Its result must name the same
  revision and base revision and a changed-paths digest. Otherwise
  `LedgerIntegrityError`.

SQLite schema v3 adds `owner`, `owner_history`, `operations`, `resources`
and `ledger_events`, an append-only log of every operation and resource
change with its epoch. Triggers repeat the rules for writes that bypass the
adapter:
- the epoch is monotonic;
- nothing is deleted;
- final operations and released or quarantined resources are immutable;
- identity and intent are immutable;
- versions grow by one.

Version 1 and 2 databases migrate in place in one transaction.

### Candidate integrity

- `freeze_result(candidate)` is what the freeze records: revision, base
  revision, `changed_paths_digest` (SHA-256 of the sorted, unique paths) and
  the count.
- `check_candidate(candidate, freeze)` fails closed unless a candidate
  obtained any other way matches it exactly: revision, base and paths. In
  #11c that means a candidate rebuilt from the coordinator's repository
  after a crash.
- In the normal path, the object `freeze_candidate` returned goes straight
  to the Verifier (ADR-0018).

### Invariants

1. At most one coordinator per instance runs: the lock.
2. A coordinator whose fence is superseded can write nothing: the store.
3. Every external side effect has a durable intent before it starts, and the
   owner is re-checked immediately before it.
4. Every step has one deterministic idempotency key. A succeeded step never
   runs again. A failed step never runs again in the same attempt.
5. An unclear outcome of a non-repeatable effect blocks the execution until
   it is reconciled from an external observation. It is never repeated
   blindly.
6. Every external resource is in the ledger before it is created and stays
   there until its release is confirmed. Its identity is fixed once known.
   Quarantined resources are never touched automatically.
7. A candidate revision is recorded only together with the successful freeze
   that produced it, including the digest of its changed paths.
8. State, journal and ledger change together or not at all.

### Port contract changes

- `ExecutionStore` (`ilmar.ports.execution_store`):
  - adds `claim_instance`, `fence`, `commit`, `operation`, `operations`,
    `unfinished_operations`, `resource` and `resources`;
  - `create`, `apply` and `append_evidence` take an optional keyword
    `fence`. Existing calls keep working on unclaimed stores and are refused
    on claimed ones.
- The contract suite gains these rules; the SQLite (file and memory) and
  in-memory adapters pass it.
- No other port changes in #11a. `SourceControlProvider.load_candidate` (the
  recompute path) comes with #11c and gets its own contract tests.

## Verification

- Contract suite (all three store variants):
  - epochs, a foreign instance refused, unfenced and superseded writes
    refused with nothing changed;
  - all-or-nothing commits;
  - operation status rules, versioning, epoch and attempt binding,
    immutable intent;
  - the resource lifecycle and identity;
  - a candidate refused without its matching freeze and accepted together
    with it;
  - eight threads racing for one key, of which exactly one wins.
- SQLite: v1→v3 and v2→v3 migrations keep data; direct-SQL tampering is
  refused for every trigger; every ledger change is logged with its epoch;
  a second connection's claim fences out the first.
- Ownership:
  - a second *process* cannot take the lock until the first releases it;
  - a removed or replaced lock file, a symlinked lock file and a symlinked
    state directory are detected or refused;
  - a failed claim releases the lock;
  - a takeover through the store is detected even without the lock.
- Journal:
  - the intent is durable before the effect;
  - a succeeded step does not run again, and a definitive failure is not
    retried;
  - an ambiguous step blocks until reconciled; repeatable steps are
    repeated;
  - a superseded coordinator records no intent and performs no effect; a
    takeover, or a replaced lock file, after the intent stops the effect;
  - candidate integrity: the revision, base and missing or extra changed
    paths each fail.
- Crash injection: a store that dies at commit *N* (for every *N*), then a
  restart. A non-repeatable effect never happens twice, nothing stays
  unfinished after recovery, and the outcome matches what the world shows.
- Mutation checks: each of seven invariants, reverted on its own, fails a
  test.

## Consequences

- Good:
  - Crash windows are explicit and finite: before the intent, between the
    intent and the effect, and between the effect and its outcome. Each has
    a defined recovery.
  - The coordinator (#11b) can be written as a sequence of `perform` calls,
    and recovery (#11c) as reconciliation over `unfinished_operations`.
- Bad:
  - **Residual window.** Between `Ownership.check()` and the effect, a few
    microseconds pass. Both mechanisms would have to fail in exactly that
    window, by the lock file being replaced *and* another coordinator
    claiming. The resource-level identity checks (ADR-0012 level 3) are
    the last line there.
  - Reconciliation needs an observation per kind (the table above). An
    effect whose outcome cannot be observed blocks its execution for a
    human. That is the intended fail-closed behaviour.
  - The journal grows with every step and is never pruned yet.
  - Intent and result fields are trusted not to contain secrets. Errors are
    recorded by type name only for that reason.
  - SQLite must be on a local filesystem (ADR-0005). `flock` is not
    reliable on network filesystems.

## Revisit when

- Coordinators run on several hosts. Then the lock becomes a lease and the
  epoch a consensus-backed fencing token.
- A kind's effect becomes idempotent by construction, so it can become
  repeatable.
- The journal size matters: decide on pruning or archiving explicitly.
