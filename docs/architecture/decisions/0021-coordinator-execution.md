# ADR-0021: One coordinator execution: steps, failure classes, claims and task completion

- Status: Proposed
- Date: 2026-11-05
- Sources: [R1], [R3] · Findings: F-004, F-021
- Implements: #11b; builds on ADR-0010, ADR-0012, ADR-0013, ADR-0018 to ADR-0020

## Context and problem

#11a provides fencing, the write-ahead journal and the resource ledger
(ADR-0020). #11b must drive one task end to end on top of them. Several
things need deciding:
- how the work is structured so that every external effect is journaled and
  fenced, and several executions can later run at once;
- which failures earn a new attempt;
- how the agent run itself is recorded;
- when a task's claim is released;
- what "done" means for an execution and for its task;
- what happens to workspaces;
- the command's exit codes.

The maintainer decided, on issue #11:
- at most 3 attempts per execution;
- retry only transient failures, never security, policy or deterministic
  ones;
- keep tasks claimed while their change request is open, and release the
  claim on terminal failure or after a merge;
- distinguish execution completion (published) from task completion
  (merged);
- delete successful workspaces and keep failed ones, recorded in the
  ledger;
- exit codes 0, 1, 2 and 3.

## Considered options

- **Where effects happen:** in the worker, next to the state logic, or
  **in one `Effects` class**, each of whose public methods is exactly one
  `Journal.perform`. An architecture test can only enforce the second.
- **The agent run:** an untracked call, or **a journaled `run-agent`
  operation**. It is costly and changes the workspace, so it is not
  repeatable, and its outcome is needed for recovery.
- **Retry scope:** every failure, or **transient failures only**, by the
  classification below.
- **Task completion:** a new execution state after SUCCEEDED, which changes
  ADR-0004's machine; or **the ledger**: the change-request resource stays
  `ACTIVE` while open and is `RELEASED` with the note `merged` or `closed`
  when it is done.

## Decision

### Structure

- **`ilmar.core.coordinator.Coordinator`.**
  - One per process; holds the `Ownership` (ADR-0020).
  - `run_once()` refuses new work while the journal has unfinished
    operations that are **not repeatable**. Unfinished repeatable ones (a
    claim release, a stop) are simply performed again by their step.
  - It then follows up published change requests, resuming any release an
    earlier pass left half done, and picks at most one eligible task.
  - `dispatch()` runs a worker. It is safe to call concurrently for
    different executions; there is no global execution mutex.
- **`ilmar.core.worker.ExecutionWorker`.**
  - Drives one execution while holding its lease.
  - Re-reads the execution after every step, so every intent is written
    against the version it last saw.
  - Keeps, in memory only, the objects later steps need: the frozen
    `CandidateRevision`, the container handle and the evidence.
- **`ilmar.core.effects.Effects`.** The only core code that calls a port
  method that changes the outside world. The Verifier, which runs inside
  the journaled verify step, is the one other exception. Each public
  method:
  - is one journaled operation with its idempotency key;
  - records resources as `INTENDED` before, and `ACTIVE`/`RELEASED`
    after;
  - declares definitive errors.

  `tests/architecture/test_side_effects.py` enforces both rules.
- **`ilmar run --once --config PATH`.** `--config` is required and never
  defaults to the working directory (ADR-0019). Order:
  1. load and build (ADR-0019);
  2. `Ownership.take`;
  3. canonical workspaces root;
  4. one pass.

### Steps of one execution

Keys follow ADR-0020: `<kind>:<execution>:<attempt>[:<step>]`.

