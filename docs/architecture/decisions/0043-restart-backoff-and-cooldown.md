# ADR-0043: Exponential restart backoff, a cooldown and configurable, stricter-only limits

- Status: Proposed
- Date: 2026-11-14
- Sources: [R31], [R32]; ADR-0028, ADR-0030, ADR-0033 · Findings: none new
- Amends ADR-0033 (Proposed) by addition: §1's fixed `RestartSec` and §3's
  fixed limits. No accepted text is edited.
- Asked for by the maintainer, 2026-11-14 (#95): "configurable restart
  limits, exponential backoff, cooldown and durable restart accounting …
  disabled by default".

## Context and problem

ADR-0033 bounds restarts twice: by systemd's settings, and by Ilmar's durable
start log. A breach of the log's limits is a restart loop. Ilmar writes the
`restart-loop` suspension and exits 78. ADR-0033 chose a **fixed** delay
(`RestartSec=12min`) and fixed limits (one start a minute, six an hour,
twenty a UTC day).

A fixed delay has two costs:

- **It is slow for a one-off crash.** The first restart after a transient
  failure waits 12 minutes.
- **It never slows a persistent failure.** A failure that repeats keeps
  restarting at the same pace until the daily limit stops it.

The maintainer has asked for exponential backoff, a cooldown and
configurable limits. ADR-0033 also leaves supervision effectively on
wherever the unit is installed. The maintainer requires it to be **disabled
by default**.

## Considered options

1. **Keep ADR-0033's fixed delay.** Simple and proven by a simulation test,
   but it has both costs above.
2. **Backoff in systemd only** (`RestartSteps=`, `RestartMaxDelaySec=`,
   systemd 254 or later). The schedule lives outside Ilmar, and nothing
   durable would notice a supervisor ignoring it.
3. **Backoff enforced by Ilmar's start log, with systemd configured to be at
   least as slow (chosen).** It keeps ADR-0033's two independent bounds. A
   supervisor faster than the log is caught and suspended. It never goes
   unnoticed.
4. **An Ilmar-written supervisor process.** It would duplicate systemd's
   process management, and a supervisor of the supervisor would still be
   needed. Rejected.

## Decision

### 1. Backoff and cooldown, from the start log alone

- **A chain.** Walking back from this start, the chain is the run of
  earlier starts, each less than `cooldown_seconds` after the one before it.
  A start more than `cooldown_seconds` after the previous one begins a new
  chain. A run that stayed healthy for longer than the cooldown therefore
  resets the backoff. No "healthy" marker is written; time alone decides.
- **The required gap.** Let `n` be the number of earlier starts in the chain.
  If `n ≥ 1`, this start must come at least
  `min(min_interval_seconds × 2^(n−1), backoff_max_seconds)` after the
  previous start. The first restart therefore waits at least
  `min_interval_seconds`, the next twice that, and so on, up to
  `backoff_max_seconds`.
- **A start too early is a restart loop,** like every other breach: the
  `restart-loop` suspension, exit 78. The hourly and daily limits of
  ADR-0033 still apply on top.
- **Clock rule (ADR-0028), unchanged:** a record dated after "now" is inside
  every window. A clock that goes backwards only makes Ilmar stricter.
- **`ilmar resume`** lifts `restart-loop` only when a start now would breach
  nothing, backoff included.

### 2. Configurable, but only stricter

A `[supervisor]` table holds the limits. The defaults are the floor, and a
value may only tighten them:

| Key | Default | Allowed |
| --- | --- | --- |
| `enabled` | `false` | `true` only in a mode whose gate allows supervisor restarts |
| `min_interval_seconds` | 60 | ≥ 60 |
| `backoff_max_seconds` | 3600 | ≥ `min_interval_seconds`, ≤ 86400 |
| `cooldown_seconds` | 21600 (6 h) | ≥ `backoff_max_seconds` |
| `per_hour` | 6 | 1 to 6 |
| `per_day` | 20 | 1 to 20 |

A value outside its range is a configuration error (exit 3). Loosening a
default needs a new ADR, not a setting.

### 3. Disabled by default

- **Under systemd, the loop refuses to run while supervision is off.** If
  `ilmar run --loop` starts under systemd (it has an `INVOCATION_ID`) and
  `[supervisor] enabled` is not `true`, it exits 3 after writing its start
  record. The shipped unit never restarts exit 3.
- **Supervision cannot be enabled before its gate.** Supervisor restarts
  belong to Mode 2 (roadmap, ADR-0030). Until a Mode 2 mode exists and its
  gate (#96) passes, `enabled = true` is a configuration error.
- **Without systemd, nothing changes:** a person starts the loop, and a
  crash is never restarted.

### 4. The shipped unit stays slower than the log

- **Backoff where systemd supports it.** The unit uses `RestartSec=2min`,
  `RestartSteps=6` and `RestartMaxDelaySec=2h` (systemd 254 or later). Each
  of its delays is at least the log's required gap for the same restart,
  and an exit-to-start delay is never longer than the start-to-start gap
  the log measures. A unit test simulates the schedule against the log.
- **Older systemd** (no `RestartSteps=`) needs a fixed `RestartSec=1h`, the
  backoff cap. ADR-0033's 12 minutes is *not* slower than the log: the sixth
  start of a chain needs 16 minutes, so it would be suspended. That is safe,
  but it is not a schedule to rely on. `StartLimitBurst=10` per day is
  unchanged.
- **Exit 2 is never restarted either** (maintainer decision, 2026-11-16):
  `RestartPreventExitStatus=2 3 78`. Exit 2 means an execution is blocked
  and needs a person; a restart would only meet the same block. Only exit 1
  (a failed pass) and a crash (a signal) are restarted.
- **If the two disagree, the log wins.** A supervisor restarting faster
  than the log allows is suspended. It is never obeyed.
- **What is and is not verified.** systemd 254 added `RestartSteps=` and
  `RestartMaxDelaySec=` for exponentially growing restart delays [R31].
  Its delay grows with the number of automatic restarts and does not
  shrink after healthy running [R32]. Ilmar's chain resets after the
  cooldown, so systemd's delay can only be longer. The unit test
  assumes systemd interpolates geometrically from `RestartSec=` to
  `RestartMaxDelaySec=` over `RestartSteps=`. That exact formula has not
  been checked against systemd's source in this decision. Before Gate 2,
  observe it on the operator's host (`systemctl --user show -p
  RestartUSecNext ilmar`). If it is faster, the log suspends: the failure is
  safe, but needs a person.

### 5. Supervisor failure

- **A dead or missing supervisor** restarts nothing: the safe direction.
- **An unusable start log** stops everything, as before: exit 78, nothing
  started.
- **A supervisor ignoring its own limits** is caught by the log (§1), as
  before.

## Consequences

- Good: a one-off crash restarts after about 2 minutes, not 12, while a
  persistent failure slows down to one restart every hour or two and stops
  within the day.
- Good: the limits can be tightened per deployment without code, and they
  can never be loosened by a setting.
- Good: supervision is off unless a person turns it on, and it cannot be
  turned on before Gate 2.
- Bad: a person restarting the unit by hand during a failing chain may be
  suspended, because systemd restarts its own counter and the log does not.
  This fails safe: `ilmar resume` after the cooldown.
- Bad: `INVOCATION_ID` detects systemd only. Another supervisor is not
  detected, so its operator must configure it to be at least as slow. The
  log still bounds it.

## Tests required

- **Backoff:** chains broken by the cooldown; each doubling; the cap; a
  start just early (breach) and just in time (accepted); future-dated
  records.
- **Configuration:** defaults, each stricter value accepted, each looser
  value refused, `enabled = true` refused in every current mode.
- **Disabled by default:** the loop under systemd with supervision off exits
  3, and its start is still recorded.
- **The unit:** its schedule simulated against the log, for every chain
  length up to the daily limit.
- **Crash, restart loop and supervisor failure:** restarts that crash at
  once, a supervisor ignoring its delays, and an unusable log.

## Revisit when

- A Mode 2 mode and its gate exist. That is when `enabled = true` may become
  valid.
- Ilmar runs under a supervisor other than systemd.
