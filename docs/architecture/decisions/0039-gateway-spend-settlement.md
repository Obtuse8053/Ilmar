# ADR-0039: Settle each attempt's model spend from the gateway into the ledger

- Status: Proposed
- Date: 2026-11-14
- Sources: none external · Findings: none (a design decision, not a research finding)
- Amends (by addition; no accepted text is edited):
  - **ADR-0029:** the per-attempt reservation becomes the gateway's budget for
    that attempt; an `extra` charge settles anything beyond it; a new
    suspension reason, `spend-uncertain`.
  - **The `ContainerRuntime` port:** a new read, `model_spend`, and a new
    field, `ContainerSpec.model_budget_micros`.
  - **Persistence:** a new event type, `execution.model_spend`, and two new
    reason codes, `model-spend-settled` and `spend-uncertain` (ADR-0024 §4).
  - **The gateway sidecar (ADR-0037, #111):** its budget comes from each
    sandbox's spec, no longer from deployment-wide settings.
- Asked for by the maintainer on 2026-11-14 (#111, P5 part 2b): per-attempt
  gateway budgets, durable ledger settlement, crash recovery and
  operator-visible spending events.

## Context and problem

Since #124 the gateway meters every model request (admission with a
reservation, then a `CHARGE` line from the provider's own usage report). That
record lived only in the sidecar's log, and the sidecar is removed with the
sandbox. The ledger only knew the attempt's ADR-0029 reservation. Three gaps
followed:

- the gateway's budget was one deployment-wide number, not tied to the
  reservation the coordinator had made for the attempt;
- actual cost above the reservation never reached the ledger;
- a crash between the agent's last request and sandbox removal left no way to
  tell what had been spent.

## Considered options

1. **The coordinator reads the sidecar's log once per attempt, before the
   sandbox is removed, and settles it in one fenced commit.** **Chosen.**
2. *The gateway writes to the store directly.* Rejected: the sidecar runs next
   to agent-written code and must hold no store or tracker credential
   (ADR-0006). It would also be a second writer outside the fence.
3. *A durable spend file on a host volume.* Rejected for now: a new mount and
   persistence format at the sandbox boundary, for little gain while the
   sidecar outlives any coordinator crash until removal (option 1 recovers
   from that). Revisit if sidecars must be removed independently.
4. *Lower the reservation to the actual cost.* Rejected: ADR-0029 keeps
   reservations as the upper bound for supervised accounting; refunds would
   need their own review.

## Decision

### 1. Per-attempt budget

`ContainerSpec.model_budget_micros` carries the attempt's reservation
(`budget.reserve_micros`) to the runtime. It is required and positive for a
`GATEWAY` sandbox and refused for any other network policy. The Podman runtime
passes it to the sidecar as `--budget`. A `GATEWAY` sandbox without a budget
policy is a `PolicyViolationError`: nothing starts.

### 2. The gateway's record

The sidecar logs `ADMIT ticket=<n> reservation=<µ$>` when a request is
admitted and `CHARGE ticket=<n> charged=<µ$> …` when it is settled.
`parse_spend` is strict: a malformed line, a duplicate `ADMIT` or a `CHARGE`
without its `ADMIT` is an error, which counts as uncertain (§4).

`ContainerRuntime.model_spend(handle, owner=…)` returns a `ModelSpend`:
requests, charged, unsettled (admitted but never charged, counted at their
reservation), overrun and an `uncertain` reason. The Podman adapter finds the
sidecar by name and the `io.ilmar.model-gateway` label, checks ownership, and
waits up to `settle_timeout` (30 s) for in-flight requests before reading.

### 3. Settlement

Once per attempt, before the sandbox is removed, the worker records one
commit holding:

- an `execution.model_spend` event (requests, charged, unsettled, overrun and
  `extra` in micro-dollars);
- an `extra` charge of `max(0, charged + unsettled − reservation)`, when
  positive;
- a `spend-uncertain` suspension when the spend is uncertain (§4).

The event is the operator-visible record. A settled attempt is never settled
again.

### 4. Uncertainty fails closed

Spend is uncertain when the gateway did not trust a usage report, a request
was admitted but never settled, the record cannot be read or parsed, or the
sandbox (and its record) is already gone. Uncertain spend suspends all new
work with `spend-uncertain` until the maintainer resumes. Unsettled requests
are charged at their reservation, so the ledger never shows less than may
have been spent.

### 5. Crash recovery

On recovery of an agent run, the worker settles the attempt first, from the
surviving sidecar record if there is one, before any other step. If the
record is gone, the attempt is settled as uncertain and the instance is
suspended.

## Consequences

- The ledger holds a lower bound equal to what the provider reported, and an
  upper bound when it cannot know.
- A crash costs at most a suspension, never an unrecorded charge.
- `tests/architecture/test_side_effects.py` lists `model_spend` as a read.
  That file is a sensitive area.
- Not yet done: an operator view (`ilmar status` or similar) that summarises
  `execution.model_spend` events. The events are already queryable.

## Revisit when

- The sidecar must be removed independently of the sandbox (option 3).
- Provider usage reports carry a signed or otherwise verifiable total.
