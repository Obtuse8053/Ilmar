# ADR-0004: Explicit execution state machine with versioned records

- Status: Accepted (PR #1, 2026-11-01); amended by ADR-0010 and ADR-0012
- Date: 2026-11-01
- Sources: [R1], [R3], [R7], [R9] · Findings: F-001, F-002, F-004, F-005, F-006

## Context and problem

MANIFEST §5 defines a ten-step workflow. §2.4 requires recovery, bounded
retries, cancellation and observability. §5 forbids accepting work just because
the agent says it succeeded. A coordinator that can crash at any point must
know, from durable state alone, where each execution stopped.

## Considered options

1. Implicit state: keep scheduler state in memory and rebuild it from the
   tracker on every poll. This is R1's approach; restarts lose retry timers
   (F-001).
2. An external workflow engine such as Temporal.
3. An explicit allow-list state machine in the core, with durable, versioned
   execution records and an append-only transition log.

## Decision

Chosen option: 3. Option 1 cannot resume work or retries after a crash, and §2.4
asks for recovery. Option 2 violates §2.5 (minimal infrastructure).

```
QUEUED ─► CLAIMED ─► PROVISIONING ─► RUNNING_AGENT ─► VERIFYING ─► PUBLISHING ─► SUCCEEDED
                         ▲   │              │              │            │
                         │   └──────────────┴──────┬───────┴────────────┘
                         │                         ▼
                         └──────────────────── RETRY_WAIT
Any non-terminal state ─► FAILED | CANCELLED
```

- Workflow steps map to states: discover = QUEUED, claim = CLAIMED, provision =
  PROVISIONING, agent and tests = RUNNING_AGENT, independent validation =
  VERIFYING, commit/push/change request = PUBLISHING. Recording is the
  transition log; recovery reads non-terminal executions back.
- `ExecutionStateMachine` is pure: it takes `now` and returns a new
  `(Execution, Transition)` pair.
- VERIFYING cannot be skipped. Several independent sources say agents'
  self-assessment is unreliable (F-004).
- Every change increments `version`, and stores reject stale writes
  (optimistic concurrency).
- Retries are bounded by `max_attempts`, which is checked both when entering
  and when leaving RETRY_WAIT. FAILED and RETRY_WAIT require an error.
- The execution ends at SUCCEEDED once a verified change request exists. Human
  approval of that request is outside this state machine.

## Consequences

- Good: crash recovery and duplicate prevention rest on durable state.
  Execution identity is kept across retries.
- Bad: two sources of truth, the tracker and the store. Reconciliation is
  required (F-001).

## Revisit when

- M3 adds backoff and stall detection. This needs a durable `retry_not_before`
  and a liveness signal (F-002).
- Planning or agent-review stages become configurable. They may become
  sub-states of RUNNING_AGENT and VERIFYING.
- Verification design changes after F-005, for example checks the agent cannot
  edit.
- Retries need context from earlier attempts (F-006).
