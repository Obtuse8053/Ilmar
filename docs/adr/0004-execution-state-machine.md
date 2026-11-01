# ADR-0004: Explicit execution state machine with versioned records

- Status: Proposed
- Date: 2026-11-01

## Context

MANIFEST §5 defines a ten-step workflow and §2.4 requires recovery, bounded
retries, cancellation and observability. §5 also requires that deterministic
verification not rely on the agent declaring success. A coordinator that can
crash at any point must be able to tell, from durable state alone, where each
execution stopped and what may happen next.

## Decision

Model each attempt to complete a task as an **execution** that moves through an
explicit set of states:

```
QUEUED ─► CLAIMED ─► PROVISIONING ─► RUNNING_AGENT ─► VERIFYING ─► PUBLISHING ─► SUCCEEDED
                         ▲   │              │              │            │
                         │   └──────────────┴──────┬───────┴────────────┘
                         │                         ▼
                         └──────────────────── RETRY_WAIT

Any non-terminal state ─► FAILED | CANCELLED
```

Workflow steps map to states as follows: discover (1) creates QUEUED; claim (2)
enters CLAIMED; provision (3) is PROVISIONING; agent and tests (4–5) are
RUNNING_AGENT; independent validation (6) is VERIFYING; commit, push and change
request (7–8) are PUBLISHING; recording (9) is the transition log; recovery (10)
reads non-terminal executions back from the store.

Rules, implemented in `ilmar.core.state_machine.ExecutionStateMachine`:

1. Transitions are an explicit allow-list (`ALLOWED_TRANSITIONS`). Anything else
   raises `InvalidTransitionError`. In particular, RUNNING_AGENT cannot skip
   VERIFYING.
2. The machine is pure. It takes `now` as an argument, never touches storage,
   and returns a new `(Execution, Transition)` pair.
3. Every change increments `Execution.version`. Stores accept an update only if
   it is exactly one version ahead of what they hold (optimistic concurrency),
   so two coordinators cannot both advance the same execution from the same
   state.
4. Every change appends a `Transition` with a human-readable reason, which
   gives an audit trail of each execution.
5. Retries are bounded. Retrying is only possible once work has started
   (PROVISIONING onwards). Entering RETRY_WAIT and resuming from it both check
   `max_attempts`, so lowering the limit takes effect for executions already
   waiting. `attempt` increments on RETRY_WAIT → PROVISIONING.
6. FAILED and RETRY_WAIT require an error description, stored in `last_error`.
7. Time must not go backwards for an execution.

## Consequences

- Recovery (M3) can query `list_in_states(non-terminal)` and resume or
  reconcile each execution from a known state.
- The same execution keeps its identity across retries, and the one-active-
  execution-per-task rule (ADR-0005) still holds while it waits.
- Planning and agent review (configurable per MANIFEST §5) are not states yet.
  They will be added when built, likely as sub-steps of RUNNING_AGENT and
  VERIFYING, through a new ADR.
- Waiting for human approval of a change request is outside the execution: the
  execution ends at SUCCEEDED once a verified change request exists. Approval
  policies (MANIFEST §2.2) will be designed in a later milestone.

## Prior art

- [R1] Symphony: the idea that issue-driven orchestration needs reconciliation
  and recovery against durable state.
- [R3] Anthropic, "Effective harnesses for long-running agents": the idea of
  durable progress artefacts that let work resume after interruption.

Ideas only, as summarised in MANIFEST §9. Neither source was accessed or copied
during M1. The state set and rules above are original to Ilmar.
