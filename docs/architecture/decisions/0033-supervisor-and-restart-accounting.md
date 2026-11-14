# ADR-0033: A systemd supervisor with durable restart accounting

- Status: Proposed
- Date: 2026-11-12; reconciled with the implementation 2026-11-14
- Sources: ADR-0022, ADR-0023, ADR-0028, ADR-0029, ADR-0030
- Number reserved for this decision by the accepted roadmap.
- Asked for by the maintainer, 2026-11-12: conditional approval of the
  accelerated roadmap, amendment 3.
- Built so far: the start log and its limits (`src/ilmar/core/starts.py`),
  the `restart-loop` suspension and its resume rule, and the shipped unit
  (`deploy/systemd/ilmar.service`). Calling it from `ilmar run --loop` is #91
  part 3. Credential delivery to the unit is a separate, approved PR.

## Changes from the first proposal (PR #115)

- **Restart timing is fixed, not growing.** The draft asked for growing
  delays (`RestartSteps`, `RestartMaxDelaySec`) and a small burst. The unit
  instead uses `RestartSec=12min` and `StartLimitBurst=10` per day, chosen so
  that systemd on its own never restarts faster than the start log allows.
  Every restart systemd makes is then one Ilmar accepts. A unit test
  simulates that schedule against the log's rules.
- **The limits are concrete:** one start a minute, six an hour, twenty a UTC
  day (`StartLimits`). The draft left them as N and M.
- **No restart behaviour exists yet.** `ilmar run --loop` is not built; until
  #91 part 3 lands, nothing calls the start log and the unit cannot start a
  loop. The draft read as if both were in place.
- **Credentials.** The unit holds none and reads no plaintext file. Delivery
  through systemd encrypted credentials (systemd 256 or later) was approved
  for its own PR. It never passes a secret through the environment or
  persists plaintext.
- **No dependency on #112.**

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
3. **systemd, plus Ilmar's own append-only start log, written first
   (chosen).**

## Decision

1. **The supervisor.** A systemd user unit is shipped and documented. Ilmar
   never installs or edits it, and agents have no access to it.
   - `Restart=on-failure` with `RestartSec=12min`; `StartLimitIntervalSec=1d`
     and `StartLimitBurst=10`. A change to these keeps the property that
     systemd alone stays within the start log's limits.
   - `RestartPreventExitStatus=3 78`: 3 means disabled or misconfigured, 78
     means suspended. A deliberate stop (exit 0) is never restarted.
   - `NoNewPrivileges=yes`; no credential in the unit or in a file it reads.
2. **Durable start accounting.**
   - **Written first:** the first action of `ilmar run --loop`, before the
     store is opened, ownership is claimed or anything is contacted, appends
     one record (time and boot id) to `<state_dir>/coordinator/starts.log`,
     with `O_APPEND` and `O_NOFOLLOW`. Then the file and its directory are
     fsynced.
   - **It survives every crash after that write.** A crash before it means
     nothing started.
   - **Its own counter:** separate from execution retries (ADR-0028) and
     upgrades (Mode 3).
3. **Limits from the log, before anything else runs.**
   - **The limits:** one start a minute, six per hour, twenty per UTC day.
   - **Clock rule (ADR-0028):** a record dated after "now" counts as inside
     every window and as too recent. A clock that goes backwards only makes
     Ilmar stricter.
   - **Too many starts is a restart loop.** Ilmar writes a `restart-loop`
     suspension when the store is reachable and exits 78 either way.
   - **Resuming:** `ilmar resume` lifts `restart-loop` only when a start now
     would breach no window (checked without writing a record). ADR-0039's
     spending review still applies to every resume.
4. **Fail closed.** An unreadable, unwritable or malformed start log means
   exit 78, with nothing started. The log stays small: after the decision,
   records older than the longest window are dropped by writing a new file,
   fsyncing it, renaming it over the old one and fsyncing the directory.
5. **Loop mode only.** `ilmar run --once` writes no start record and is not
   counted.

## Consequences

- **Two independent bounds:** restarts are bounded even if the unit is
  misconfigured, and even if the supervisor ignores its own limits.
- **No new store schema:** the start log is a plain file, so it works when
  the store is unavailable.
- **Restarts are slow by design:** at most five an hour from systemd. A
  persistent failure stops the unit within a day.

## Tests required

- **Built:** ordering and fsync of the record; the interval, hourly and daily
  limits; a clock that went backwards; corrupt, unreadable and unwritable
  logs; compaction; the resume check; the unit's settings and its restart
  schedule against the log.
- **With #91 part 3:** the record written before the store is opened, with a
  fault injected at every later step; a restart loop writing the suspension
  and exiting 78, also without a store; repeated `ilmar run --loop` starts
  (a supervisor ignoring its limits) suspended by the log.

## Revisit when

- Ilmar runs under a supervisor other than systemd (for example a Podman
  Quadlet or a container orchestrator).
- Mode 3 adds upgrades, which keep their own counter.
