# ADR-0022: Crash recovery: durable state is the only memory

- Status: Proposed
- Date: 2026-11-05
- Sources: [R1], [R3] · Findings: F-004, F-021
- Implements: #11c; builds on ADR-0010, ADR-0012, ADR-0018, ADR-0020, ADR-0021

## Context and problem

#11b (ADR-0021) runs one execution end to end, with every external effect
journaled and fenced (ADR-0020). When the process dies mid-step, what is
left behind is:

- unfinished journal operations;
- ledger rows in `INTENDED`, `ACTIVE` or `RELEASING`;
- a sandbox that may still run;
- a branch that may or may not have moved;
- evidence that may or may not have been written.

ADR-0021 handled this by refusing new work while any non-repeatable
operation was unfinished. #11c replaces that refusal with recovery: from
every execution state and every journaled boundary, the execution either
finishes correctly or stops for a human. Nothing is guessed.

The maintainer decided, on issue #11:

1. Evidence names its run and has a format version, so it is matched to its
   exact run and never inferred from timestamps.
2. A `remove_owned` method on the container runtime port. It removes
   exactly the recorded execution's resources (container, egress sidecar,
   network) and fails closed on any ownership ambiguity.
3. A crash after the sandbox was created but before the agent ran: remove
   the sandbox and start a new attempt. A possibly contaminated sandbox is
   never reused.
4. Recovery runs before new tasks are discovered. Only executions that
   cannot be safely reconciled are blocked; unrelated work continues.
5. A publishing branch that moved unexpectedly blocks for a human. Ilmar
   never force-pushes, resets or overwrites an unverified head.

Further requirements:

- recover at every journaled boundary and from every state;
- rebuild state only from the journal, the ledger and trusted external
  observations;
- reload frozen candidates with `load_candidate` and check them;
- never run the agent after a candidate was frozen;
- keep fencing, per-execution leases and future concurrency;
- recovery is idempotent across repeated crashes.

## Considered options

- **A separate recovery pass** with one rule per operation kind, then normal
  execution; or **one worker whose every step reads durable state**, so that
  running normally and recovering are the same code. With the first, two
  code paths would have to agree about every boundary. With the second,
  "recover" is "run the next step", and idempotency follows from the steps
  being decided by what is recorded.
- **Matching evidence to a verification run:** by time (latest record), by
  candidate, or **by an explicit run number in the record**. Time and
  candidate both fail when the same candidate is verified twice; only a
  run number identifies the interrupted run (decision 1).
- **Sandbox removal after a restart:** the in-memory `stop(handle)`, which
  needs the handle and the runtime's memory of the sandbox, or **a
  `remove_owned(name, owner)` that works from labels alone** (decision 2).
- **Interrupted agent run:** resume in the same sandbox, or **remove the
  sandbox and start a new attempt** (decision 3). The sandbox ran untrusted
  code with no recorded outcome, so it is never trusted again.
- **A candidate that cannot be reloaded:** fail the execution, or **block
  it**. Freezing succeeded, so the problem is the coordinator's own
  repository, not the agent's work. Failing would release the task as if
  the agent had failed.
- **A fenced-out process:** skip what it cannot lease, or **stop at once**.
  Skipping made a superseded coordinator report "no eligible task" with
  exit 0.

## Decision

### Invariants

Checked after every crash point by the crash-injection suite
(`tests/unit/test_recovery.py`):

- **I1 — Exactly once:** at most one push of the published revision and at
  most one change request per execution, whatever the crash points.
- **I2 — Verified equals published:** the published head is the frozen
  candidate, the latest evidence of the final attempt names that
  candidate, and the evidence digest is intact.
- **I3 — No agent after a freeze:** an attempt runs the agent at most once,
  and no attempt runs it after any attempt froze a candidate. The worker
  and `Effects.run_agent` both enforce this.
- **I4 — Nothing runs unaccounted for:** every sandbox in the ledger ends
  `RELEASED` or `QUARANTINED`; no sandbox is left running.