| State | Effect | Recorded before | Recorded with success |
| --- | --- | --- | --- |
| QUEUED | `claim` | intent | CLAIMED, or CANCELLED if claimed by another or ineligible |
| CLAIMED / RETRY_WAIT | — | PROVISIONING (a new attempt and a new owner token) | — |
| PROVISIONING | create the attempt directory (refused if it exists) | WORKSPACE `INTENDED` | `ACTIVE` with `dev:ino` |
| PROVISIONING | `branch_head` then `checkout` into `…/agent` | WORKSPACE `INTENDED` | `ACTIVE`; RUNNING_AGENT with `base_revision` and branch; result keeps the head observed (the publish lease) |
| RUNNING_AGENT | `start` (through `authorize_mount`, the recorded identity, `components.agent_sandbox`) | CONTAINER `INTENDED` with the owner token | `ACTIVE` with the container id |
| RUNNING_AGENT | `run-agent` | intent | outcome, exit status, a truncated summary |
| RUNNING_AGENT | `stop` (always, also after an agent error) | CONTAINER `RELEASING` | `RELEASED`; a `ContainmentError` → `QUARANTINED` |
| RUNNING_AGENT | `freeze_candidate` (only after COMPLETED) | intent with the base | VERIFYING with `candidate_revision`, atomically with the freeze (store-enforced); FAILED if nothing changed |
| VERIFYING | `Verifier.verify` with **the object the freeze returned**, run *n* = evidence of the attempt + 1 | one CONTAINER per policy job, VERIFICATION_CHECKOUT, all `INTENDED` | containers `RELEASED`, checkout `ACTIVE`; PUBLISHING or FAILED by verdict |
| PUBLISHING | `publish_revision(candidate_revision, expected_head = the head observed at checkout)`, then read the head back | intent | the head, which must equal the candidate |
| PUBLISHING | `upsert_change_request` (with `ilmar:needs-human-review` for `PUBLISH_FOR_REVIEW`) | CHANGE_REQUEST `INTENDED` | `ACTIVE` with its key; SUCCEEDED with the URL |
| SUCCEEDED | `post_update`, delete the attempt directory | everything inside `RELEASING` | `RELEASED` ("deleted after success"). The claim is **kept** |
| FAILED | `post_update`, `release` | — | the workspace stays `ACTIVE`: kept for inspection |

**Before publishing**, and again around the change request, the worker
checks:
- the in-memory candidate equals the journal's freeze: revision, base and
  changed-paths digest (`check_candidate`), and equals the execution's
  recorded revisions;
- the latest evidence is the one this run produced, is publishable, and
  names the same candidate and base.

After the push:
- the push effect reads the branch head back, and it must be the candidate;
- right before opening the change request, the head is read again;
- the upsert effect records the head right after.

If the branch moved at any of these points, no change request is opened,
or the execution is **not** marked complete (it stays PUBLISHING, blocked,
with the change request tracked in the ledger).

Any mismatch blocks the execution. The published revision is therefore
exactly the verified frozen candidate on an unchanged base.

### Failure classes

| Class | Examples | Result |
| --- | --- | --- |
| Transient (before freezing) | agent TIMED_OUT; a sandbox start that left no container; an agent run interrupted by a runtime error | a new attempt while attempts remain, otherwise FAILED ("attempts exhausted"). **At most 3 attempts**, capped in code regardless of configuration |
| Transient (after freezing) | verification that did not finish and left no sandbox; a push that the branch head shows did not happen | **the same frozen candidate** is verified again (run *n+1*: a new key, directory and sandboxes) or pushed again (`retry-1`, `retry-2`) in the same attempt, at most 3 tries. **The agent is never re-run for these.** If all tries fail, the execution stays in VERIFYING or PUBLISHING, blocked (exit 2), with the candidate kept for #11c |
| Deterministic | agent FAILED; the agent changed nothing; `REJECTED_CHECKS_FAILED`; a failed checkout (an unknown base branch, or a tree the adapter refuses as unsafe, would fail the same way) | FAILED, never retried |
| Security / policy | `REJECTED_CI_CONFIG`; `CandidateRejectedError`; `PolicyViolationError`; `OwnershipConflictError` (foreign change request, replaced directory); `LeaseConflictError` / `NonFastForwardError` (the branch moved) | FAILED, never retried |
| Blocked | an ambiguous effect that cannot be decided by observation; a `ContainmentError` (quarantine); `LedgerIntegrityError`; a stale view | the worker stops; exit 2. Quarantined resources and the claim stay for a human |
| Ownership lost | `FencingError`, also when it hits the commit of an effect's outcome (never masked as a refused follow-up) | propagated at once; exit 3; nothing more happens |

