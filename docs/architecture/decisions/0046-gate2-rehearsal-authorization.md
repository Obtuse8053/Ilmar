# ADR-0046: A Gate 2 rehearsal: an expiring, explicit authorization for supervised restarts

- Status: Proposed
- Date: 2026-11-16
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
- **Duration:** at most **72 hours**. Expiry needs no one to act.
- **Executions:** `max_executions` of at most 20.
- **Spending:** `max_spend_micros`, no higher than the attested workspace
  cap. The workspace cap stays the independent backstop.
- **Restarts:** `max_restarts`, from 1 to 10. These are starts recorded in
  the start log while the rehearsal is in force. Once they are used up, the
  next start exits 3, and the unit never restarts exit 3.
- **Gate 1 first:** a **Gate 1 gate record** must already exist for the same
  repository ID. A rehearsal cannot be written before Gate 1.

### 2. Writing one

`ilmar gate rehearsal`, by a person (`--as`) under the instance lock, with:

- `--disposable`, as in ADR-0041;
- `--workspace-cap-usd`: the person's attestation of the workspace cap, at
  most `[budget] provider_cap_attested_usd`;
- `--supervisor systemd`: the person states that they installed the unit
  themselves. Ilmar never installs or enables a unit.

`ilmar gate end-trial` revokes it, like any trial. A second trial of any kind
is refused while one is in force.

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

- **Store:** a rehearsal is written only by a person; it is refused without a
  Gate 1 record for the same repository ID; it is refused while another
  trial is in force; it is never returned as a gate record.
- **Configuration:** `enabled = true` and the gateway route are refused in
  `supervised` mode without `rehearsal = true`, and refused in every other
  mode.
- **Loop:** under systemd with `rehearsal = true`, it exits 3:
  - without `--trial`;
  - without a rehearsal in force;
  - with a Gate 1 trial instead of a rehearsal;
  - after expiry or revocation;
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