- **I5 — Every operation finishes:** after recovery no operation is
  `INTENDED` or `AMBIGUOUS` except under a blocked execution.
- **I6 — Fail closed:** whatever cannot be proven blocks, and only its own
  execution. Recovery never deletes, overwrites or reuses anything whose
  ownership or content it cannot prove.
- **I7 — Fencing:** a coordinator that lost ownership raises `FencingError`
  and performs no further writes or effects. Settling an operation
  (`Journal.reconcile`) needs the execution's lease and current ownership,
  exactly like performing one.
- **I8 — Failure classes survive a crash:** a failure recorded as
  never-transient (security, policy, ownership, containment, integrity) ends
  the execution after a restart, exactly as without one. An interruption
  that recovery abandoned stays transient through further crashes.
- **I9 — Evidence is for this policy:** a candidate is published only on
  evidence whose policy digest equals the policy now in force.
- **I10 — Time never runs backwards in persisted state** (see "Time").

### Where state comes from

The worker keeps nothing between steps. Each step reads:

- **The journal:** operations by key, with status and recorded result.
- **The ledger:** resources, with owner tokens and directory identities.
- **Trusted external observations:**
  - branch heads, read through `SourceControlProvider`;
  - frozen candidates, reloaded with `load_candidate` and checked against
    the freeze operation's digest (`check_candidate`);
  - sandboxes, found by name and owner labels through `remove_owned`;
  - directory identities.
- **The evidence store:** evidence for this execution, attempt and run.

Agent output and anything in a workspace are never trusted as state.

### Recovery order

`Coordinator.run_once()`:

1. `recover()` drives, oldest first:
   - every non-terminal execution;
   - every terminal one that still `needs_attention`. That means unfinished
     operations, live sandbox rows, a published attempt whose workspace is
     not yet released, or a failed execution whose claim is not yet
     released.

   A tracker read error blocks only that execution. A lease held by another
   worker skips it. `FencingError` stops the pass.
2. Change-request follow-up (ADR-0021).
3. Only then are new tasks discovered. A task with a published or failed
   execution is never picked again.

The pass's exit code is the worst of its results. A blocked execution
yields 2 while other executions in the same pass still run (decision 4).

### Reconciliation rules

| State or boundary | What is recorded | Rule |
| --- | --- | --- |
| `QUEUED` / `CLAIMED` / `RETRY_WAIT` | — | Claim, or start the attempt; the claim is repeatable. |
| `PROVISIONING`, create or checkout unfinished | operation `INTENDED`/`AMBIGUOUS` | Settled by the directory's identity against the ledger; then a transient retry (new attempt). A `FAILED` step is deterministic. |
| `RUNNING_AGENT`, no sandbox start | — | Start a sandbox and run the agent once. The sandbox is removed in `finally`. |
| `RUNNING_AGENT`, start unfinished or succeeded, no run | start op, container row | Remove the sandbox from the ledger with `remove_owned`, settle the start, then a new attempt (decision 3). |
| `RUNNING_AGENT`, run unfinished | `run-agent` `INTENDED` | Abandon the run (`FAILED`, "interrupted"), remove the sandbox, then a new attempt. The run is never repeated in that attempt. |
| `RUNNING_AGENT`, run recorded | `run-agent` result | Remove the sandbox, then act on the recorded outcome: completed → freeze; timed out → transient; failed → deterministic. |
| Freeze | container row `RELEASED`; workspace identity | Freeze only after the sandbox is confirmed gone and the workspace is the recorded directory (identity, no symlink). Otherwise block. The freeze is repeatable and deterministic. |
| `VERIFYING`, run *n* unfinished | `verify:<n>` `INTENDED` | Remove run *n*'s job sandboxes from the ledger. Evidence with this attempt, run *n*, candidate and base → `SUCCEEDED` with that outcome. No such evidence → `FAILED`, then run *n + 1*. Block after three runs. |
| `PUBLISHING` | candidate, exactly one successful verification | Reload and check the candidate. Then match the latest evidence by run and digest. |
| Push unfinished | `publish:<step>` `INTENDED` | Head = candidate → `SUCCEEDED`. Head = expected head → `FAILED`, push again (at most three tries). Any other head → **block**; never force, reset or overwrite (decision 5). |
| Change request | `upsert` | Reuse the recorded change request row. Its follow-up requires the head to still equal the verified candidate. |
| Terminal, cleanup unfinished | stop, delete, release ops | Settle each one (a sandbox by `remove_owned`, a directory by identity), then finish: delete the published workspace; release the claim of a failed execution; keep failed workspaces. |

