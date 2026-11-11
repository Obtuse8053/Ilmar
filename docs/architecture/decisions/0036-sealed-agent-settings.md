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
     container.
   - **Podman:** uses `--tmpfs <path>:ro,nosuid,nodev,noexec,notmpcopyup`, or
     `rw` for writable paths. `notmpcopyup` matters: without it, Podman copies
     whatever is at the path into the tmpfs. The first CI run of this change
     showed the repository's `.claude/settings.json` reappearing inside the
     seal.
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
   | `~/.claude/.credentials.json` | read-only, empty | Claude login and OAuth tokens on Linux; added in review, because the writable `~/.claude` would otherwise let the agent create it after the check |

   The sealed project directory hides the repository's own `.claude/` from the
   agent. The agent can neither read nor change it, so a candidate can never
   contain changes to it.
3. **The probe checks the boundary and fails closed.** Before Claude Code
   starts, the probe runs with Claude Code's exact environment and reports the
   mount table.
   - **Refused (`policy-violation`, never retried):**
     - a sealed path that is not the visible mount (none at all, or one hidden
       under a later mount of a parent directory), or that has the wrong
       access;
     - a read-only seal that is not empty;
     - a variable the runner passed that is missing from the scan;
     - `CLAUDE_CONFIG_DIR`, which would move user settings elsewhere;
     - any route ADR-0029 §5 already forbids.
   - **Never lets Claude Code start (`ProviderError`):** any step that does
     not finish. That covers a tool missing, a file that cannot be read, a
     missing completion marker or an explicit error line.

## Consequences

This decision does **not** give a complete single-credential guarantee. The
table separates what is enforced today from what only the credential-injecting
proxy (option 4) can enforce.

| Route to a model | Enforced today | Needs the proxy |
| --- | --- | --- |
| A credential variable in the image, container or runner environment | Refused at start by the probe (names only) | — |
| A Bedrock, Vertex or other provider endpoint | Blocked by the egress allowlist (the model host only) | — |
| A foreign `ANTHROPIC_BASE_URL` | Refused at start; other hosts are blocked by the allowlist | — |
| `apiKeyHelper` or `env` in project or user settings, at start or during the run | Sealed: the files cannot be created or changed (real-Podman tests) | — |
| An OAuth login in `~/.claude/.credentials.json`, at start or during the run | Sealed: it cannot be created or changed | — |
| `CLAUDE_CONFIG_DIR` moving the settings elsewhere | Refused at start; the agent cannot change the environment of the running Claude Code | — |
| Login state or an API key in `~/.claude.json` | Refused only if the file exists at start; **it stays writable during the run** | Yes |
| A nested `claude` started by the agent, with its own configuration | Not prevented; it can use only credentials already in the sandbox | Yes |
| A credential already in the sandbox (committed to the repository, or the configured key itself), used directly against the model host | Not prevented: the allowlist admits any credential to the model host | Yes |
| The configured key copied into a published candidate | **Not prevented today** (see Gate 1 below) | Yes: the key would never be in the sandbox |

**`~/.claude.json`.** According to the documentation, it holds the OAuth
session, MCP server configuration and per-project state [R29]. Claude Code
rewrites it during a run, so it cannot be sealed without breaking Claude Code.
- **What the agent can write there:** login data or an API key. Whether a
  given Claude Code version would use such an entry ahead of the
  `ANTHROPIC_API_KEY` it was started with is not established; no version could
  be run from the development environment.
- **MCP servers:** those in the file are ignored, because Ilmar runs Claude
  Code with `--strict-mcp-config`.
- **Bounded, not closed:** a value written there must already exist in the
  sandbox, and the allowlist admits only the model host. So this route
  matters only where another credential is reachable from inside the sandbox.
  It stays open until the proxy exists.

**What the agent loses:** it cannot see or edit a repository's `.claude/`
directory.

### Gate 1 and the proxy (recommendation, for the maintainer to decide)

Gate 1 runs Mode 1, supervised, on a dedicated test repository (ADR-0030). On
that repository the routes the proxy would close need a credential other than
the configured key inside the sandbox. A test repository the maintainer
controls holds none. **The proxy is therefore not strictly required for Gate
1**, provided that:

1. **The pinned-version compatibility test passes** (mandatory, as before):
   the pinned Claude Code runs with these seals, including the
   credentials-file seal, and never uses repository settings.
2. **The test repository has no credentials:** none in its content or
   history, and no secret-bearing workflows. This is recorded with the gate
   evidence.
3. **Publication cannot leak the configured key.** Today the agent can read
   the key and could write it into a candidate that Ilmar then publishes. A
   deterministic check that refuses to publish a candidate containing the
   value of any configured secret (#110) is a **Gate 1 prerequisite**. The proxy
   removes the need, because the key never enters the sandbox.

**The proxy must be in place before:**
- any repository that is not fully controlled by the maintainer or may contain
  credentials;
- Mode 2 or any unsupervised operation;
- self-hosting (M4).

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
  - as the sandbox user, writing, creating, removing, renaming, linking,
    unmounting or remounting each sealed location fails, including
    `~/.claude/.credentials.json`; after every attempt it is still the empty
    seal, and the probe still accepts the sandbox;
  - the repository's `.claude/` is hidden inside and unchanged on the host;
  - Claude Code's state directory stays writable;
  - the real probe accepts the sealed sandbox and refuses an unsealed one.
- **Pinned version (`test_claude_code_pinned.py`), no credential needed;
  mandatory for Gate 1:**
  - the real, pinned Claude Code runs sealed with an invalid key, including
    the read-only directory where it would look for login credentials;
  - a repository `apiKeyHelper` never runs;
  - the authentication failure is classified as a provider refusal.
  - It needs the pinned image, so it runs with the Gate 1 evidence, not in
    CI.

## Revisit when

- A credential-injecting proxy (option 4) is accepted. Seals then become
  defence in depth.
- Claude Code reads settings or credentials from a location not listed here.
- The pinned-version test fails.
