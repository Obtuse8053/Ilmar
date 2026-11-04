# ADR-0012: Recovery-safe external side effects

- Status: Accepted (PR #34, 2026-11-04)
- Date: 2026-11-04
- Sources: [R1], [R3] · Findings: F-001, F-006

## Context and problem

M3 adds restart recovery (#15), idempotent retries (#13) and handoffs (#14).
The M2 adapters and coordinator (#4–#11) are written first. If they create
containers, branches or commits that the durable state cannot find again, or
side effects that cannot safely be repeated, M3 would need a redesign.

## Considered options

1. Rely on reconciling with the tracker on every cycle, as [R1] does with
   in-memory state. This loses knowledge of a half-finished publish.
2. Use an external workflow engine. Rejected in ADR-0004.
3. Persist intent before acting, derive every resource name from persisted
   fields, and make each side effect idempotent or checkable.

## Decision

Chosen option: 3. The rules for M2 code:

1. **Write-ahead.** Before performing a state's external side effect, the
   coordinator persists the transition and every identifier the side effect
   will use: branch, candidate revision, container name. Each resource it is
   about to create (container, sandbox network, workspace directory) also gets
   a row in a **resource ledger** in the execution store: kind, name,
   execution id, attempt, and a random **ownership token**.
2. **Deterministic names.**
   - Workspace directories: `<root>/<execution-id>/<attempt>/…` (#4).
   - Container and sandbox network names: instance id, execution id and
     attempt.
   - Branch name: the sanitised task key.

   Execution ids are unique, so the names of two different executions never
   collide. A restarted coordinator can find these resources even if it
   crashed before recording a handle. Finding a resource is not the same as
   being allowed to destroy it; rule 3 decides that.
3. **Ownership verification before replacing or deleting.**
   - Every container and network is created carrying labels for the Ilmar
     instance id, execution id, attempt and ownership token.
   - The coordinator replaces, stops or removes a resource only if its labels
     match a ledger row of the execution it is acting for. If the labels are
     missing or differ, it does not touch the resource: the execution moves
     to FAILED with reason `resource-ownership-conflict` and is flagged for a
     human.
   - Workspace directories are deleted only if they are in the ledger and
     resolve under the configured root. Symlinks are never followed during
     deletion. Ownership markers live in the coordinator's store, not in
     directories the agent can write.
   - **Fencing.** In this single-coordinator design, ownership is held, not
     merely checked. A version check alone could go stale between the check
     and the operation, so the guarantee rests on three levels:
     1. **Instance.** The coordinator takes an exclusive OS lock on a file in
        its state directory (which must be on a local filesystem, ADR-0005)
        and holds it for its whole lifetime. Only the kernel releases it, when
        the process exits. Ownership of an instance therefore cannot change
        while the holder is alive, so no stale coordinator can run alongside
        a new one. Every command that changes anything, including manual CLI
        commands, needs this lock. Read-only commands do not.
     2. **Execution.** Inside the coordinator, at most one worker owns an
        execution at a time, through an in-process lease. Before each
        sensitive operation, the worker checks that it still holds the lease,
        that it still holds the instance lock, and that the stored execution
        version equals its last write. The lease cannot be taken from it
        between the check and the operation. Sensitive operations are: start,
        replace, stop or remove a container or network; claim or release a
        task; freeze a candidate; push; upsert a change request; delete a
        workspace.
     3. **Resource.** The operation itself targets the resource's *immutable*
        identity, captured during the ownership check: the container or
        network id whose labels matched, not its name; a push with an
        expected-head lease; a workspace directory confirmed by device and
        inode, deleted without following symlinks. If the identity changed,
        the operation fails as a conflict instead of acting on a replacement.

     If any check fails, the worker performs no further side effects for that
     execution. It records the conflict and stops. This needs no distributed
     consensus, because there is exactly one coordinator per instance and one
     owner per execution.
   - A retry (a new attempt) creates new resources under new names. It may
     clean up the previous attempt's resources only through the ledger and
     only after verifying ownership. It never touches resources of another
     execution or another Ilmar instance.
4. **Idempotent or checkable effects.**
   - Claiming a task is idempotent.
   - Starting a container whose derived name already exists replaces it only
     after ownership verification (rule 3); otherwise it fails as a conflict.
   - Freezing a candidate reuses the recorded `candidate_revision` if one
     exists.
   - Publishing a revision is a no-op if the remote head already matches;
     otherwise it uses a lease.
   - Upserting a change request is idempotent. It only ever edits change
     requests whose source branch is the execution's branch and which carry
     Ilmar's marker. It never edits ones a human opened.
   - Progress comments may be duplicated in M2. Make them detectable later.
5. **Per-state recovery**, implemented in #15, always subject to rule 3:

   | State | Recovery action |
   | --- | --- |
   | QUEUED, CLAIMED | Continue. |
   | PROVISIONING | Clean up the ledger resources this execution verifiably owns, then provision again. |
   | RUNNING_AGENT | Stop the container. The attempt counts as failed (RETRY_WAIT). |
   | VERIFYING | Verify the recorded candidate again. |
   | PUBLISHING | Repeat publish and upsert for the recorded candidate. |

6. **Handoff data.** Evidence records (ADR-0010) and agent summaries are stored
   per attempt, so #14 can pass them to the next attempt without a schema
   redesign.

## Consequences

- Good: M3 recovery becomes a lookup table over durable state rather than a
  redesign. Retries cannot overwrite human work, because of the publish lease.
- Good: a retry or recovery can never kill, replace or delete another
  execution's container, network or workspace. Conflicts stop work instead of
  destroying it.
- Bad: every adapter must expose idempotent operations, so contracts must test
  re-execution (#3, #5, #7).
- Bad: adds a resource ledger table (schema change), and runtimes must
  support labels. Podman and Docker do.

## Revisit when

- Coordinators run on several hosts. The per-instance lock would then need a
  distributed lease, and fencing tokens would have to be checked by the
  external systems themselves.
- #15 finds a state with no safe recovery action.