`Effects` gains one `settle_*` method per boundary. Each method reconciles
through `Journal.reconcile` and never calls a mutating port method itself.
The architecture test now allows a public `Effects` method to use either
`perform` or `reconcile`. `load_candidate` is the only read-only exception.

### Failure classes across a crash

- The journal records every never-transient error (`NEVER_TRANSIENT`) as
  `FAILED`, with the error class as the operation's note, for every kind.
  Before this, they were left `AMBIGUOUS`. A crash between that record and
  the execution's failure then made recovery treat the step as interrupted
  and retry it: a policy violation was re-run and could publish from a new
  attempt. It also made every later pass re-dispatch the failed execution.
- Recovery reads that note. A never-transient failure, recorded `FAILED`
  (or `AMBIGUOUS` in older records), fails the execution after its
  sandboxes are removed. This covers provisioning, the agent sandbox, the
  agent run and verification.
- An interrupted step that recovery settles as `FAILED` carries the note
  `ABANDONED`. A second crash before the retry must not turn it into a
  deterministic failure.

### One execution's boundary

`run_once` runs each execution, recovered or new, and each change-request
follow-up inside its own boundary:
- `FencingError` stops the pass.
- A lease held elsewhere skips the execution.
- Any other unexpected exception (an adapter or Ilmar bug) is logged and
  reported as that execution's `BLOCKED` result: exit 2, never silenced.
  The next pass tries again from durable state.

This is the per-execution process boundary that decision 4 needs. Without
it, one execution's bug aborted every pass, oldest first, and no unrelated
task ever ran.

### Time

- **Deadlines of running work** (agent and command timeouts, proxy
  start-up) are measured with `time.monotonic` inside the adapters and are
  never persisted.
- **Persisted timestamps** come from the wall clock through one
  `NonDecreasingClock` per process. Every transition is also clamped to the
  execution's last persisted update (`not_before`). A backward step (NTP, a
  restored snapshot) therefore never makes a write invalid: timestamps
  stand still until the wall clock catches up. Before this, a step back
  during a pass recorded a succeeded change request as ambiguous and
  blocked it for good.
- Nothing in Ilmar compares a persisted timestamp with the current time to
  make a decision (no wall-clock expiry), so standing still is safe.

### Evidence and policy

`publish` blocks when the evidence's `policy_digest` differs from the
policy in force, for example when the configuration changed while Ilmar
was down. Re-verifying instead would need a PUBLISHING → VERIFYING
transition, which ADR-0004 does not allow. A human decides.

### Port contract changes

- **`ContainerRuntime.remove_owned(name, *, owner) -> bool`** (new).
  - Removes the named sandbox and its companions (for Podman: the egress
    sidecar and network) from labels alone, with no in-memory state.
  - Returns `False` if nothing exists.
  - Checks the owner of **every** part before removing **any** part.
  - A foreign, unlabelled or ambiguous (duplicate) part raises
    `OwnershipConflictError` and touches nothing. The ledger row is then
    quarantined.
  - An unconfirmed removal raises `ContainmentError`.
  - Containers are removed by the immutable ID read with their labels. The
    network is too: by the ID that `network inspect` returned with its
    labels, never by name. A network replaced between the check and the
    removal is someone else's and stays.
  - Partial leftovers from an interrupted removal are finished.
  - Contract-tested on the fake and on real Podman.
