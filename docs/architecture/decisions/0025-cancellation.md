# ADR-0025: Cancellation through a request inbox, applied by the lease holder at safe points

- Status: Accepted (2026-11-09, maintainer approval on PR #72)
- Date: 2026-11-09
- Sources: [R1], [R21] · Findings: F-001, F-024
- Implements: #16 (design only; implementation follows approval). Prepares
  #12 (an ineligible task is cancelled the same way). Builds on ADR-0004,
  ADR-0012, ADR-0014, ADR-0020, ADR-0021, ADR-0022, ADR-0024

## Context and problem

#16 asks to stop an execution on request, with the running container stopped,
the task released and CANCELLED recorded. Only ownership-verified resources
may be stopped or removed (ADR-0012, rule 3). Today the only way into
CANCELLED is a refused claim (ADR-0021).

Several existing decisions shape any design:

- **One owner, holding a lock.** A coordinator holds the instance lock for
  its whole pass. "Every command that changes anything, including manual CLI
  commands, needs this lock" (ADR-0012, level 1). A pass can last as long as
  an agent run, up to the agent timeout.
- **One worker per execution.** Only the worker holding an execution's lease
  may perform its sensitive operations, after checking lease, lock and
  version (ADR-0012, level 2). A second thread or process that stopped "its"
  container would break that.
- **The agent run is one non-repeatable journaled operation**, executed
  synchronously in the pass (ADR-0021). After a crash it is abandoned, never
  repeated (ADR-0022).
- **A process inside a container cannot be bounded by killing its
  `podman exec` client** (F-024). Only removing the whole container
  guarantees that nothing keeps writing. The runtime already does exactly
  that on a timeout, by the container id it holds, and raises
  `ContainmentError` when removal cannot be confirmed (ADR-0014, ADR-0022).
- **What has been pushed stays pushed.** Ilmar never force-pushes, resets or
  deletes a branch, and publishes exactly once (ADR-0022, I1 and decision 5).
- **The event log reserves `execution.cancel_requested` and
  `execution.cancelled`** for this issue (ADR-0024 §5).

The design must say:
- who may request a cancellation, and how the request reaches an owner that
  is busy;
- how the request becomes durable;
- what happens in each state, especially during an agent run;
- how crashes at every point are recovered;
- how sandbox containment is kept.

## Considered options

**How a request reaches the owner**

1. **`ilmar cancel` takes the instance lock and acts itself.** It cannot run
   while a pass holds the lock, so it could never stop a running agent. If it
   could, it would act on an execution whose lease another worker holds
   (level 2).
2. **`ilmar cancel` writes into the execution store without the lock.** This
   breaks level 1 for the store itself. Store writes are fenced by the owner's
   epoch, which a CLI does not have.
3. **A tracker label or comment as the request.** Labels and comments carry
   no provenance (`docs/workflow.md`), so anyone who can triage could stop any
   execution. A task that becomes ineligible on the tracker is #12's case
   (F-001). #12 uses this ADR's mechanism with the coordinator as the actor.
4. **A request inbox in the coordinator's private directory, applied by the
   lease holder.** `ilmar cancel` only *submits* a request, a file under
   `coordinator_root`. The coordinator, holding the lock and the execution's
   lease, *applies* it: it records the request durably, then acts.

**How a running agent is stopped**

1. **Wait for the agent to finish or time out.** This is safe but slow: a
   cancellation could wait for the whole agent timeout.
2. **Signal the process inside the container** (graceful). The process may
   ignore the signal, and killing the exec client leaves it running (F-024).
3. **A second thread removes the sandbox.** This breaks the lease (level 2)
   and races the worker's own journaled stop.
4. **A stop signal handed to the running call**, which ends the run exactly
   like a timeout: the runtime kills only its own exec client's process
   group, removes the whole sandbox by the container id it holds, and
   confirms containment. The worker then performs its normal journaled,
   ownership-verified stop.

**Where a pending cancellation is kept**

1. **A new column on the execution row.** This needs a schema migration. The
   row can only change with a state transition, so the commit model would
   need changing.
2. **The durable `execution.cancel_requested` event**, written in a fenced
   commit, read back when deciding. ADR-0024 lets durable events drive
   decisions. The event changes no row, so I11 is unaffected.

## Decision

Chosen options: request option 4, stop option 4, record option 2.

### 1. Requests: submitted by an operator, applied by the owner

- **The command:** `ilmar cancel --config PATH EXECUTION_ID --as NAME`.
  - `--as` is required. It is the person's short lower-case label, with the
    same rule as an event's `actor.ref` (ADR-0024 §7). An invalid label is
    refused.
  - There is no free-text reason. The event records the stable code
    `cancel-requested`.
- **What it does, without the instance lock:**
  1. It opens the store read-only (ADR-0024 §8).
  2. It checks that the execution exists and is not terminal. If it is
     terminal, it exits 3 and changes nothing.
  3. It checks that no cancellation was already accepted for it (an
     `accepted` `execution.cancel_requested` event). If one was, it reports
     that request and exits 0 without writing.
  4. It publishes one request file,
     `<coordinator_root>/requests/cancel/<execution-id>.json`, by the
     protocol in section 1a. **The first request wins:** an existing request
     is never overwritten. A later submitter reports the request that stands
     (its id and actor) and exits 0.
  5. It reports when the request takes effect: in the running pass, or at
     the next `ilmar run --once`.
- **This is an explicit, narrow addition to ADR-0012 level 1.**
  - Submitting a request changes nothing that Ilmar reads without
    validating it.
  - Nothing in the store, the ledger or the outside world changes until the
    coordinator, holding the lock, applies the request.
  - Every other mutating command still needs the lock.
- **Authorisation is the operating system's:**
  - `coordinator_root` belongs to the account that runs Ilmar.
  - It is never mountable into a sandbox (`WorkspaceLayout.check_mountable`).
    An agent therefore cannot cancel its own execution or another one.
  - Tracker users cannot cancel through this path.
  - Remote or multi-user cancellation needs its own ADR (see "Revisit when").
- **The coordinator applies a request** at each safe point (section 3).
  **The worker holding the execution's lease is the only component that
  applies a request durably.**
  - It reads the request through the validation routine of section 1b. A
    request that does not pass is never applied.
  - It records `execution.cancel_requested` in one fenced commit, under the
    lease:
    - `actor` is human, with the label as `ref`;
    - the reason is `cancel-requested`;
    - `data` holds `request` (the request id) and `decision`: `accepted`,
      `too-late`, `already-terminal`, or `duplicate` (a cancellation of this
      execution was already accepted).
  - Only after that commit does it remove the file, by the cleanup rules of
    section 1a.
  - An entry that fails validation is moved aside by the worker, at a safe
    point and never by the watcher, and reported. It is never acted on
    (section 1b).
- **A pending cancellation** is an `accepted` `execution.cancel_requested`
  event on an execution that is not terminal yet. Every decision point reads
  it from the store. The inbox file is only the way in.
- **Store change:** `Commit` gains `extra_events`, as ADR-0024 §9 foresaw.
  - Only registered types that no row derives may be given, and they are
    validated like every event.
  - The pairing check (ADR-0024 §2) ignores them; they must name an execution
    the commit changes or the store holds.
  - `execution.cancel_requested` becomes constructible, and the registry
    gains the code `cancel-requested`. #12 adds `task-ineligible` with the
    coordinator as actor.

### 1a. The request file protocol: first request wins, never overwritten

**The inbox:** `<coordinator_root>/requests/cancel/`.
- The CLI creates `requests/` and `requests/cancel/` with mode 0700 if they
  are missing. It walks to them from `coordinator_root` with
  `openat(…, O_DIRECTORY | O_NOFOLLOW)` at every level.
- If any level is a symlink, not a directory, not owned by the effective user,
  or open to group or others, it is refused. The CLI exits 3 and the
  coordinator ignores the whole inbox, reporting why. Nothing is repaired
  automatically.

**Names:**
- **The final name** is derived only from the execution id, which is already
  restricted to `[A-Za-z0-9][A-Za-z0-9_-]{0,63}`:
  `<execution-id>.json`. No name is ever taken from a directory listing to
  decide which execution a request is for.
- **A temporary name** has the form `.tmp-<execution-id>.<request-id>`. The
  request id is `q-` followed by 32 random hex digits, from `secrets`.

**Publishing a request, by the CLI:**
1. Create the temporary file with
   `openat(inbox, tmp, O_CREAT | O_EXCL | O_WRONLY | O_NOFOLLOW | O_CLOEXEC,
   0o600)`. `O_EXCL` means a stray entry with that name is never reused or
   followed.
2. Write the canonical JSON request:
   - a schema tag, `ilmar.cancel-request/1`;
   - the request id;
   - the execution id;
   - the actor label;
   - when it was written, for people only (ADR-0022, "Time").

   At most 1 KiB. Then `fsync` the file.
3. **Publish it with `linkat(inbox, tmp, inbox, final, 0)`**: a hard link,
   without following symlinks.
   - The link is created atomically, and it **fails with `EEXIST` if the final
     name exists**, whatever is there: a request, a symlink or anything else.
   - Unlike `rename`, it can never replace an existing entry. Of any number of
     concurrent submitters, exactly one link succeeds, so **the first request
     wins**.
4. Remove the temporary name (`unlinkat`), then `fsync` the inbox directory.
5. **On `EEXIST`,** the CLI removes its own temporary file, then reads the
   existing entry through the validation routine (section 1b).
   - A valid request: it reports that request (id, actor) and exits 0.
   - Anything else: it reports that the inbox holds an invalid entry for this
     execution, which the coordinator will move aside, and exits 3. It never
     replaces or removes that entry itself.

**Crash cleanup.** Every intermediate state is either inert or a complete,
valid request. Nothing relies on timestamps.

| Crash point | What remains | Effect and cleanup |
| --- | --- | --- |
| CLI, before or while writing the temporary file | `.tmp-<execution>.<request>`, perhaps partial | Inert: nothing ever reads a temporary name as a request. A later submission uses a new request id, so no name collides. The worker removes the execution's temporary files once the execution is terminal, or when it applies a request for it (below). |
| CLI, after `linkat`, before removing the temporary name | the final name and the temporary name, one inode (`st_nlink == 2`), a complete, `fsync`ed request | A valid request (section 1b accepts exactly this second link). When the worker removes the request, it also removes the companion temporary name. |
| Worker, after recording, before renaming the file | the request file | At the next safe point, its request id is already recorded, so it is cleaned up (below) and nothing is recorded twice. |
| Worker, after renaming to `.applying-…`, before unlinking it | `.applying-<execution>.<random>` | Inert. At the next safe point it is verified: a recorded request id is unlinked; anything else is moved to `rejected/` (below). |
| Worker, between the two unlinks | the temporary name | Inert; removed as in the first row. |

**Removal by the worker: the check-then-unlink race.**

POSIX gives **no atomic compare-and-delete**:
- `unlinkat` and `renameat` act on whatever entry has the name at the moment
  of the call. Neither can be made conditional on the inode or the content
  seen by an earlier `fstat` or read.
- Checking an entry and then unlinking its name therefore always leaves a
  window in which the name can be pointed at something else.
- Linux's `renameat2(RENAME_NOREPLACE)` is not in Python's standard library
  (ADR-0003), and it would not close this window either.

This ADR does not claim such a guarantee. It states who can write the inbox,
and it makes every removal safe under that assumption.

*Trust assumptions for writers to the inbox*, enforced as far as the
operating system allows (section 1b, step 1):

| Writer | May do | Never does |
| --- | --- | --- |
| `ilmar cancel` (any number at once) | create its own `.tmp-<execution>.<request>` name with `O_EXCL`; publish it with `linkat`, which never replaces; unlink its own temporary name | rename, replace or unlink a final name, or anyone else's temporary name |
| The worker (one per instance: it holds the instance lock, and the execution's lease) | everything below, and only under the lock | delete an entry it has not verified at a private name it chose |
| The operator account, by hand | anything; it owns the directory | — (outside the protocol, trusted) |
| Agents and sandboxes | nothing: `coordinator_root` is never mountable | — |

Under these assumptions:
- only the worker moves or deletes a **final name**;
- the only way a final name can change between the worker's check and its
  action is a **manual change by the operator** (or by an `ilmar cancel` that
  was modified to break the protocol, which is the same account).

The design does not try to win that race. It makes sure that losing it
**never destroys an entry the worker did not verify**, and never records
anything it did not validate.

*Cleanup is rename first, verify after*, so that what is deleted is exactly
what was verified:
1. **Validate** the request (section 1b) and **record** it durably (one
   fenced commit; idempotent by request id).
2. **Take the entry out of the shared name:**
   `renameat(inbox, "<execution-id>.json", inbox,
   ".applying-<execution-id>.<random>")`.
   - The rename moves whatever is at the final name at that instant to a name
     only the worker uses, chosen with 128 random bits.
   - `ENOENT` means there is nothing to clean.
   - The name's uniqueness rests on its randomness, not on an atomic
     no-replace, and the ADR says so.
3. **Verify the moved entry:**
   - open `.applying-…` with `O_NOFOLLOW | O_NONBLOCK`;
   - `fstat` it, and read and digest it;
   - compare it with what was validated in step 1: `st_dev`, `st_ino` and the
     content digest.
4. **If it matches,** unlink `.applying-…`. Nobody else uses that name, so
   this unlink removes exactly what was verified. Then, if the companion
   `.tmp-<execution>.<request>` still exists with the same inode, unlink it;
   `ENOENT` is fine, because its own CLI may have removed it first.
5. **If it does not match** (the operator swapped the entry in the window):
   - The worker **does not delete it.** It renames it to
     `rejected/<execution-id>.<random>` and reports it:
     - as invalid, if it fails validation;
     - or as "a different request, not applied: resubmit", if it passes.
   - A symlink is moved as a symlink; its target is never touched.
   - The recorded request stays recorded, because it is what was validated
     (and, during a run, what stopped the agent).

*Moving an invalid entry aside* uses the same rename-first rule:
- the final name is renamed to `rejected/<execution-id>.<random>`, and the
  moved entry is then validated again;
- if, because of the same race, the moved entry turns out to be a **valid**
  request, it is kept in `rejected/` and reported as "a valid request moved
  aside by a concurrent change: resubmit". It is not restored automatically,
  because a restore would be another unconditional rename.

*Crash cleanup for these steps:*
- A crash leaves at most an `.applying-<execution>.<random>` name, which is
  inert: section 1b never reads it as a request.
- At the next safe point, the worker verifies each such name for that
  execution:
  - an entry whose request id is already recorded is unlinked;
  - anything else is moved to `rejected/` and reported.
- A temporary file that a concurrent CLI is still writing is removed only
  when the execution is already terminal or a request was already applied,
  by the same rename-first rule. That CLI's `linkat` then fails, and it exits
  3. It never publishes a half-written request.

### 1b. Validation, shared by the watcher and the worker

One read-only routine, `read_cancel_request(inbox_fd, execution_id)`, decides
whether a valid request exists **for that execution**. It never writes. Both
the watcher and the worker call it, and nothing else reads the inbox.

1. **The path is safe:** `inbox_fd` was opened as section 1a describes, with
   no symlinks, the right owner and mode 0700.
2. **The entry is opened safely:**
   - `openat(inbox_fd, "<execution-id>.json", O_RDONLY | O_NOFOLLOW |
     O_NONBLOCK | O_CLOEXEC)`;
   - a symlink at the final name fails to open (`ELOOP`), and a FIFO or device
     does not block the reader.
3. **The opened file passes `fstat`:**
   - a regular file;
   - owned by the effective user, with no group or other permission bits;
   - at most 1 KiB;
   - `st_nlink == 1`, or `2` only when the second link is the matching
     `.tmp-<execution-id>.<request-id>` with the same `st_dev` and `st_ino`. A
     foreign hard link is refused.
4. **The content is read once,** at most 1 KiB plus one byte, from the opened
   descriptor. Every later check uses these bytes; the name is never read a
   second time.
5. **The content is strict:**
   - UTF-8 JSON with no duplicate keys;
   - exactly the expected keys, the right schema tag and canonical form;
   - a well-formed request id;
   - **an execution id equal to the one the caller asked for** (the file
     name alone is not enough);
   - an actor label that passes ADR-0024's `actor.ref` rule.

   Nesting deeper than the format has, oversized numbers and NaN are refused.
6. **The result** is either "no request" or an immutable `CancelRequest`:
   request id, actor, execution id, content digest, `st_dev` and `st_ino`.
   **Anything that fails a check is "no request"**, together with a reason
   for the report. It is never an error that stops a pass.

**The watcher:**
- It signals only when this routine returns a `CancelRequest` for **the
  execution whose agent is running**. Any invalid, malformed, oversized,
  symlinked, hard-linked, foreign-owned or mismatched entry, or an unsafe
  inbox, **never interrupts the agent**. The run continues as if nothing were
  there.
- It hands the validated `CancelRequest` to the worker through a
  single-assignment slot together with the stop signal. It never writes,
  never moves or removes an entry, and calls no port method.
- It reports invalid entries at most once per run, on the process's log
  stream, and nothing durable.

**The worker** calls the same routine at each safe point, and after a
stopped run:
- It records the request the watcher validated, which is what stopped the
  run. It then cleans up by the rename-first rule of section 1a, which never
  deletes an entry it has not verified at its private name.
- If the routine finds an invalid entry at a safe point, the worker moves it
  aside, which is the only place an invalid entry is touched:
  - it renames the entry itself, never a symlink's target, within the inbox
    to `rejected/<execution-id>.<random>`;
  - it then validates the moved entry again (section 1a, "Moving an invalid
    entry aside").

  It reports it and records nothing durable.

### 2. Stopping a running agent: a stop signal on the running call

- **Port change:** `ContainerRuntime.run(…, stop: StopSignal | None = None)`.
  - `StopSignal` is read-only: `is_set() -> bool`, which `threading.Event`
    satisfies.
  - The runtime checks it at least once a second while the command runs.
  - When it is set, the runtime does **exactly what it does on a timeout**:
    - it kills its own `podman exec` client's process group, never any other
      process;
    - it removes the whole sandbox, with its proxy and network, by the
      container id it already holds;
    - it confirms the removal. If removal cannot be confirmed,
      `ContainmentError` is raised, and the sandbox is quarantined.

    It returns `CommandResult(cancelled=True)`.
- **`AgentRunner.run(…, stop=)`** passes the signal through.
  `AgentOutcome.CANCELLED` is new. The run's journaled operation then ends
  `SUCCEEDED`, with the outcome `cancelled`: the run ended, and how it ended
  is known.
- **A watcher thread** runs only while the agent runs. Every 2 seconds it
  calls the validation routine (section 1b) for this execution, and sets the
  signal **only for a valid request for this execution**.
  - It writes nothing, removes nothing and calls no port method.
  - The stop itself happens inside the journaled `run-agent` effect the
    worker started, so the watcher adds no new, unjournaled effect.
  - The request is recorded by the worker right after the run returns. That
    is the request the watcher validated, handed over with the signal.
- **After the run,** the worker performs its normal journaled sandbox stop,
  `remove_owned` with ownership verification (ADR-0022). That stop finishes
  any leftover, or finds nothing.
- **The worker never freezes** a cancelled run's workspace: agent output
  never becomes a candidate. The workspace is kept for inspection, like a
  failed attempt's (ADR-0021).

### 3. Where a cancellation takes effect

The worker checks for a pending cancellation, and for a request in the inbox,
at **every safe point**:
- before each step;
- before a new attempt, a retry, a freeze, a verification run and the first
  push;
- during the agent run, through the watcher.

| State when applied | What happens before CANCELLED |
| --- | --- |
| QUEUED | No claim intent recorded: nothing to undo. A claim intent recorded: it is settled first (it is repeatable), then the claim is released. |
| CLAIMED, RETRY_WAIT | The claim is released. Earlier attempts' workspaces stay. |
| PROVISIONING | An unfinished directory step is settled as it is today. No sandbox is started. Workspaces stay. |
| RUNNING_AGENT, no run yet | A started sandbox is removed through the journaled, ownership-verified stop. The agent is never run. |
| RUNNING_AGENT, run in progress | The stop signal ends the run (section 2), then the normal stop. Never frozen. |
| RUNNING_AGENT, run recorded | Not frozen, even if the agent completed. The sandbox is removed as usual. |
| VERIFYING | A verification run in progress finishes or times out: it is bounded, and it works on a separate checkout. No further run starts. Its sandboxes are removed through the ledger, and evidence is kept. |
| PUBLISHING, before the first `publish-revision` intent | Nothing has been pushed. The candidate and evidence are kept. |
| PUBLISHING, a `publish-revision` intent exists | **Too late.** The request is recorded as `too-late`, and publishing completes exactly once (I1). The human closes the change request, which releases the claim through the existing path (ADR-0021). |
| Terminal | Recorded as `already-terminal`. Nothing changes. |

**Ending:**
- the transition into CANCELLED carries the reason `cancel-requested`;
- the worker posts a note on the task and releases the claim, both
  journaled, with step `cancelled`;
- if quarantined resources remain, the execution is BLOCKED and the claim
  kept for a human, as for a failure;
- a cancelled task is **never started again automatically**, as for a
  failure. A human resets it (#12).

The transition event is the record of the cancellation. The reserved
`execution.cancelled` stays unused, because a second event for the same row
would break ADR-0024's one-event-per-changed-row pairing.

**Precedence:** the first transition committed wins. A failure recorded
before a cancellation is applied stays a failure, and the request is then
recorded as `already-terminal`. A cancellation never turns into a failure.

### 4. Fencing

- Every durable step is a fenced commit by the execution's lease holder, and
  every effect is journaled with ownership verification, exactly as today:
  - recording the request;
  - removing sandboxes;
  - releasing the claim;
  - the transition.
- The watcher writes nothing. The CLI writes only its request file.
- A fenced-out coordinator (`FencingError`) applies nothing. Its pending
  request files stay for the new owner.
- A request file is deleted only after its event is committed. Because the
  request id is recorded, applying the same file twice records it once.

### 5. Crash recovery

| Crash | Recovery |
| --- | --- |
| CLI, while publishing a request | See section 1a: an inert temporary file, or a complete request. |
| Before the request is recorded | The file is still there. It is applied at the next safe point. |
| After it is recorded, before the file is deleted | The request id is already recorded, so the file is deleted and nothing is recorded twice. |
| During the stop of a running agent | `run-agent` is `INTENDED`: the existing rule abandons it ("interrupted") and removes the sandbox with `remove_owned`. The pending cancellation then ends the execution, **instead of** starting a new attempt. |
| After CANCELLED, before the claim is released | The execution still `needs_attention` (ADR-0022): the next pass posts the note and releases the claim. |

**A new invariant I12 joins ADR-0022's list, checked at every crash point:**
- Once a cancellation is accepted, no agent run, freeze, verification run or
  first push starts for that execution.
- Every sandbox ends `RELEASED` or `QUARANTINED`.
- The execution ends CANCELLED, or BLOCKED with quarantined resources.
  `too-late` requests excepted, it never publishes.

### 6. What does not change

- Unattended operation stays disabled. `ilmar cancel` is a human command,
  and nothing schedules it.
- No change to the task tracker port. #12 adds ineligibility.
- `SUCCEEDED` executions with an open change request are not cancelled by
  this command. Closing the change request already releases the claim.

### Tests the implementation must add

- **Contract:**
  - `ContainerRuntime.run` with a stop signal on the fake and on **real
    Podman**: the process inside the container is gone, the sandbox,
    proxy and network are removed, and `cancelled` is reported;
  - an unconfirmed removal raises `ContainmentError`;
  - `AgentRunner` passes the signal through.
- **Coordinator:** cancellation in each row of section 3's table, with the
  events, the claim and the ledger checked. The `too-late` path publishes
  exactly once. A cancelled task is never picked again.
- **Crash suite:** a "cancelled" scenario at every write and every effect
  crash point, including during the agent run. It checks I4, I5, I11 and
  I12.
- **CLI:**
  - a request is refused for a terminal or unknown execution, or an invalid
    `--as`;
  - submitting needs no lock and changes nothing else (the store is
    byte-for-byte unchanged).
- **Acceptance, first request wins (section 1a), adversarial:**
  - **Concurrent submissions:** at least 16 processes start together, each
    `ilmar cancel` for the same execution with a different `--as`.
    - Exactly one final file exists, byte for byte one submitter's request.
    - Every process exits 0 and reports that same request id and actor.
    - No temporary file is left behind.
    - The worker then records exactly one `execution.cancel_requested`.

    It is repeated many times to exercise the race.
  - **No overwrite:** with any entry at the final name (a valid request, an
    invalid file, a symlink to a valid request elsewhere, a directory or a
    FIFO), a submission leaves that entry byte-for-byte and inode-for-inode
    unchanged. It exits 0 for a valid request, otherwise 3.
  - **Crash at each step of the protocol:** a real process is killed with
    `os._exit` after creating, after writing, after `fsync`, after `linkat`,
    and after removing the temporary name. Each time:
    - the inbox holds nothing, an inert temporary file, or one valid request;
    - a later submission behaves as specified;
    - the worker applies at most one request and removes the leftovers by
      the cleanup rules.
  - **Worker crashes:** after recording and before removal, and between the
    two removals, the request is recorded once and the leftovers are
    cleaned.
  - **An unsafe inbox:** `requests/` or `requests/cancel/` as a symlink, open
    to group or others, or owned by another user (simulated by an injected
    owner check). The CLI refuses with exit 3 and writes nothing.
- **Acceptance, the watcher never interrupts on a bad entry (section 1b),
  adversarial.** For each entry below, placed while a scripted agent runs, the
  agent runs to completion, no stop signal is set, no event is recorded, and
  the worker later moves the entry aside without touching any symlink's
  target:
  - a symlink at the final name, pointing at a valid request elsewhere;
  - a symlinked inbox directory;
  - a FIFO, which must not block the watcher, and a directory;
  - a hard link to a valid request with no matching temporary name;
  - mode 0644, and a foreign owner (by the injected check);
  - an oversized file;
  - truncated or non-UTF-8 content, and non-canonical JSON;
  - duplicate or extra keys, a wrong schema tag, a malformed request id;
  - an invalid actor label;
  - deep nesting, huge numbers and NaN;
  - **a valid request whose execution id names a different execution**
    than its file name, and a valid request for another execution, which
    must not stop this one.

  Also:
  - **Swapped after validation:** a valid request is replaced by another
    entry between the watcher's validation and the worker's cleanup. The
    worker records the request that stopped the run and deletes nothing it
    did not verify at its private name (see the race tests below).
- **Acceptance, the check-then-unlink race (section 1a), adversarial.** A test
  hook runs a concurrent change **exactly in each window**:
  - between validation and recording;
  - between recording and the rename to `.applying-…`;
  - between that rename and the verification;
  - between the verification and the unlink.

  The concurrent changes are:
  - replace the final entry with a different valid request, an invalid file,
    a symlink to a valid request, or a FIFO;
  - delete it;
  - remove the companion temporary name (as its own CLI would).

  For every window and change:
  - the validated request is recorded **exactly once**;
  - **every entry that was not verified survives** byte for byte, either at
    the final name or under `rejected/`, with a report saying why;
  - no symlink target is touched;
  - `ENOENT` on the companion or the entry is tolerated;
  - I11 holds, and no other event is recorded.
- **A race stress test:** a thread swaps entries at the final name in a tight
  loop while the worker applies and cleans up, over many iterations. Every
  entry the swapper created is accounted for at the end: the one verified
  request is deleted and everything else is preserved. A request is never
  recorded twice, and nothing unvalidated is ever recorded.
- **Crash during cleanup:** a killed worker after the rename to `.applying-…`
  leaves an inert entry. The next safe point unlinks it if its request is
  recorded, and otherwise moves it to `rejected/`.
  - **Positive control:** a valid request stops the run within the watcher
    interval and the stop's own bound. Exactly that request is recorded,
    with its actor, and the file and its companion are removed.
- **Architecture:**
  - the request inbox is inside `coordinator_root`;
  - no code path other than the worker records `execution.cancel_requested`
    or moves or removes inbox entries;
  - the watcher calls no port method and no writing filesystem call;
  - `read_cancel_request` is the only code that reads the inbox.

## Consequences

- **Good:**
  - An operator can stop a running agent within seconds, without a second
    owner, through the same containment path a timeout uses.
  - Every cancellation is durable, attributed to a person, and explained by
    its events.
  - #12 reuses the mechanism for ineligible tasks.
- **Good:** no schema migration. The pending state is a durable event, and
  I11 is unaffected.
- **Bad:** ADR-0012 level 1 gains a narrow exception for submitting a
  request, which needs the maintainer's explicit approval. The exception is
  limited to one directory, never mountable, holding only requests that the
  owner validates.
- **Bad:** two port signatures change (`ContainerRuntime.run` and
  `AgentRunner.run`). The container runtime port is a sensitive area
  (AGENTS.md §6), so the change needs independent verification on real
  Podman.
- **Bad:** a cancellation that arrives after the first push cannot undo it.
  It is recorded as `too-late`, and the human closes the change request.
- **Bad:** a verification run in progress is not interrupted. It finishes
  within its own timeout.
- **Bad:** the protocol depends on POSIX `linkat`, `openat`, `O_NOFOLLOW` and
  per-file ownership. These hold on the local filesystems ADR-0005 already
  requires; a filesystem without hard links is refused at submission (exit
  3).
- **Residual risk:** there is no atomic compare-and-delete in POSIX, so a
  check and a later removal by name can always be raced. The design narrows
  who can race it, and keeps the race harmless:
  - Only the worker removes final names. CLIs never replace or unlink them,
    and agents cannot reach the inbox, so only a manual change by the
    operator account can race the worker.
  - Rename-first cleanup then deletes only what it verified at a private
    name. A swapped-in entry is preserved and reported, never lost. A valid
    request swapped in that way is not applied and must be resubmitted.

  The operator account can always rewrite its own inbox, and can already
  stop Ilmar. Validation protects the coordinator from malformed or unsafe
  entries and from anything it did not validate, not from that account.

## Revisit when

- Cancellation must come from people without access to the coordinator's
  account: tracker users, a web interface, several operators. That needs
  authenticated requests, so a new ADR.
- Coordinators run on several hosts (ADR-0012's own trigger). The inbox would
  need to become a shared, authenticated queue.
- Verification runs become long enough that waiting for them is a problem.
  The stop signal could then reach the verifier too.
- Podman gains a way to bound an exec session's lifetime (F-024).

## Clarification (2026-11-09, #16 part 1 review)

Section 1a lets the worker remove an execution's temporary files once the
execution is terminal or a request for it was applied. A submitter can still
be running at that moment: the worker may then remove its temporary file
before the submitter's `linkat`. Section 1a does not say what the CLI does
then. This clarifies section 1a; the decision is unchanged.

- **Nothing is published.** `linkat` fails with `ENOENT` because the source is
  gone. The final name is untouched and no request is lost or overwritten.
- **The CLI retries with bounded attempts.** It starts again from step 1
  with a new request id, at most three attempts in all. The same applies
  when step 5 finds the final name gone, which happens when the worker took
  the standing request between the `EEXIST` and the read.
- **After the last attempt, it fails closed.** The CLI reports that the inbox
  kept changing and exits 3. It never falls back to `rename` or to
  overwriting, and it never reports a request it did not see.

The worker's cleanup rules are unchanged.