`Effects.NEVER_TRANSIENT` lists the failures that are never turned into a
transient failure, whichever port raised them:
- `ContainmentError`;
- `PolicyViolationError`;
- `OwnershipConflictError`;
- `CandidateRejectedError`;
- `LedgerIntegrityError`;
- `FencingError`.

Every `except ProviderError` lets them through first, because
`ContainmentError` is itself a `ProviderError`: Podman's `start` raises it
when sandbox or sidecar teardown cannot be confirmed, and the container is
then quarantined. GitHub's `CrossOriginRedirectError` is also a
`PolicyViolationError`.

Unexpected errors at the process boundary exit 2 ("a human must look"),
never 0 or 1.

An agent FAILED outcome cannot be told apart from a deterministic failure
by the `AgentResult` alone, so it is not retried. A runner-provided reason
may refine this later (#14).

### `run-agent`

A new operation kind, not repeatable. Reconciliation: an unknown outcome
abandons the attempt, recorded as `FAILED` with the reason. This extends
ADR-0020's table; ADR-0020 itself is unchanged.

### Claims, execution completion and task completion

- **An execution completes** (SUCCEEDED) when its verified candidate is
  published as a change request. The task stays claimed.
- **On later passes**, the coordinator reads each open change request's
  state with the new read-only port method
  `SourceControlProvider.change_request_state` (OPEN, MERGED or CLOSED).
  - **MERGED: the task is complete.** The claim is released (journaled
    `release-task:<id>:<attempt>:merged`), a note is posted, and the
    change-request resource is `RELEASED` with the note
    `merged: task complete`.
  - **CLOSED:** the same, with the note `closed without merging`.
- **On terminal failure**, the claim is released and a note posted.
- **A task with a published or failed execution is never started again
  automatically**, even if it shows up as eligible.
  - Published: its change request exists, open or merged.
  - Failed: releasing the claim makes the task eligible on the tracker,
    but a new execution would be an automatic retry of a failure that was
    classified as not retryable.

  A human decides. A command to reset a task comes later (#12).

### Workspaces

- After SUCCEEDED, the attempt directory (agent workspace and verification
  checkouts) is deleted through `remove_directory` with the recorded
  identity, and every resource inside is `RELEASED`. A replaced directory
  is quarantined, never deleted.
- Failed attempts' directories stay. Their resources remain `ACTIVE`,
  meaning they exist and are kept for inspection. Cleanup policy belongs to
  #12/#15.

### Exit codes

| Code | Meaning |
| --- | --- |
| 0 | success: published, cancelled because someone else claimed the task, or nothing to do |
| 1 | the execution failed |
| 2 | blocked: manual intervention or reconciliation is needed (also when unfinished operations exist at startup) |
| 3 | configuration or ownership error (invalid configuration, instance locked, ownership lost) |

### Parallelism

Leadership is per instance; work is per execution. Everything an execution
touches is scoped by instance, execution, attempt and run:
- names;
- directories;
- keys;
- ledger rows;
- owner tokens.

Workers share no mutable state, hold a lease each, and only touch their own
execution's records (ADR-0020). #11b dispatches one execution per pass; a
bounded pool can replace the call without changing the design.

### Other changes

- `SourceControlProvider.change_request_state` (port contract addition),
  implemented by the GitHub adapter (`GET /pulls/{n}`: `merged` / `state`)
  and the fake, with contract tests on both.
- The Verifier takes the fence and writes evidence fenced.
- `ilmar run --once --config PATH`.

## Verification

- End to end with fakes (`tests/unit/test_coordinator.py`):
  - a full run;
  - **the pushed revision equals the execution's and the evidence's
    candidate**, on the recorded base;
  - the Verifier received exactly the frozen candidate, whose digest
    matches the freeze;
  - one claim, one push and one change request;
  - every journal entry is final;
  - sandboxes are released, and the workspace is deleted and released.
- The claim is kept while the change request is open, and nothing new
  starts. Merged and closed change requests release the claim; merged is
  reported as task completion.
- Retries:
  - a timeout is followed by a successful second attempt, with the failed
    attempt kept;
  - three timeouts → FAILED at attempt 3, even with `max_attempts = 5`;
  - agent failure, no changes, failed checks and changed CI configuration
    are never retried;
  - a moved branch is never overwritten or retried;
  - claimed by someone else → CANCELLED with nothing created.
- Ambiguity:
  - a push whose answer was lost is recognised, not repeated;
  - a push that never happened is retried under `retry-1`;
  - a sandbox that never started → a new attempt;
  - an unremovable sandbox → quarantine, exit 2, never frozen;
  - unfinished operations block the next pass.
- Integrity: a candidate swapped in memory, or evidence for another
  candidate, blocks before verification or publication.
- Adversarial review of `46c1662`, each fix mutation-checked:
  - a `ContainmentError` from start → quarantine, FAILED, never retried;
  - a cross-origin redirect while pushing → FAILED, tried once;
  - a failed checkout → FAILED, never retried;
  - a failed task is never started again;
  - a verification infrastructure failure → the same frozen candidate is
    re-verified (run 2), with the agent run once;
  - repeated verification failures, and repeated push failures, block
    without re-running the agent;
  - a branch moved after the push, before the change request, or while it
    opens → no completion;
  - an interrupted claim release after a merge is resumed;
  - a repeatable unfinished effect does not stop other work, and a
    non-repeatable one does;
  - a lost fence on an outcome commit raises `FencingError`;
  - an unexpected error exits 2.
- Fencing and concurrency:
  - a takeover during the agent run stops the worker before freezing;
  - two executions run their agents at the same moment and publish
    separately;
  - one execution never has two workers;
  - verification runs one sandbox per job.
- Architecture: only `effects.py` (and the Verifier) call mutating port
  methods, and every `Effects` method goes through the journal.
  Mutation-checked.
- CLI: `--config` and `--once` are required; an invalid configuration and
  a locked instance exit 3.
- Real Podman (CI): one execution through a real ALLOWLIST agent sandbox
  with the egress proxy, two real verification sandboxes (one proves no
  network), the SQLite store with fencing, and fake tracker and source
  control. It publishes the verified candidate, leaves no containers and
  deletes the workspace.

## Consequences

- Good:
  - Every external effect of a run is journaled, fenced and attributable
    to one execution. The published revision can only be the verified
    frozen candidate.
  - Retries cannot repeat an effect: a new attempt has new keys, names and
    directories.
- Bad:
  - Agent FAILED outcomes are never retried, even when the cause was
    transient (an API hiccup). This is deliberate until runners report a
    reason.
  - A late-stage failure that persists for three tries leaves the
    execution blocked in VERIFYING or PUBLISHING until #11c resumes it from
    the frozen candidate (`load_candidate`). This is deliberate: the agent
    is never re-run once a candidate exists.
  - A transient failure of a checkout is not retried, because it cannot be
    told apart from a deterministic one.
  - Containers of a coordinator that lost ownership mid-run are left for
    the new owner. A stale owner must not act; #11c cleans up through the
    ledger.
  - Until #11c, any crash or ambiguous outcome blocks new work for the
    instance. That is fail closed, but the instance stops until a human
    reconciles.
  - Failed workspaces accumulate until #12/#15 adds cleanup.
  - A published or failed task is never re-run automatically, even if its
    change request was closed. Re-running is a human decision.

## Revisit when

- Agent runners report why they failed (#14). Then some FAILED outcomes may
  become transient.
- Several executions run in parallel. Then add a bounded pool and test the
  store under concurrent writers.
- #11c adds recovery. Then remove the "unfinished operations block new
  work" rule for the cases it reconciles.
