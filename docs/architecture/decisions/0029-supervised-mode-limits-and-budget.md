# ADR-0029: Mode 1 limits, accounting reservations, suspension and one credential path

- Status: Proposed
- Date: 2026-11-10
- Sources: [R14], [R15], [R21], [R27] · Findings: F-002, F-019
- Depends on: ADR-0028 (time), ADR-0030 (modes)
- Amends (by addition only): ADR-0021 (a fixed limit on an attempt's
  lifetime, enforced outside the coordinator), ADR-0023 (no credential path
  besides the one key).

## Context and problem

Mode 1 of the autonomy roadmap runs Ilmar in a loop under supervision
(ADR-0030). Before that is safe, spending and runtime need bounds that hold
across crashes, and that do not rely on numbers the agent reports.

Today:

- Ilmar records no usage.
- The model key enters the sandbox (the ADR-0006 exception).
- The agent timeout is enforced by the coordinator process (ADR-0014). If
  the coordinator dies, nothing stops the container. In Mode 1, restarts are
  manual, so a container could run unbounded until a human returns.

The roadmap fixes two principles:

- **Reservations are accounting, not cost limits.** They decide whether new
  work may start. They do not stop an execution that costs more.
- **The hard cost limit is the provider's spend limit** on a dedicated
  workspace ([R27]), reachable through exactly one credential.

## Considered options

**Accounting:**

1. *No local accounting; rely on the provider cap alone.* Rejected. The cap
   is monthly. One bad day could use most of a month.
2. *Charge what the agent reports.* Claude Code's JSON output includes a cost
   ([R15]). Rejected as a way to lower a charge: the agent runs untrusted
   code, and the value is missing when the agent crashes.
