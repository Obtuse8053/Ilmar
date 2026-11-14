# ADR-0039: Settle each attempt's model spend from the gateway into the ledger

- Status: Proposed
- Date: 2026-11-14 (revised after the #127 review the same day)
- Sources: none external · Findings: none (a design decision, not a research finding)
- Builds on the sidecar model gateway. Its design record, ADR-0037, is
  proposed in PR #115 and is **not yet on `main`**; the gateway as built is
  in `src/ilmar/adapters/podman/model_gateway.py` and `gateway_admission.py`
  (merged in #118–#124). Until #115 lands, "ADR-0037" in this repository
  refers to that proposal.
- Amends (by addition; no accepted text is edited):
  - **ADR-0029:** the per-attempt reservation becomes the gateway's budget for
    that attempt; an `extra` charge settles reported cost beyond it; a new
    suspension reason, `spend-uncertain`, which only an operator's spend
    review lifts.
  - **The `ContainerRuntime` port:** a new mutating operation,
    `close_model_gateway`, a new read, `model_spend`, and a new field,
    `ContainerSpec.model_budget_micros`.
  - **Persistence:** a new journal kind, `close-gateway` (repeatable); two new
    event types, `execution.model_spend` and `execution.spend_reviewed`; three
    new reason codes, `model-spend-settled`, `spend-uncertain` and
    `spend-reviewed` (ADR-0024 §4).
  - **The gateway sidecar:** its budget comes from each sandbox's spec, no
    longer from deployment-wide settings; its log gains `READY`, `ADMIT` and
    `CLOSED` records.
- Asked for by the maintainer on 2026-11-14 (#111, P5 part 2b): per-attempt
  gateway budgets, durable ledger settlement, crash recovery and
  operator-visible spending events.

## Context and problem

Since #124 the gateway meters every model request: admission with a
reservation, then a `CHARGE` line from the provider's own usage report. That
record lived only in the sidecar's log, and the sidecar is removed with the
sandbox. The ledger only knew the attempt's ADR-0029 reservation. Three gaps
followed:

- the gateway's budget was one deployment-wide number, not tied to the
  reservation the coordinator had made for the attempt;
- cost the provider reported above the reservation never reached the ledger;
- a crash between the agent's last request and sandbox removal left no way to
  tell what had been spent.

**What the figures are, and what they are not.** The gateway's reservation is
an estimate from the request's size and `max_tokens`, and its charge is what
the provider's usage report says, at the configured prices. Neither is a
guaranteed bound on what the provider bills: prices can be wrong or change,
reports can be wrong, and a request can be billed without a usable report.
The provider's own spending cap, set by the operator on the provider's
account, is the external hard backstop (ADR-0029). The ledger records the best
figure Ilmar has, and says when it does not know.

## Considered options

1. **The coordinator quiesces the sandbox, reads the gateway's log, and
   settles it in one fenced commit before the sandbox is removed.**
   **Chosen.**
2. *The gateway writes to the store directly.* Rejected: the sidecar runs next
   to agent-written code and must hold no store or tracker credential
   (ADR-0006). It would also be a second writer outside the fence.
3. *A durable spend file on a host volume.* Rejected for now: a new mount and
   persistence format at the sandbox boundary. Option 1 recovers from a
   coordinator crash because the sidecar survives until removal. Revisit if
   sidecars must be removed independently.
4. *Read the log without quiescing.* Rejected (#127 review): the agent may
   still be running after a coordinator crash, or may have left a background
   process, and could make another billable request after the log is read.
5. *Lower the reservation to the reported cost.* Rejected: refunds would need
   their own review.

## Decision

### 1. Per-attempt budget

`ContainerSpec.model_budget_micros` carries the attempt's reservation
(`budget.reserve_micros`) to the runtime. It is required and positive for a
`GATEWAY` sandbox and refused for any other network policy. The Podman runtime
passes it to the sidecar as `--budget`. A `GATEWAY` sandbox without a budget
policy is a `PolicyViolationError`: nothing starts.

### 2. The gateway's record

The sidecar logs, in order:

- `ilmar-gateway READY` once, when it starts serving;
- `ADMIT ticket=<n> reservation=<µ$>` before a metered request may reach the
  provider;
- `CHARGE ticket=<n> charged=<µ$> …` when the request is settled;
- `CLOSED admitted=<n> settled=<n> charged=<µ$> open=<n>` once, when it is
  closed (§3).

Admission and closing exclude each other, so the `CLOSED` totals are exactly
what the log holds before it. `parse_spend` does not trust a record when a
line does not parse, `READY` appears twice (the gateway restarted and lost its
state) or after an `ADMIT`, a ticket is admitted twice or charged without its
admission, `CLOSED` appears twice, an `ADMIT` follows it, or its totals differ
from the lines before it (lines are missing). An empty or cut-off log has no
`CLOSED` line and is never taken as the whole cost.

### 3. Quiescence before reading

`ContainerRuntime.close_model_gateway(name, owner=…)` (mutating, journaled as
`close-gateway`, repeatable):

1. kills the sandbox's own container (it is kept, not removed) and confirms
   it stopped within a bound; otherwise `ContainmentError`;
2. signals the gateway (`SIGUSR1`) to admit nothing more;
3. the gateway waits up to `--drain-seconds` (20 s) for requests in flight,
   then writes `CLOSED`.

`ContainerRuntime.model_spend(name, owner=…)` (a read) then requires the
agent's container to be stopped, the gateway to be owned by `owner` and
labelled `io.ilmar.model-gateway`, and a complete, consistent record. It waits
up to `settle_timeout` (30 s, longer than the drain) for `CLOSED`. Anything
else is a `ProviderError`. Both work from durable data (the sandbox name and
the owner in the ledger), so a restarted coordinator can use them.

### 4. Settlement on every removal path

Every removal of an attempt's agent sandbox goes through one worker method,
which settles first: the normal end of a run, each recovery branch, and the
sweep of live sandboxes at the end of an execution. Settlement happens once
per attempt and is one commit holding:

- an `execution.model_spend` event (requests, charged, unsettled, overrun and
  `extra`, in micro-dollars): the operator-visible record;
- an `extra` charge of `max(0, charged + unsettled − reservation)`, when
  positive;
- a `spend-uncertain` suspension when the spend is uncertain (§5), unless
  another suspension is already in force (§6 still holds it).

A sandbox whose agent run was never begun settles at zero without being read:
the gateway accepts only the per-attempt throwaway token, and that token is
handed to the sandbox only by the agent-run step (ADR-0037 §1, as proposed).

### 5. Uncertainty fails closed

Spend is uncertain when the stop cannot be confirmed, the record is
incomplete, inconsistent, unreadable or already gone, the gateway did not
trust a usage report, or a request was in flight at close or never settled.
Unsettled requests are charged at their reservation, the best estimate
available (not a bound; see Context). Any other, unexpected error during
settlement is recorded as uncertain too, and then raised: the execution stops
at the worker's process boundary.

### 6. Only an operator's spend review lifts uncertainty

Each uncertain `execution.model_spend` event stays *unreviewed* until an
`execution.spend_reviewed` event (a person, naming that event's sequence
number) answers it. While any is unreviewed, `ilmar resume` refuses to lift
*every* suspension, whatever its reason, and lists the attempts. So another
suspension in force at the time cannot hide the uncertainty.
`ilmar resume --provider-spend-reviewed` records the operator's confirmation
that they checked the provider's own usage and billing for those attempts. It
writes the review events in the same commit as the resume. `ilmar budget`
lists unreviewed attempts.

### 7. Crash recovery

On recovery of an agent run whose agent ran, the worker settles first: it
quiesces the sandbox (stopping an agent that may still be running and
spending), then reads and records, and only then abandons or completes the
run and removes the sandbox. If the sandbox, and the record with it, is gone,
the attempt is settled as uncertain.

## Consequences

- The ledger holds what the gateway's own complete record says, or says it
  does not know. A crash costs at most a suspension and a spend review, never
  an unrecorded settlement.
- The provider's spending cap stays the hard limit. This decision does not
  make it optional.
- `tests/architecture/test_side_effects.py` lists `model_spend` as a read and
  `close_model_gateway` as a mutation. That file is a sensitive area.
- Not yet done: an operator summary of `execution.model_spend` events beyond
  `ilmar budget`'s list of unreviewed attempts.

## Revisit when

- The sidecar must be removed independently of the sandbox (option 3).
- Provider usage reports carry a signed or otherwise verifiable total.
- ADR-0037 is accepted with a different token hand-over (§4's zero settlement
  rests on it).
