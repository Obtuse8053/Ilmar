# ADR-0025: Cancellation through a request inbox, applied by the lease holder at safe points

- Status: Proposed
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
  3. It writes one request file:
     `<coordinator_root>/requests/cancel/<execution-id>.json`.
     - The file is written atomically: a temporary file opened with
       `O_CREAT | O_EXCL | O_NOFOLLOW`, mode 0600, then renamed into place.
     - It holds a schema tag, a random request id, the execution id, the
       actor label, and when it was written. The time is for people only and
       never decides anything (ADR-0022, "Time").
     - A request already pending for that execution is left as it is, with
       exit 0.
  4. It reports when the request takes effect: in the running pass, or at
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
  - The worker reads the file, validates it (schema, a known and matching
    execution id, the actor label) and records `execution.cancel_requested`
    in one fenced commit, under the execution's lease:
    - `actor` is human, with the label as `ref`;
    - the reason is `cancel-requested`;
    - `data` holds `request` (the request id) and `decision` (`accepted`,
      `too-late` or `already-terminal`).
  - Only after that commit does it delete the file.
  - An invalid file is moved aside to `rejected/` and reported. It is never
    acted on.
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
- **A watcher thread** runs only while the agent runs. It reads the inbox
  every 2 seconds and sets the signal when a request for this execution
  appears.
  - It writes nothing and calls no port method.
  - The stop itself happens inside the journaled `run-agent` effect the
    worker started, so the watcher adds no new, unjournaled effect.
  - The request is recorded by the worker right after the run returns.
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
  - a second request is idempotent;
  - the file is written atomically, with mode 0600, and never follows a
    symlink;
  - an invalid request file is moved aside, never acted on;
  - submitting needs no lock and changes nothing else (the store is
    byte-for-byte unchanged).
- **Architecture:**
  - the request inbox is inside `coordinator_root`;
  - no code path other than the worker records `execution.cancel_requested`;
  - the watcher calls no port method.

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

## Revisit when

- Cancellation must come from people without access to the coordinator's
  account: tracker users, a web interface, several operators. That needs
  authenticated requests, so a new ADR.
- Coordinators run on several hosts (ADR-0012's own trigger). The inbox would
  need to become a shared, authenticated queue.
- Verification runs become long enough that waiting for them is a problem.
  The stop signal could then reach the verifier too.
- Podman gains a way to bound an exec session's lifetime (F-024).
