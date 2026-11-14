# Running Ilmar under systemd (ADR-0033)

> `ilmar run --loop` (#91) runs only with `[autonomy] mode = "supervised"`,
> `[autonomy] repository_id`, and an active gate record written by a person
> with `ilmar gate record` (ADR-0040). No such record may exist before Gate 1
> (#93) passes, so the loop stays refused (exit 3) until then. Credentials
> for the unit come with their own PR; until then, do not enable it.

## Install

Ilmar never installs or edits the unit. As the operator:

```sh
install -m 0644 deploy/systemd/ilmar.service ~/.config/systemd/user/ilmar.service
systemctl --user daemon-reload
```

## Credentials (proposed; not yet implemented)

The unit reads **no plaintext secrets file**: no `EnvironmentFile=`, no
`Environment=` with a secret, and no plaintext `LoadCredential=`. A test
(`tests/unit/test_starts.py`) keeps it that way. Loop mode (#91) needs a way
to receive the forge token and the model credential. The proposal, for the
maintainer's decision:

**systemd encrypted credentials, scoped to the operator's user** (systemd 256
or later).

1. The operator encrypts each secret once, typing it at a prompt so it never
   reaches a shell history or a plaintext file:

   ```sh
   install -d -m 0700 ~/.config/ilmar/credentials
   systemd-ask-password -n "GitHub token: " |
     systemd-creds encrypt --user --name=github-token - \
       ~/.config/ilmar/credentials/github-token.cred
   ```

   The file at rest is ciphertext. It is bound to this user and machine, and
   to the host key or the TPM2 when there is one, so a copy is useless
   elsewhere.
2. The unit names it:
   `LoadCredentialEncrypted=github-token:%h/.config/ilmar/credentials/github-token.cred`.
3. At each start, systemd decrypts it into the service's private
   `$CREDENTIALS_DIRECTORY`, which lives in memory, is readable only by the
   service, and disappears when the service stops.
4. Ilmar would read it from there, by name, instead of from an environment
   variable: a configuration key such as `token_credential = "github-token"`
   next to today's `token_env`. It would never copy the value into its own
   environment, which every child process inherits. The model credential
   still reaches only the gateway sidecar, through the file-free handoff of
   #120.

**Alternatives considered.**

| Option | At rest | Headless restarts | Verdict |
| --- | --- | --- | --- |
| `EnvironmentFile=` or plaintext `LoadCredential=` | plaintext on disk | yes | rejected (#117 review) |
| Encrypted user credentials (above) | ciphertext, bound to user and machine | yes | **proposed** |
| Kernel keyring, entered after each boot | memory only | until reboot | possible stricter mode: a reboot needs the operator |
| Desktop Secret Service (D-Bus keyring) | encrypted | needs an unlocked login session | not headless |
| System credential (TPM2) with a system unit | ciphertext | yes | needs root and a system unit |

**Limits of the proposal.** Anyone who can run code as the operator's user
can decrypt a user-scoped credential, just as they could read the process's
memory. Encryption protects copies, backups and disk access, not a
compromised account. Hosts with systemd older than 256 cannot use user-scoped
encryption.

Implementing step 4 touches credential handling, a sensitive area. It comes
in its own pull request after the maintainer approves this design.

## How restarts are bounded

Restarts are bounded twice, and the two bounds are independent of each other.

**Supervision is off by default** (ADR-0043 §3). Under systemd, `ilmar run
--loop` exits 3 (never restarted) unless `[supervisor] enabled = true`. That
setting is refused until a Mode 2 gate exists, so do not enable the unit yet.

**1. systemd's own limits.** These are set in the unit:
- `Restart=on-failure`, with a delay that grows from `RestartSec=2min` to
  `RestartMaxDelaySec=2h` over `RestartSteps=6` (systemd 254 or later);
- at most `StartLimitBurst=10` starts per day (`StartLimitIntervalSec=1d`);
- exit 3 (configuration) and exit 78 (suspended) are never restarted
  (`RestartPreventExitStatus=3 78`), and a clean stop (exit 0) is never
  restarted either.

Each of systemd's delays is at least the start log's required gap for the
same restart (below), so every restart systemd makes is one Ilmar accepts. A
test simulates the schedule against the log.
- **systemd's growing delay does not shrink after a long healthy run**, so it
  can only be slower than the log, whose chain resets after a cooldown.
- **The exact formula of systemd's growth was not verified** against its
  source. Check it on your host before Gate 2:
  `systemctl --user show -p RestartUSecNext ilmar`. If systemd is ever
  faster, the log suspends: that is safe, but it needs you.
- **With systemd older than 254**, delete `RestartSteps` and
  `RestartMaxDelaySec`, and set `RestartSec=1h`. A shorter fixed delay
  (ADR-0033's 12 minutes) is suspended at the sixth start of a chain.

**2. Ilmar's own start log.** It works even if the unit is changed or
replaced.
- **What it records:** the first thing `ilmar run --loop` does, before
  reading the store or claiming ownership, is to append a record to
  `<state_dir>/coordinator/starts.log` and fsync it.
- **The defaults** (`[supervisor]`, ADR-0043; each may only be made stricter):
  - at least 60 seconds between starts (`min_interval_seconds`);
  - within a chain of restarts, each less than 6 hours after the one before
    (`cooldown_seconds`), a gap that doubles from 60 seconds up to one hour
    (`backoff_max_seconds`);
  - at most 6 starts per hour (`per_hour`);
  - at most 20 starts per UTC day (`per_day`).
- **On a breach:** Ilmar writes a `restart-loop` suspension and exits 78.
- **Clock rule:** a record dated in the future counts as recent, so a clock
  that went backwards can only make the limits stricter.
- **A broken log:** if the log is unreadable, unwritable or malformed, Ilmar
  exits 78 and starts nothing. This includes a record cut off by a crash.
  Inspect the file, remove the broken line and start again.
- **Manual runs:** `ilmar run --once` writes no record.

## Resuming after a restart loop

`ilmar resume --as <you>` lifts a `restart-loop` suspension only when a start
now would not breach the limits, backoff included. If it would, wait for the
window or the backoff to pass.
Resuming writes no start record. Fix the cause first: the service log
(`journalctl --user -u ilmar`) shows why each start failed.