- **`SourceControlProvider.load_candidate(repository, *, base_revision,
  revision) -> CandidateRevision`** (new, read only).
  - Succeeds only for a revision frozen by this provider whose single parent
    is the base. The parent is read from the raw commit object, so
    `info/grafts`, shallow files and replace refs cannot fake it.
  - Recomputes the changed paths, without renames, external diff tools or
    textconv.
  - Raises `CandidateRejectedError` otherwise.
  - The core rechecks the result against the freeze digest. After a
    successful freeze, a rejection is an integrity problem and blocks.
- **Evidence format 2.**
  - `EvidenceRecord.run` (≥ 1) is written as `{"format": 2, "run": n, …}`.
  - A record without a run is still written as format 1, byte for byte, so
    every stored digest still verifies. A test pins a pre-#11c digest.
  - Readers accept 1 and 2; format 2 requires a run.

No existing method changed meaning. `stop()` remains for the normal path of
the code that holds a handle. Sandbox removal in the worker now always goes
through `remove_owned`.

### Testing

- **Crash at every store write** (27 in a clean run) and **crash before
  every external effect** (14).
  - Each crash is followed by recovery in a new process: a new store
    connection, a new `Ownership` and a new coordinator over the same
    external fakes.
  - Every invariant is checked after recovery.
  - `Crash` is a `BaseException`, so nothing in Ilmar can catch it. After
    it fires, every write and effect fails too.
- **Every path, not only the happy one.** The same exhaustive injection
  runs for:
  - a transient agent failure followed by a new attempt;
  - a verification failure, where the claim is released and the workspace
    kept;
  - exhausted attempts;
  - a security failure;
  - the follow-up after a merge.
- **Crashes during recovery**, on every path: every first crash point, then
  a second crash early, midway and late in recovery.
- **One test per blocking rule:**
  - a moved branch;
  - a foreign sandbox;
  - a tampered or unloadable candidate;
  - a replaced or symlinked workspace;
  - a fenced-out process;
  - a recorded security failure, with and without a crash;
  - a wall clock stepped back across a restart and during a pass;
  - an unexpected error in one execution;
  - evidence from another policy;
  - a Podman network replaced between inspection and removal;
  - a blocked execution next to unrelated work that publishes;
  - several interrupted executions recovered together.
- **The architecture test** derives the mutating port methods from
  `ilmar.ports`, so a new one (`remove_owned` was missing) cannot be
  forgotten. Every mutating call in `Effects` must sit inside an effect
  closure handed to the journal.
- **Mutation checks** confirmed that the suite fails when sandbox removal or
  the push reconciliation is broken.
- **Real Podman** (`tests/integration/test_recovery_podman.py`, in CI):
  - a coordinator dies during the agent run in an allowlisted sandbox;
  - a new process with a new `PodmanRuntime` removes the container, sidecar
    and network;
  - it publishes from attempt 2 in a new sandbox;
  - plus `remove_owned` on real resources: complete, partial and a foreign
    owner.

## Consequences

- ADR-0021's rule that unfinished operations block new work is replaced by
  recovery. All other parts of ADR-0021 stand.
- Normal running and recovery share one code path. A new step must be
  written as "decide from durable state", or the crash suite fails.
- An interrupted agent run or provisioning costs a whole new attempt. This
  is accepted: a contaminated sandbox is never reused. Three interrupted
  attempts end the execution as "attempts exhausted". The task is then
  released but, like any failed task, not picked again automatically.
- Blocked executions need a human. Until then, their task stays claimed
  and their resources stay as they are.
- A directory identity is `(device, inode)`. A directory deleted and
  re-created can get the same inode. Workspace content is untrusted agent
  output anyway and is always verified before publishing, so this cannot
  publish anything unverified.
- No unblock command exists yet. A human resolves the external cause; the
  next pass re-evaluates and finishes when the cause is gone (tested for a
  moved branch).

## Revisit when

- A per-execution boundary that catches unexpected exceptions turns out to
  hide recurring bugs. Every catch is logged and reported as blocked.
- Executions run in parallel in one process (leases already allow it), or
  coordinators run on several hosts: the evidence of "no live sandbox" must
  then cover remote runtimes.
- A human-facing unblock or abandon command is added.
- A runtime other than Podman implements `remove_owned`.
