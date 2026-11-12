# Running Ilmar under systemd (ADR-0033)

> Loop mode (`ilmar run --loop`) arrives with #91. Until then this unit is
> shipped for review only; autonomous operation stays disabled.

## Install

Ilmar never installs or edits the unit. As the operator:

```sh
install -m 0644 deploy/systemd/ilmar.service ~/.config/systemd/user/ilmar.service
install -m 0600 /dev/null ~/.config/ilmar/secrets.env   # then add the secret variables
systemctl --user daemon-reload
```

Put the variables named by `token_env` / `api_key_env` in `secrets.env`, never
in the unit or the TOML configuration.

## How restarts are bounded

Restarts are bounded twice, and the two bounds are independent of each other.

**1. systemd's own limits.** These are set in the unit:
- `Restart=on-failure`, with growing delays (`RestartSec`, `RestartSteps` and
  `RestartMaxDelaySec`);
- at most `StartLimitBurst` starts per day (`StartLimitIntervalSec=1d`);
- exit 3 (configuration) and exit 78 (suspended) are never restarted
  (`RestartPreventExitStatus=3 78`), and a clean stop (exit 0) is never
  restarted either.

**2. Ilmar's own start log.** It works even if the unit is changed or
replaced.
- **What it records:** the first thing `ilmar run --loop` does, before
  reading the store or claiming ownership, is to append a record to
  `<state_dir>/coordinator/starts.log` and fsync it.
- **The defaults:**
  - at least 60 seconds between starts;
  - at most 6 starts per hour;
  - at most 20 starts per UTC day.
- **On a breach:** Ilmar writes a `restart-loop` suspension and exits 78.
- **Clock rule:** a record dated in the future counts as recent, so a clock
  that went backwards can only make the limits stricter.
- **A broken log:** if the log is unreadable, unwritable or malformed, Ilmar
  exits 78 and starts nothing. This includes a record cut off by a crash.
  Inspect the file, remove the broken line and start again.
- **Manual runs:** `ilmar run --once` writes no record.

## Resuming after a restart loop

`ilmar resume --as <you>` lifts a `restart-loop` suspension only when a start
now would not breach the limits. If it would, wait for the window to pass.
Resuming writes no start record. Fix the cause first: the service log
(`journalctl --user -u ilmar`) shows why each start failed.