3. *Gateway metering first.* Rejected for Mode 1. It is the Mode 2 design
   (#94) and the longest single item on the roadmap.
4. *A fixed, conservative reservation per attempt, never reduced in Mode 1.*
   **Chosen.**

**Bounding an attempt's lifetime if the coordinator dies:**

1. *Rely on recovery to remove the container after a restart.* Rejected.
   Mode 1 restarts are manual, so this bound is unknown.
2. *A lifetime enforced by the container runtime or inside the container,
   independent of the coordinator.* **Chosen.**

## Decision

### 1. Independent limits on every attempt

All of these limits hold even if the budget is wrong:

| Limit | Enforced by | Survives a coordinator crash |
| --- | --- | --- |
| `execution_wall_seconds` | The coordinator, as today, **and** Podman's `--timeout` set at container creation, which conmon (the per-container monitor, not the coordinator) enforces ([R21], to be confirmed in #90) | Yes, from the runtime side |
| `stall_timeout_seconds` | The coordinator: no agent output for this long ends the attempt (#13) | No. The runtime lifetime covers it |
| `max_turns` | Passed to the runner ([R15]) | Yes, it is the agent's own limit |
| `max_attempts` | The existing attempt counter. A new attempt and owner token are created before the container starts (ADR-0021), so a crash consumes the attempt. The code cap of 3 stays | Yes |
| Backoff | `retry_not_before`, persisted (#13, ADR-0028) | Yes |
| Concurrency | `max_concurrent_executions` must be 1 in Mode 1 | Yes |

A recovered execution is never re-run under the same attempt (ADR-0021,
ADR-0022). This ADR adds a test of that rule at every new step.

**The runtime-side lifetime is required.**
- **Podman.** The Podman adapter passes `--timeout` with
  `execution_wall_seconds` plus a small grace period, so the coordinator's
  own kill normally acts first.
- **Proof.** A real-Podman crash test must show the container stops after
  the coordinator process is killed, with no coordinator running.
- **No fallback without approval.** If #90 finds that `--timeout` does not
  hold on the supported Podman versions, Mode 1 stays disabled until a
  replacement is approved. A `timeout` wrapper as the container's entrypoint
  is the first candidate, but it needs that approval.

### 2. Accounting reservations

- **Unit.** Amounts are stored as integer micro-dollars (USD). The
  configuration takes decimal dollars.
- **Reserve.** Before each agent run, in the same transaction that starts
  the attempt, Ilmar writes a `budget_ledger` row:
  - the attempt;
  - the amount, `reserve_per_execution`;
  - its UTC day and month (ADR-0028).

  The row is committed before the container exists.
- **Admit.** The attempt is admitted only if the period sums plus this
  reservation stay within `daily` and `monthly`. Otherwise Ilmar suspends
  with `budget-daily` or `budget-monthly`.
- **Never reduced in Mode 1.** Nothing trusted can measure the real cost, so
  no row is lowered, released or refunded. That includes after a crash, a
  cancellation, a timeout or an early exit.
- **Agent-reported cost may raise a charge, never lower it.** If the
  runner's result reports a cost above the reservation, the difference is
  recorded as an extra charge.
- **Verification is not charged.** It runs without model access
  (ADR-0011).
- **The provider's spend cap limits actual cost.** The configuration
  requires the operator to attest it (`provider_cap_attested`), and it may
  not exceed `monthly`.

### 3. Suspension

A durable `suspension` record stops all new work. Its reasons are:
`budget-daily`, `budget-monthly`, `provider-cap`, `kill-switch`, `operator`
and `clock-anomaly`.

- **Effects:**
  - no new claim and no new container;
  - a running agent is cancelled at its next safe point through the existing
    cancellation path (ADR-0025);
  - the loop exits with code 78 (ADR-0030).
- **Resume:** `ilmar resume` clears the record only if its cause is gone (a
  new budget day for `budget-daily`, no kill file for `kill-switch`). A
  budget suspension never lifts by itself.
- **Events:** the suspension and the resume are events in the same commit
  (ADR-0024).

### 4. Kill switch

- **The file.** `kill_switch_file` names a path that the configuration
  validation checks is outside every workspace, repository and sandbox mount.
- **When it is checked:**
  - before each claim;
  - before each container start;
  - between passes;
  - by the cancellation watcher while an agent runs.
- **Effect:** while the file exists, Ilmar suspends with `kill-switch`.
- **Without the file:** `ilmar suspend` sets the same suspension from the
  command line.

### 5. One credential path

- **The key.** The model credential is one API key, read from the variable
  named by `model_key_env` (`ANTHROPIC_API_KEY` by default). The key is
  created in a dedicated, non-default Anthropic workspace with a monthly spend
  limit ([R27]). Ilmar passes it to the sandbox as today (ADR-0023: in
  memory, never on disk).
- **Refused at container start.** Ilmar refuses the start if the container's
  environment or mounts would give the agent any other route to a model
  ([R14]):
  - `ANTHROPIC_AUTH_TOKEN`;
  - `apiKeyHelper` settings;
  - Claude login or OAuth credential files;
  - Bedrock or Vertex settings;
  - an `ANTHROPIC_BASE_URL` other than the configured gateway (Mode 2).

  The check runs on the container specification Ilmar builds, and is
  repeated in the contract tests for the runner.
- **Provider errors, fail closed.** According to Anthropic's documentation
  (as cited by the maintainer; [R27]), a configured workspace spend limit
  returns HTTP 400, and a usage-tier limit returns HTTP 429.

  | Error | Handling |
  | --- | --- |
  | HTTP 400 identified as the workspace spend limit | Suspend with `provider-cap`; never retried |
  | HTTP 429 that indicates a spend or usage limit | Suspend with `provider-cap`; never retried |
  | HTTP 429 that is an ordinary rate limit | Transient: the attempt ends and backoff applies (#13). It counts as an attempt and keeps its reservation |
  | Any authentication, authorization or billing error the runner cannot classify | Suspend; never retried |

  Claude Code may retry inside its own run. Ilmar classifies only the final
  outcome.
- **Verified enforcement (Gate 1, #93).** The operator records the
  workspace's limit. A drill against a scratch workspace with a minimal limit,
  run through Claude Code, must establish:
  - the exact status and error body Claude Code surfaces at the cap;
  - that Ilmar suspends rather than retrying;
  - how far spending went past the cap (below).
- **Exact cost containment is not proven.** The provider's limit is the hard
  limit in Mode 1. How precisely it is enforced is not yet known: requests
  in flight when the limit is crossed, and any reporting lag, may let
  spending pass it. Until the drill measures this, the residual exposure is
  unknown. It is bounded only by concurrency 1, `max_turns`, the
  runtime-side lifetime and the attempt limit. The Gate 1 evidence must
  state the observed overshoot. If it is not acceptable to the maintainer,
  `provider_cap_attested` is set below the real budget by that margin.

### 6. Fail closed

In any mode above manual, Ilmar does not start new work if:

- the ledger cannot be written;
- `provider_cap_attested`, `daily`, `monthly` or `reserve_per_execution` is
  zero or missing;
- the kill-switch path is unset or inside a workspace;
- a suspension record exists;
- the clock is anomalous (ADR-0028).

### 7. Store loss and restore

A store restored from an older backup can forget charges. The documented
restore procedure therefore creates the kill-switch file before Ilmar
starts. The provider cap bounds anything forgotten.

### 8. Schema

Each issue that changes the store gets its own schema version, in roadmap
order. Each migration runs forward from the version before it, and older
code refuses a newer schema (ADR-0005):

| Schema | Issue | Adds |
| --- | --- | --- |
| 6 | #87 (ADR-0027) | Second instance-scoped journal kind |
| 7 | #13 | `retry_not_before`; the clock high-water mark (ADR-0028) |
| 8 | #18 | `budget_ledger`; `suspension` |
| 9 | #91 | `gate_record` (ADR-0030) |

#90 (limits) and #92 (protected paths) need no schema change. If the order
of the issues changes, the numbers follow the merge order: no two pull
requests claim the same version, and each rebases its migration onto the
version merged before it.

## Consequences

- Good:
  - every execution is bounded in time, turns and attempts, even if the
    coordinator dies;
  - the daily allowance is conservative and crash-proof, because nothing ever
    frees it;
  - the hard cost limit is outside Ilmar, behind one credential.
- Bad:
  - reservations over-count. Fewer executions fit in a day than the real
    cost would allow;
  - one execution can still cost more than its reservation. Only the
    provider cap bounds that in Mode 1. This is the reason for Mode 2's
    gateway;
  - the runtime-side lifetime adds a Podman feature to verify ([R21]);
  - until the Gate 1 drill, how far spending can pass the provider's cap is
    unknown.

## Tests required

**Budget:**

- exhaustion exactly at the limit, during a pass and during an execution;
- `ilmar resume` while still exhausted;
- agent-reported cost below and above the reservation;
- no reported cost at all.

**Crash scenarios** (crash suite):

- at reserve, admit, container start, kill, suspend and resume;
- I-invariants hold;
- no attempt runs without a committed reservation;
- nothing is refunded.

**Runtime limits:**

- the coordinator process killed (`SIGKILL`) during an agent run: with no
  coordinator running, the container stops by its `--timeout` (real Podman,
  integration, required in CI);
- stall;
- `max_turns` reaches the runner.

**Kill switch:** before a claim, before a start and during an agent run.

**Credential path:**

- each forbidden variable, setting or mount refuses the start;
- HTTP 400 at the workspace limit and a spend or usage HTTP 429 suspend
  without a retry;
- an ordinary rate-limit 429 backs off and counts as an attempt;
- an unclassified authentication or billing error suspends.

**Concurrency:** two loops on one store never admit beyond the allowance.

## Revisit when

- The gateway (#94) provides trusted metering. That allows downward
  reconciliation in Mode 2.
- The provider offers daily or per-key spend limits, which could replace the
  local daily allowance as the hard limit.
