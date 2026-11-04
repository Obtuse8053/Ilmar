# ADR-0012: Recovery-safe external side effects

- Status: Proposed
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
   will use: branch, candidate revision, container name.
2. **Deterministic names.**
   - The workspace path comes from a sanitised task key (#4).
   - The container name comes from execution id plus attempt.
   - The branch name comes from the sanitised task key.

   A restarted coordinator can therefore find and clean up these resources
   even if it crashed before recording a handle.
3. **Idempotent or checkable effects.**
   - Claiming a task is idempotent.
   - Starting a container replaces any container with the same derived name.
   - Freezing a candidate reuses the recorded `candidate_revision` if one
     exists.
   - Publishing a revision is a no-op if the remote head already matches;
     otherwise it uses a lease.
   - Upserting a change request is idempotent.
   - Progress comments may be duplicated in M2. Make them detectable later.
4. **Per-state recovery**, implemented in #15:

   | State | Recovery action |
   | --- | --- |
   | QUEUED, CLAIMED | Continue. |
   | PROVISIONING | Clean up the derived resources and provision again. |
   | RUNNING_AGENT | Stop the container. The attempt counts as failed (RETRY_WAIT). |
   | VERIFYING | Verify the recorded candidate again. |
   | PUBLISHING | Repeat publish and upsert for the recorded candidate. |

5. **Handoff data.** Evidence records (ADR-0010) and agent summaries are stored
   per attempt, so #14 can pass them to the next attempt without a schema
   redesign.

## Consequences

- Good: M3 recovery becomes a lookup table over durable state rather than a
  redesign. Retries cannot overwrite human work, because of the publish lease.
- Bad: every adapter must expose idempotent operations, so contracts must test
  re-execution (#3, #5, #7).

## Revisit when

- Coordinators run on several hosts.
- #15 finds a state with no safe recovery action.
