# ADR-0036: Seal the agent's own settings at the sandbox boundary

- Status: Proposed
- Date: 2026-11-11
- Sources: [R14], [R21], [R29] · Findings: F-019
- Amends (by addition; no accepted text is edited):
  - **ADR-0029 §5:** how the single credential path holds *during* a run, not
    only at the start.
  - **ADR-0014:** a new sandbox property, sealed paths.
- Asked for by the maintainer in the review of #109, 2026-11-11.

## Context and problem

ADR-0029 §5 allows the agent one route to a model: the configured key, whose
Anthropic workspace carries the spend limit. #109 adds a probe that checks the
sandbox before Claude Code starts. Review found two gaps:

1. **The probe was not strictly fail-closed.** A missing `awk` or `grep`, an
   unreadable settings file, or a scan that stopped part-way could still
   report "clean".
2. **The check is only a snapshot.** Claude Code reads settings, including
   `apiKeyHelper` and `env`, from the project's `.claude/settings.json` and
   `.claude/settings.local.json` and from `~/.claude/settings.json` and
   `~/.claude/settings.local.json` [R29], and, as reported by the maintainer,
   it can reload them while it runs.
   The agent can write all four. A run that passed the check could therefore
   add a helper or a provider variable halfway through.

## Considered options

1. **Seal the settings locations at the sandbox boundary (chosen).** The
   runtime mounts an empty tmpfs over each location. The agent's processes run
   as an unprivileged user without capabilities, so they cannot unmount it,
   cannot remove or rename a mount point, and cannot write into a read-only
   one.
   - This holds whatever Claude Code does: it needs no knowledge of when or
     whether Claude Code reloads settings.
2. **Claude Code flags** that limit which settings sources load.
   - This depends on the behaviour of each Claude Code version.
   - It cannot be verified from the development environment, where the
     package registry refuses the Claude Code package [R29 access log].
   - It leaves the files writable.
   - Rejected as the primary control.
3. **Watch the files and stop the run when one changes.**
   - This only detects the change after it has happened: a helper could run
     before the watcher reacts.
   - Rejected.
4. **A credential-injecting egress proxy.** The proxy terminates TLS, removes
   any credential the agent sends, and adds the configured key, so the key
   never enters the sandbox at all.
   - This is the strongest boundary, and it matches ADR-0006's aim of keeping
     credentials out of the sandbox.
   - It needs a sandbox-trusted CA and TLS termination: a new security
     component and its own ADR.
   - Recorded as the follow-up.

## Decision

1. **Sealed paths.** `ContainerSpec.sealed_paths` lists directories the
   runtime mounts as empty tmpfs.
   - **Path forms:** a path is `~/…` (under the sandbox user's home), relative
     (under the working directory) or absolute.
   - **Access:** read-only unless marked writable.
   - **Containment:** a mount point cannot be removed or renamed from inside,
     and nothing the agent does can lift a seal.
   - **The host is untouched:** whatever is at the path on the host stays
     there unchanged, hidden from the sandbox.
   - **Refusal:** a runtime that cannot seal a path refuses to start the
     container. Podman uses `--tmpfs <path>:ro,nosuid,nodev,noexec`, or `rw`
     for writable paths.
   - **Paths the sandbox needs:** `/`, `/tmp`, the working directory and the
     home directory are never sealed.
2. **What the Claude Code sandbox seals** (`SEALED_PATHS`), always and with no
   configuration option to remove it:

   | Path | Access | Why |
   | --- | --- | --- |
   | `.claude` (project) | read-only, empty | project and local settings, hooks, `apiKeyHelper` |
   | `~/.claude` | writable | Claude Code keeps state here; as a mount point it cannot be renamed away to plant a new settings file |
   | `~/.claude/settings.json` | read-only, empty | user settings |
   | `~/.claude/settings.local.json` | read-only, empty | user local settings |

   The sealed project directory hides the repository's own `.claude/` from the
   agent. The agent can neither read nor change it, so a candidate can never
   contain changes to it.
3. **The probe checks the boundary and fails closed.** Before Claude Code
   starts, the probe runs with Claude Code's exact environment and reports the
   mount table.
   - **Refused (`policy-violation`, never retried):**
     - a sealed path that is not mounted with the right access;
     - a variable the runner passed that is missing from the scan;
     - `CLAUDE_CONFIG_DIR`, which would move user settings elsewhere;
     - any route ADR-0029 §5 already forbids.
   - **Never lets Claude Code start (`ProviderError`):** any step that does
     not finish. That covers a tool missing, a file that cannot be read, a
     missing completion marker or an explicit error line.

## Consequences

- **What it guarantees:** after the check, no settings file Claude Code reads
  from these locations can change. The guarantee rests on the kernel's mount
  rules, not on Claude Code's behaviour.
- **What stays open:**
  - **The global state file.** `~/.claude.json`, which holds the OAuth
    session, MCP configuration and state [R29], must stay writable for Claude
    Code's state. The probe refuses it at the start, and `--strict-mcp-config`
    already ignores its MCP servers.
  - **Other credentials.** An agent could still use a credential that is
    already in the sandbox, for example one committed to the repository, by
    calling the API directly. The egress allowlist limits it to the model
    host, and the spend limit applies only to the configured workspace.
  - **The proxy.** Option 4 closes both of these. It is the recommended
    follow-up ADR.
- **What the agent loses:** it cannot see or edit a repository's `.claude/`
  directory.

## Tests required

- **Probe, real shell:**
  - a clean scan is complete;
  - every route is found;
  - a missing `awk` or `grep` fails;
  - a settings path that is a directory, a dangling link or unreadable fails;
  - each completion marker is required.
- **Probe, fakes:**
  - each forbidden variable, file and setting refuses;
  - each unsealed path, or one with the wrong access, refuses;
  - an incomplete environment scan refuses;
  - no `claude` command runs after any refusal.
- **Real Podman (`test_sealed_paths_podman.py`), in CI:**
  - as the sandbox user, writing, creating, removing, renaming, unmounting or
    remounting each sealed location fails;
  - the repository's `.claude/` is hidden inside and unchanged on the host;
  - Claude Code's state directory stays writable;
  - the real probe accepts the sealed sandbox and refuses an unsealed one.
- **Pinned version (`test_claude_code_pinned.py`), no credential needed:**
  - the real, pinned Claude Code runs sealed with an invalid key;
  - a repository `apiKeyHelper` never runs;
  - the authentication failure is classified as a provider refusal.
  - It needs the pinned image, so it runs with the Gate 1 evidence, not in
    CI.

## Revisit when

- A credential-injecting proxy (option 4) is accepted. Seals then become
  defence in depth.
- Claude Code reads settings or credentials from a location not listed here.
- The pinned-version test fails.
