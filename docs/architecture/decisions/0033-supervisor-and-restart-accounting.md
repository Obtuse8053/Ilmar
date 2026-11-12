# ADR-0033: A systemd supervisor with durable restart accounting

- Status: Proposed
- Date: 2026-11-12
- Sources: ADR-0022, ADR-0023, ADR-0028, ADR-0029, ADR-0030
- Number reserved for this decision by the accepted roadmap.
- Asked for by the maintainer, 2026-11-12: conditional approval of the
  accelerated roadmap, amendment 3.

## Context and problem

Mode 2 restarts Ilmar after a crash without a human, but restarts must stay
bounded.
- **Ilmar never restarts itself** (roadmap, ADR-0030).
- **Ownership claims are not a reliable restart counter.** A process can
  crash before it claims, or fail to reach the store at all, and claims also
  happen for reasons other than restarts.
- **What is needed:** durable accounting of every start attempt, written
  before anything else, that survives crashes.

## Considered options

1. **systemd alone.** Its start limits are real, but they live in a
   supervisor Ilmar does not control. A misconfigured or replaced unit would
   bypass them.
2. **Counting ownership claims.** Rejected by the maintainer, for the reasons
   above.
3. **systemd, plus Ilmar's own append-only start log, written first (chosen).**

## Decision

1. **The supervisor.** A systemd user unit is shipped and documented; Ilmar
   never installs or edits it, and agents have no access to it.
   - **Restart policy:** `Restart=on-failure`, `RestartSec` with
     `RestartSteps` and `RestartMaxDelaySec` for growing delays, and
     `StartLimitIntervalSec=1d` with a small `StartLimitBurst`.
   - **Exit codes never restarted:** `RestartPreventExitStatus=3 78`. 3 means
     disabled or misconfigured; 78 means suspended.
   - **A deliberate stop (exit 0)** is never restarted.
2. **Durable start accounting.**
   - **Written first:** the very first action of `ilmar run --loop`, before
     reading the store, claiming ownership or contacting anything, appends one
     record to `<state_dir>/coordinator/starts.log`. The record holds the time
     and the boot id. It is written with `O_APPEND`, then the file and the
     directory are fsynced.
   - **It survives every crash after that write.** A crash before it means
     nothing started.
   - **Its own counter:** it is separate from execution retries (ADR-0028)
     and from upgrades (Mode 3).
3. **Limits are checked from the log, before anything else runs.**
   - **The limits:** a minimum interval between starts, at most N starts per
     hour and at most M per UTC day (configuration, with conservative
     defaults).
   - **Clock rule (ADR-0028):** a record dated after "now" counts as inside
     every window. A clock that goes backwards can only make Ilmar stricter.
   - **Too many starts is a restart loop.** Ilmar writes a `restart-loop`
     suspension when the store is reachable, and exits 78 either way, which
     systemd never restarts.
   - **Resuming:** `ilmar resume` lifts a `restart-loop` suspension only when
     the log shows no breach in the current windows.
4. **Fail closed.** An unreadable, unwritable or malformed start log means
   exit 78, with nothing started.
   - **Size:** the log is kept small by rewriting only records older than the
     longest window, atomically (write a new file, fsync, rename).
5. **Loop mode only.** Manual `ilmar run --once` writes no start record and
   is not counted.

## Consequences

- **Two independent bounds:** restarts are bounded even if the unit is
  misconfigured, and even if the supervisor ignores its own limits.
- **No new store schema:** the start log is a plain file, so it works when
  the store is unavailable.

## Tests required

- **Ordering:** the start record is written and fsynced before the store is
  opened; a fault injected at every later step leaves the record.
- **Limits:**
  - starts faster than the minimum interval;
  - the hourly and daily limits;
  - a clock that went backwards;
  - a corrupt, unreadable or unwritable log.
- **Suspension:** a restart loop writes the suspension and exits 78, and
  exits 78 without a store too.
- **The unit file:** a check of its settings (the never-restarted exit codes,
  the start limits), plus restart after exit 78 being refused.
- **A supervisor that ignores its limits:** simulated by starting
  `ilmar run --loop` repeatedly; the start log suspends it.

## Revisit when

- Ilmar runs under a supervisor other than systemd (for example a Podman
  Quadlet or a container orchestrator).
- Mode 3 adds upgrades, which keep their own counter.
