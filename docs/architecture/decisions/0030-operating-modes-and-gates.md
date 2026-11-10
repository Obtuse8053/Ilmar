# ADR-0030: Operating modes, gate records and the supervised loop

- Status: Accepted
- Date: 2026-11-10
- Sources: none external · Findings: F-013
- Depends on: ADR-0028, ADR-0029, ADR-0031
- Amends (by addition only): ADR-0023 §"Unattended operation stays
  disabled". The default stays disabled. This ADR defines the only way to
  enable a loop.

## Context and problem

ADR-0023 keeps `ilmar run` to single passes (`--once`) and offers no
configuration switch. The autonomy roadmap introduces three modes, each off
until its gate passes:

| Mode | Value | Status after this ADR |
| --- | --- | --- |
| 0 | `manual` | Today's behaviour, the default |
| 1 | `supervised` | Defined here; usable only after Gate 1 (#93) |
| 2 | `autonomous` | Reserved; refused until ADR-0032 and ADR-0033 are accepted |
| 3 | `self-hosting` | Reserved; refused until ADR-0034 is accepted |

The maintainer requires an explicit opt-in. Configuration alone must not be
enough, because a configuration file is easy to change by mistake.

## Considered options

1. *A configuration switch.* Rejected. It is a single point of failure, and
   ADR-0023 rejected it for the same reason.
2. *A CLI flag.* Rejected on its own. A service unit can carry it
   permanently.
3. *Configuration, plus a gate record in the store written by a human
   command, plus a CLI flag.* **Chosen.** Each part is cheap, and they fail
   independently.

## Decision

### 1. Three conditions for a loop

`ilmar run --loop` runs passes until it is stopped, but only if **all** of
these hold:

1. `[autonomy] mode` is `supervised`.
2. The store holds an unrevoked gate record for that mode.
3. The configuration and store pass every fail-closed check of ADR-0029 §6.

Otherwise it exits 3 with the reason, as today. `ilmar run --once` is
unchanged in every mode. Without `--loop`, nothing changes.

### 2. Gate records

- **Writing one.** `ilmar gate record --mode supervised --evidence <url>`
  writes a gate record: the mode, the time and an evidence reference (the
  merged PR that holds the gate evidence).
- **Revoking one.** `ilmar gate revoke --mode supervised` revokes it. The
  revocation is recorded, not deleted.
- **Only a human writes them.** Both are operator commands on the host. The
  store is never mounted into a sandbox (ADR-0011), so no agent can write a
  gate record.
- **No record before Gate 1.** Until #93 passes, there is none. The loop
  therefore stays disabled in code, not only in policy.

### 3. The loop

- **Between passes**, the loop waits `poll_interval_seconds` (default 60) on
  the monotonic clock.
- **Before each pass**, it checks the kill switch and the suspension record
  (ADR-0029).
- **Exit codes:**
  - 0: stopped cleanly by a signal;
  - 3: not enabled;
  - 78: suspended.
- **No restarts.** In Mode 1 the loop never restarts itself. After a crash
  it stays down until the operator starts it (restarts are Mode 2, ADR-0033).
- **One execution at a time** (ADR-0029). Two loops on one store are
  serialised by the existing instance lock and fencing (ADR-0020).

### 4. Lowering a mode is always allowed

Setting a lower `mode`, revoking a gate record or suspending always takes
effect at the next check and never needs a gate. Raising a mode always needs
the higher mode's gate record.

### 5. What Mode 1 may work on

The loop processes approved tasks of the configured repository. Gate 1 and
Mode 1 use a dedicated test repository (the maintainer's decision of
2026-11-10).

Ilmar's own repository is not a target until two things hold:

- the appropriate M4 gate has passed (MANIFEST §10 places self-hosting in
  M4);
- the maintainer has separately approved it.

Configuration validation refuses `self_repository` as the target in Mode 1.
ADR-0031's protected paths and its CI rules apply to every target from the
first run.

## Consequences

- Good:
  - autonomy stays disabled in code until a human records a passed gate;
  - three independent opt-ins;
  - lowering autonomy is always one step.
- Bad:
  - one more table (`gate_record`, schema 9, ADR-0029 §8) and two commands;
  - an operator must remember to revoke a record if the gate evidence is
    later found wrong.

## Tests required

- Each missing condition gives exit 3 with its reason.
- A revoked record disables the loop at the next pass.
- `--once` is unchanged in every mode.
- Two loops on one store: one owner.
- The kill file between passes gives exit 78.
- `autonomous` and `self-hosting` are refused by configuration validation.
- Ilmar's own repository as the target in Mode 1 is refused by configuration
  validation.
- An architecture test: no code path inside the coordinator writes a gate
  record; only the CLI command does.

## Revisit when

- The M4 gate for self-hosting has passed and the maintainer considers
  allowing Ilmar's own repository as a target.
- ADR-0032 to ADR-0034 are proposed for Modes 2 and 3.
