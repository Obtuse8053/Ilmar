# ADR-0028: A persisted time may only delay new work

- Status: Accepted
- Date: 2026-11-10
- Sources: none external · Findings: F-002
- Amends (by addition only; no accepted text is edited):
  - ADR-0022 §Time: a closed list of decisions that may compare a persisted
    time with the current time.

## Context and problem

ADR-0022 §Time says that nothing in Ilmar compares a persisted timestamp
with the current time to make a decision. That rule is why a clock step back
(NTP, a restored snapshot) is harmless: timestamps stand still and nothing
expires.

The autonomy roadmap (`docs/autonomy-roadmap.md`) needs three decisions that
depend on the wall clock:

1. **Retry backoff** (#13, F-002): a task in RETRY_WAIT must not be retried
   before a durable `retry_not_before`. A restart must not shorten the wait.
2. **Budget periods** (#18, ADR-0029): a daily and a monthly accounting
   allowance need day and month boundaries.
3. **Restart pacing** (#95, Mode 2): a minimum interval and backoff between
   process starts, which must survive host restarts.

Each needs a persisted time, compared with the current time. The question is
how to allow that without making any safety decision depend on the clock.

## Considered options

1. *Keep the ban; count passes instead of time.* Rejected. A pass can take
   seconds or an hour, so "wait three passes" is not a backoff. Budget days
   cannot be expressed in passes.
2. *Measure only in-process with the monotonic clock.* Rejected. Nothing is
   persisted, so a restart resets the backoff and the budget day. That is the
   failure that durable scheduling exists to prevent.
3. *Allow wall-clock comparisons in general.* Rejected. Lease expiry, owner
   liveness or recovery by timeout would make ownership and fencing depend on
   a clock that can step.
4. *A closed list of uses that can only delay or refuse the start of new
   work, with restrictive handling of clock anomalies.* **Chosen.**

## Decision

### 1. The closed list

A persisted time may be compared with the current time **only** for:

| Use | Decision it may take | Introduced by |
| --- | --- | --- |
| A task's `retry_not_before` | Whether a new attempt may be claimed now | #13 |
| Budget day and month (UTC) | Whether a new execution may be admitted under the period's allowance | ADR-0029 |
| Restart pacing at process start | How long a fresh process waits before its first pass | ADR-0033 (Mode 2, to come) |

Adding a use needs a new ADR.

### 2. Never by the clock

These decisions stay clock-free, exactly as ADR-0022 decided:

- ownership, leases, fencing and the instance lock;
- recovery of journaled operations, and any judgement that an owner or an
  execution is dead;
- quarantine and its release (#59 adds only human commands);
- cancellation;
- verification and publication;
- refunding, releasing or reducing a budget charge.

### 3. Direction: a wrong clock can only delay or refuse

Every use on the list admits new work only when a persisted time lies in the
past. So a clock that runs late can only make Ilmar wait longer, and two more
rules cover the other cases:

- **High-water mark.** The store keeps the latest wall-clock time any
  admission decision has seen, updated in the same transaction as that
  decision. If the current time is earlier than the mark, Ilmar admits no
  new work until the clock catches up. This covers NTP steps back and a
  restored snapshot of the host. Ownership, recovery and timestamps work as
  they do today (`NonDecreasingClock`).
- **Forward jumps.** While a process runs, it compares the wall clock's
  progress with the monotonic clock's. If they differ by more than
  `clock_jump_tolerance_seconds` (default 300), Ilmar suspends with reason
  `clock-anomaly` (ADR-0029) and a human resumes it. Without this, a forward
  jump could roll a budget day over early or end a backoff.

A forward jump while no Ilmar process is running cannot be detected this
way. It can end a backoff early or open a new budget day early. That residual
risk is bounded by the provider's monthly spending cap (ADR-0029) and by the
attempt limit, which does not depend on time.

### 4. Boundaries are UTC

Budget days and months are computed in UTC from the wall clock. There is no
time-zone setting: UTC has no daylight-saving steps, and the operator's local
day is not a safety property.

### 5. In-process deadlines are unchanged

Agent and command timeouts, the stall timeout (#13) and the wall-clock kill
(ADR-0029) are measured with `time.monotonic` inside the process and never
persisted, as ADR-0022 decided.

## Consequences

- Good:
  - #13, #18 and later #95 can persist times without making any safety
    decision depend on the clock;
  - every clock fault falls on the safe side, except an undetectable forward
    jump while Ilmar is down, which the provider cap bounds.
- Bad:
  - a new table column (the high-water mark) and a suspension reason;
  - a clock stepped far back stalls new work until it catches up. That is
    deliberate and visible as an event.

## Tests required

- Fake clocks: a backward step stops admissions until the mark is reached;
  ownership, recovery and timestamps behave as before.
- A wall-clock jump within one process beyond the tolerance suspends.
- Backoff and budget days survive a restart, and a restart cannot shorten
  them.
- An architecture test: only the modules that implement the listed uses read
  a persisted time and compare it with the clock. `core/tracker.py` and
  recovery still read none.

## Revisit when

- A use outside the list is proposed.
- A trusted time source (for example the gateway's or the provider's
  timestamps) becomes available, which could replace the high-water mark.
