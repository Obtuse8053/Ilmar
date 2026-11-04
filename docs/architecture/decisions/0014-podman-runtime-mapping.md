# ADR-0014: Podman runtime mapping, workspace identity and command timeouts

- Status: Accepted (PR #39, 2026-11-04)
- Date: 2026-11-04
- Sources: [R18], [R21] · Findings: F-020, F-023, F-024
- Implements: ADR-0011 (profile), ADR-0012 (ownership and mount identity) for Podman

## Context and problem

ADR-0011 defines a provider-neutral sandbox profile, and ADR-0012 requires
ownership-checked replacement and a mount gate. The first real runtime (#5)
has to map these onto Podman. That raises questions neither ADR answers:

- Which user namespace mapping? Rootless Podman maps container root to the
  invoking user. A non-root container user then becomes a sub-UID on the host,
  so the coordinator could not read or delete the files the agent writes.
- How does the runtime learn which directory the ledger recorded, so it can
  enforce the mount gate itself?
- What happens to processes a timed-out command started? `podman exec` has no
  timeout or kill operation. Killing the client does not stop the process in
  the container ([R21]).
- How are secrets passed? Values on the `podman` command line are visible to
  other processes on the host.

## Considered options

User namespace:
1. Podman's default rootless mapping, with the sandbox user on a sub-UID. Best
   containment, but workspace files are owned by a sub-UID the coordinator
   cannot manage without `podman unshare` or ownership changes.
2. `--userns=keep-id:uid=1000,gid=1000`. The coordinator's UID becomes the
   non-root sandbox user 1000, so files the agent writes belong to the
   coordinator.
3. Idmapped mounts: a sub-UID sandbox user with the workspace mapped to the
   coordinator. Not reliably available in rootless Podman 4.9 (Ubuntu 24.04).

Timeouts:
1. Kill only the `podman exec` client and leave the container running.
2. Track the command's process group with a shell wrapper. This depends on
   the image's shell and is not reliable for daemonised children.
3. Remove the whole container on timeout.

## Decision

- **User:** option 2. The container runs as non-root UID and GID 1000, which
  maps to the coordinator's host account. Rootless Podman is required, and
  `podman info` is checked once (rootful Podman is refused). Deployment
  guidance: run Ilmar under a dedicated host account that holds nothing
  besides Ilmar's state.
- **Profile mapping:**
  - `--cap-drop=all` and `--security-opt=no-new-privileges`, with the default
    seccomp profile.
  - `--read-only --read-only-tmpfs=false`, plus tmpfs at `/tmp` and the home
    directory (`nosuid,nodev`, size-limited).
  - `--pids-limit`, `--memory` and `--cpus`, from the spec or the adapter's
    defaults.
  - `--init` reaps orphaned processes, so zombies do not use up the process
    limit.
  - `--pull=never`: images are provisioned explicitly, never at run time.
  - `--log-driver=none`.
- **Network `NONE`:** `--network=none --no-hosts`: only loopback, no route,
  and no `host.containers.internal` entry. (`--dns=none` is rejected together
  with `--network=none` by Podman 4.9. The integration tests check that no
  resolver is configured.) `ALLOWLIST` follows with the filtering proxy
  (#40).
- **`UNRESTRICTED` is default-deny (port contract):** a `ContainerSpec` alone
  never grants it. The runtime refuses it with `PolicyViolationError` unless
  the deployment constructed it with `allow_unrestricted_network=True`, an
  explicit trusted or development opt-in. Even then it is logged as a
  warning. An untrusted agent cannot obtain unrestricted egress through a
  spec.
- **Environment:**
  - The container gets only `ContainerSpec.env` plus `HOME`.
  - `--env-host=false` and `--http-proxy=false` are explicit, so neither host
    nor proxy variables are inherited, whatever `containers.conf` says.
  - Values go in a private temporary `--env-file` that is deleted right
    after use, never on the command line.
  - The `podman` process itself runs with a fixed minimal environment
    (`PATH`, `HOME`, the `XDG_*` and `CONTAINERS_*` variables), so no
    coordinator secret is visible to it.
- **Workspace identity:** `ContainerSpec` gains `workspace_identity`, the
  `(device, inode)` the resource ledger recorded. `DirectoryIdentity` moves to
  the domain for this.
  1. Immediately before `podman run`, the adapter calls `authorize_mount`
     (ADR-0012).
  2. After the start, it stats the mount target through `/proc/<pid>/root`.
     It does not follow symlinks and needs no tools in the image.
  3. On any mismatch, or if the check fails, the container is removed and
     the start fails.

  The port contract now requires every runtime to refuse a replaced or
  symlinked workspace.
- **Timeouts (port contract change):** option 3. A command that exceeds its
  timeout is reported with `timed_out=True`, and the whole container is
  removed, so nothing the command started can keep writing to the workspace.
  The `podman` client runs in its own process group, and the whole group is
  killed. Callers treat a timeout as the end of that sandbox, and the
  coordinator starts a new one if it retries.
- **Removal is confirmed, or it fails closed.** Every removal (after a
  timeout, on replacement, on `stop`, after a failed mount check) is followed
  by an `inspect` of the ID. If `podman rm` fails or times out and the
  container is not confirmed gone:
  - a `ContainmentError` is raised (carrying the container ID and name),
    rather than a timeout result that would imply the sandbox is gone;
  - the workspace is **quarantined**: no `start`, under any container name,
    mounts it until a retry confirms the container is gone;
  - the coordinator must record the container in the resource ledger as
    "removal pending" and must not reuse, mount or freeze that workspace until
    reconciliation confirms removal (#12, #15). The in-process quarantine does
    not survive a restart; the ledger does.
- **Identity of operations:** every operation uses the full 64-hex container
  ID. Names are looked up with an anchored `ps` filter and then compared
  exactly. Replacement and removal first check the ownership labels
  (`io.ilmar.owner.*`).
- **Verification:** opt-in integration tests run the contract and the profile
  against real rootless Podman in CI. The checks cover:
  - capabilities, `NoNewPrivs` and seccomp mode;
  - the UID;
  - the read-only root;
  - that no host directory other than the workspace is mounted;
  - the environment;
  - cgroup limits;
  - the network `NONE` structure;
  - a nonce-controlled reachability case.

  The CI job fails if the suite would skip.

## Consequences

- Good:
  - The coordinator can read, freeze and delete agent output with its
    descriptor-bound helpers (ADR-0012).
  - No secret appears on a command line or in the `podman` process
    environment.
  - A timed-out command can never keep modifying a workspace.
  - Mount identity is enforced by the runtime, not left to each caller.
- Bad:
  - With keep-id, a full container escape lands as the coordinator's host
    account rather than as an unprivileged sub-UID. The other layers
    (rootless, no capabilities, `no-new-privileges`, seccomp, read-only root,
    no network) are what make an escape unlikely. A dedicated host account
    limits the damage.
  - A timeout costs the sandbox. Short checks that can time out should run in
    their own container.
  - A failed removal blocks that workspace until removal is confirmed, which
    may need an operator.
  - Development setups that want open networking must opt in explicitly.
  - Images must provide `sleep` (the keep-alive command), and must be
    provisioned before use because nothing is pulled at run time.
  - `podman exec` exit status 125 is ambiguous: it can come from Podman or
    from the command. It is reported as a provider error only when the
    container has stopped.
  - On SELinux-enforcing hosts the bind mount needs relabelling. That is not
    handled yet (Revisit when).

## Revisit when

- Idmapped mounts work in rootless Podman on the supported distributions.
  Then move the sandbox user to a sub-UID (option 3) and record a new ADR.
- `podman exec` gains a reliable timeout or kill of exec sessions.
- Ilmar is deployed on SELinux-enforcing hosts (Fedora, RHEL). Then add
  relabelling with `relabel=private`, with tests.
- The adapter registry (#10) exposes `SandboxProfile` settings in
  configuration.
