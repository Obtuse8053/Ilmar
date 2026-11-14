# ADR-0041: A bounded, expiring trial authorization for the Gate 1 live run

- Status: Proposed
- Date: 2026-11-14
- Sources: none external · Findings: none (a design decision, not a research finding)
- Amends (by addition; no accepted text is edited):
  - **ADR-0030 §2:** "no record before Gate 1" stands for gate records. A
    separate, narrower authorization lets the Gate 1 live run itself happen.
  - **ADR-0040:** a second kind of person-written change, kept apart from
    gate records.
  - **ADR-0029 §8:** schema 10 adds `trial_authorization`.
  - **ADR-0024 §4:** event types `instance.trial_authorized` and
    `instance.trial_revoked`; reason codes `trial-authorized` and
    `trial-revoked`.
- Asked for by the maintainer on 2026-11-14 to resolve a circular
  dependency. Gate 1 (#93) requires a supervised `ilmar run --loop` on the
  test repository, but the loop needs a gate record, and no gate record may
  exist before Gate 1.

## Considered options

1. **A separate trial authorization, accepted only by `ilmar run --loop
   --trial`, bounded in executions, time and spending, and expiring on its
   own.** **Chosen.**
2. *A provisional gate record.* Rejected: it would be indistinguishable from
   the permanent record to every check that reads gate records. A mistake
   could then authorize production operation.
3. *Run the Gate 1 live run as repeated `--once` passes.* Rejected as the
   evidence: Gate 1 must show the loop itself (its checks between passes, its
   stops, its kill switch).

## Decision

### 1. What a trial is

`TrialTerms`, every field checked:

- the dedicated disposable repository's immutable ID and name (never Ilmar's
  own repository);
- the exact agent version;
- `max_executions` (1 to 20);
- `max_spend_micros`: reservations and charges made under the trial;
- `starts_at` and `expires_at`, at most 72 hours apart;
- `purpose`: the Gate 1 issue or comment it serves.

### 2. Writing one

`ilmar gate trial` writes it as a person (`--as`), under the instance lock.
It requires:

- `--disposable`: the person's statement that the repository is dedicated and
  disposable, with no credentials in its content, history or workflows;
- `[autonomy] mode = "supervised"` and every Mode 1 rule of the
  configuration;
- the configured `[autonomy] repository_id`;
- a spending cap no higher than the attested provider cap and no lower than
  one reservation.

`ilmar gate end-trial` revokes it, and `ilmar gate show` lists trials with
gate records. The store refuses a trial change from anyone but a person,
refuses a second trial while one is in force, and refuses revoking when none
is unrevoked. A trial is never deleted and never rewritten: triggers allow
only one revocation. An architecture test checks that only `cli_gate.py`
builds a trial change.

### 3. Kept apart from gate records

- A trial lives in its own table and is never returned as a gate record.
- `ilmar run --loop` (permanent) reads only gate records and ignores trials.
- `ilmar run --loop --trial` reads only trials and ignores gate records. A
  gate record never substitutes for an expired or used-up trial.
- So a trial can never authorize production operation: it runs only the
  explicit trial command, on the one repository it names, within its bounds,
  until it expires.

### 4. What `--loop --trial` checks before every pass

Every rule of the permanent loop applies (ADR-0040 §4):

- the configuration digest;
- `supervised` mode;
- the repository ID in the configuration, the authorization and the live
  forge;
- the agent version;
- the kill switch, any suspension and the clock.

Instead of a gate record, it requires all of these:

- **a trial in force:** unrevoked, `starts_at ≤ now < expires_at`. Expiry
  needs no one to act;
- **executions:** fewer than `max_executions` created since `starts_at`;
- **spending:** what was reserved or charged since `starts_at`, plus one more
  reservation, within `max_spend_micros`.

Any failure stops the loop: 3 for the authorization, 78 for a suspension or
the kill switch. A pass already running is bounded by the agent and execution
time limits (ADR-0029), not by the expiry.

## Consequences

- The Gate 1 live run can exercise the real loop without any gate record
  existing.
- The trial's limits are Ilmar's own accounting. The provider's spend cap
  stays the hard backstop.
- A trial is evidence for Gate 1, not Gate 1. Its run, records and the
  operator's verification go into the Gate 1 evidence PR. Only then does a
  person write the permanent record with `ilmar gate record`.

## Tests

- **Store contract:** authorize and revoke; one in force; expiry without a
  revocation; a later trial after expiry; refused from the coordinator; never
  a gate record.
- **Loop:** in force allows a pass without a gate; not authorized, revoked,
  expired, executions used up and spending cap each refuse, even with a gate
  record present. The permanent loop ignores a trial. Every other Mode 1 rule
  still applies.
- **Command:** the execution limit and the spending cap stop a real trial
  loop; expiry, revocation, suspension and the kill switch stop it.
- **CLI:** authorize, show, end; every bound refused; the attestation and
  supervised mode are required.

## Revisit when

- Gate 1 has passed and its record is written. Trials are then only for
  re-running Gate 1 evidence, for example after an agent version change.
