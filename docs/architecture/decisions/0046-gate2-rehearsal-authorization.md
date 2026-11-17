# ADR-0046: A Gate 2 rehearsal: an expiring, explicit authorization for supervised restarts

- Status: Proposed
- Date: 2026-11-16, revised 2026-11-17 after review
- Depends on: ADR-0040 (gate records), ADR-0041 (trial authorization),
  ADR-0043 (restart backoff, Proposed), ADR-0044 (gateway configuration,
  Proposed)
- Issues: #96 (Gate 2), #95 (restarts), #111 (gateway)

## Context and problem

Gate 2 (#96) asks for evidence that Ilmar runs **unattended** safely:

- an outside supervisor restarts it after a crash;
- a restart loop suspends it;
- it reaches the model only through the gateway.

But until Gate 2 has passed:

- ADR-0043 refuses `[supervisor] enabled = true` in every mode, so under
  systemd the loop exits 3;
- ADR-0044 allows the gateway only in manual mode.

So the evidence Gate 2 needs cannot be produced before Gate 2. Gate 1 had
the same circularity, and solved it with a trial authorization (ADR-0041).

The permanent requirements must not weaken. A gate record stays the only
authorization for unattended production use. Nothing written for a
rehearsal may ever count as one.

## Considered options

1. **Record Gate 2 first on credential-free evidence alone.** That inverts the
   gate's purpose. Rejected.
2. **A configuration switch for test runs.** A setting is not a person's dated,
   bounded, revocable decision, and it outlives the test. Rejected.
3. **A second kind of trial authorization, the Gate 2 rehearsal (chosen).** It
   is the ADR-0041 mechanism: written by a person, stored apart from gate
   records, bounded, expiring, revocable. It lifts exactly two refusals, and
   only for the trial loop.

## Decision

### 1. What a rehearsal is

It is a trial (ADR-0041) with `kind = "gate2-rehearsal"`. It has every
ADR-0041 term, and these:

- **The repository:** the dedicated disposable repository, by immutable ID,
  never Ilmar's own repository or a fork of it.
- **Duration:** an explicit `--expires-in`, with no default, of at most
  **72 hours**. Expiry needs no one to act: it is checked before every pass
  and at every admission (ADR-0041 §4 and §5).
- **Executions:** `max_executions` of at most 20.
- **Spending:** `max_spend_micros`, no higher than the attested workspace
  cap. The workspace cap stays the independent backstop.
- **Restarts:** `max_restarts`, from 1 to 10. These are starts recorded in
  the start log while the rehearsal is in force. Once they are used up, the
  next start exits 3, and the unit never restarts exit 3.
- **Gate 1 first:** a **Gate 1 gate record** must already exist for the same
  repository ID: unrevoked, its evidence naming this immutable repository
  ID, and passing every check ADR-0040 makes when reading a record. Ilmar
  does not re-verify what ADR-0040 leaves to the person who recorded Gate 1.
  This is checked:
  - when the rehearsal is written: before Gate 1 is recorded,
    `ilmar gate rehearsal` refuses, and nothing else can write one;
  - before every pass and at every admission: if the Gate 1 record is
    missing, revoked or names another repository, the rehearsal is not in
    force.

### 2. Writing one

`ilmar gate rehearsal`, by a person (`--as`) under the instance lock. It
is an **explicit authorization**: every limit is given on the command line,
none has a default, and the command refuses if any is missing:

- `--authorization`: the URL of a maintainer's comment on #96 naming the
  repository ID, the expiry and every limit. The store keeps it with the
  record, and Gate 2's evidence PR cites it;
- `--repository-id`, `--expires-in`, `--max-executions`, `--max-spend-usd`
  and `--max-restarts`, each checked against §1's bounds;
- `--disposable`, as in ADR-0041;
- `--workspace-cap-usd`: the person's attestation of the workspace cap, at
  most `[budget] provider_cap_attested_usd`;
- `--supervisor systemd`: the person states that they installed the unit
  themselves. Ilmar never installs or enables a unit.

**Revocation.** `ilmar gate end-trial` revokes it, like any trial:

- it takes effect at the next check, at the latest at the next admission;
  a pass already admitted finishes under Ilmar's ordinary limits and the
  next start exits 3;
- it is final: a revoked rehearsal is never reinstated. Continuing needs a
  new rehearsal with a new authorization;
- the kill switch and every suspension also stop the loop, as today, and
  do not need the rehearsal to be revoked.

A second trial of any kind is refused while one is in force.

### 3. Exactly what it lifts, and only for `ilmar run --loop --trial`

While a rehearsal is in force, and only then:

- **Supervision:** `[supervisor] enabled = true` loads in `supervised` mode
  only with `[supervisor] rehearsal = true`. Under systemd, the loop runs
  only with `--trial` and a rehearsal in force. Otherwise it exits 3 after
  its start record (ADR-0043 §3), as today.
- **The gateway:** the gateway route (ADR-0044) loads in `supervised` mode
  only with `[supervisor] rehearsal = true`. The trial loop then requires it:
  a rehearsal is never run with the model key inside the sandbox.

Everything else is unchanged:

- every ADR-0041 §4 check before every pass, and §5 at every admission;
- the start log, its backoff and limits, and `restart-loop` (ADR-0033,
  ADR-0043);
- the kill switch, the budget, spend review and suspensions;
- no merges, and no self-repository.

### 4. Never a gate

- A rehearsal is a trial: it lives in the trial table, is never a gate
  record, and never satisfies one.
- The permanent `ilmar run --loop` ignores it.
- **Recording Gate 2** remains `ilmar gate record` by a person, with an
  evidence PR merged and verified at its SHA (ADR-0040). The rehearsal's
  evidence feeds that PR. Nothing is recorded automatically.
- When the rehearsal ends (expiry, revocation, its restart, execution or
  spend limit), the next start exits 3 and nothing restarts it.

### 5. The guarantees, in one place

| Property | How it holds |
| --- | --- |
| Explicitly authorized | Written only by a person, with a maintainer's #96 comment and every limit stated, no defaults (§2). |
| Disposable-only | The disposable repository by immutable ID, never Ilmar's own or a fork; `--disposable` required; the configuration's repository ID must match before every pass (§1, ADR-0041 §4). |
| Expiring | At most 72 hours, checked before every pass and at every admission; no one has to act (§1). |
| Revocable | `ilmar gate end-trial`, effective by the next admission and final (§2). |
| Unavailable before Gate 1 | Refused unless an unrevoked Gate 1 record exists for the same repository ID, checked at writing and before every pass (§1). |

## Consequences

- Good: Gate 2's evidence can be produced on the real path, with real
  restarts through systemd and the real gateway. No permanent rule is
  loosened, and no setting outlives the rehearsal.
- Good: the worst case is bounded by the smallest of these limits:
  - time: 72 hours;
  - restarts: 10;
  - executions: 20;
  - spending: Ilmar's own budget, plus the workspace cap behind it.
- Bad: one more trial kind and one more configuration key, with tests for
  every refusal.
- Bad: the operator edits the unit's `ExecStart` to add `--trial` for the
  rehearsal, and removes it afterwards. The loop refuses to run if it is
  missing, so a forgotten edit fails safe.

## Tests required

- **Store and command:** a rehearsal is written only by a person; it is
  refused without a Gate 1 record, with a revoked one, or with one for
  another repository ID; it is
  refused without `--authorization` or any limit, or with a limit out of
  bounds; it is refused while another trial is in force; it is never
  returned as a gate record; a revoked one is never reinstated.
- **Configuration:** `enabled = true` and the gateway route are refused in
  `supervised` mode without `rehearsal = true`, and refused in every other
  mode.
- **Loop:** under systemd with `rehearsal = true`, it exits 3:
  - without `--trial`;
  - without a rehearsal in force;
  - with a Gate 1 trial instead of a rehearsal;
  - after expiry or revocation;
  - once the Gate 1 record is revoked;
  - with a configuration naming another repository ID;
  - once `max_restarts` starts are recorded under it.

  With all conditions met, it runs, and a crash is restarted under the
  backoff.
- **Permanent loop:** the permanent loop ignores a rehearsal.
- **Soak:** the fake-provider soak (#148) runs under a rehearsal, with
  restarts.

## Revisit when

- Gate 2 is recorded. Then a Mode 2 mode allows supervision permanently, and
  rehearsals are needed only for later gates.
- A gate other than Gate 2 has the same circularity.
