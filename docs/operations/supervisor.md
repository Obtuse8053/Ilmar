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

## Credentials (systemd encrypted credentials)

The unit reads **no plaintext secrets file**: no `EnvironmentFile=`, no
`Environment=` with a secret, and no plaintext `LoadCredential=`. A test
(`tests/unit/test_starts.py`) keeps it that way. Loop mode (#91) needs a way
to receive the forge token and the model credential. The maintainer approved
this design; step 4 is implemented (`[secrets] source`):

**systemd encrypted credentials, scoped to the operator's user** (systemd 256
or later).

1. The operator encrypts each secret once, typing it at a prompt so it never
   reaches a shell history or a plaintext file:

   ```sh
   install -d -m 0700 ~/.config/ilmar/credentials
   systemd-ask-password -n "GitHub token: " |
     systemd-creds encrypt --user --name=ILMAR_GITHUB_TOKEN - \
       ~/.config/ilmar/credentials/ILMAR_GITHUB_TOKEN.cred
   ```

   The file at rest is ciphertext. It is bound to this user and machine, and
   to the host key or the TPM2 when there is one, so a copy is useless
   elsewhere.
2. The unit names it:
   `LoadCredentialEncrypted=ILMAR_GITHUB_TOKEN:%h/.config/ilmar/credentials/ILMAR_GITHUB_TOKEN.cred`.
3. At each start, systemd decrypts it into the service's private
   `$CREDENTIALS_DIRECTORY`, which lives in memory, is readable only by the
   service, and disappears when the service stops.
4. Ilmar reads it from there, by name, instead of from the environment. Set

   ```toml
   [secrets]
   source = "systemd-credentials"
   ```

   Each credential is named exactly like the variable the configuration
   already names (`token_env = "ILMAR_GITHUB_TOKEN"` reads the credential
   `ILMAR_GITHUB_TOKEN`), so step 1's `--name=` and step 2's
   `LoadCredentialEncrypted=<name>:` use that name. Ilmar never copies a value
   into its own environment, which every child process would inherit.
   **It fails closed**, and nothing starts, when:
   - `$CREDENTIALS_DIRECTORY` is unset, relative, not a directory, not yours,
     or writable by others;
   - a credential is missing, a symlink, not a regular file, not yours,
     readable by group or others, larger than 16 KiB, not UTF-8, empty, or
     longer than one line (one trailing newline is allowed).

   There is **no fallback** to the environment. `ilmar config validate
   --check-environment` checks each file the same way. That only works
   inside the unit, where `$CREDENTIALS_DIRECTORY` exists, and it never
   prints a value. Today the model credential is read the same way. Once
   the gateway is wired (Mode 2), it reaches only the gateway sidecar,
   through the file-free hand-over of #120.

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

**Requirements.** Use systemd 256 or later for user-scoped encryption
(`systemd-creds --user`). Older hosts (this repository's CI host and the
development container run 255) cannot encrypt user-scoped credentials. On
those hosts, keep `source = "environment"` and run Ilmar by hand, which is
how Mode 1 runs anyway.

## How restarts are bounded

Restarts are bounded twice, and the two bounds are independent of each other.

**1. systemd's own limits.** These are set in the unit:
- `Restart=on-failure`, after a fixed `RestartSec=12min`;
- at most `StartLimitBurst=10` starts per day (`StartLimitIntervalSec=1d`);
- exit 3 (configuration) and exit 78 (suspended) are never restarted
  (`RestartPreventExitStatus=3 78`), and a clean stop (exit 0) is never
  restarted either.

The timing keeps systemd inside the start log's limits (below), so every
restart systemd makes is one Ilmar accepts:
- Even if every run failed at once, 12 minutes apart gives at most 5 starts
  in any hour; the log allows 6, each at least 60 seconds apart.
- systemd stops after 10 starts in a day; the log allows 20.

The start log then only catches what the unit does not: manual starts, a
changed unit, or a clock that jumped. The delay does not grow, on purpose:
systemd's growing delays do not shrink back after a long healthy run, and a
fixed schedule is one the tests can check. A test simulates this schedule
against the start log.

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
