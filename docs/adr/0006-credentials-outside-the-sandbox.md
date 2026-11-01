# ADR-0006: Provider credentials stay outside the agent sandbox

- Status: Proposed
- Date: 2026-11-01

## Context

MANIFEST §2.6 says untrusted workloads must not be able to bypass orchestration
or approval policies. The coding agent runs arbitrary commands, may be steered
by untrusted task text (prompt injection), and runs project code and tests. If
it held a source-control token, it could push to protected branches, approve or
merge its own change, edit CI, or touch other repositories.

## Decision

- The **coordinator** (trusted, on the host) holds tracker and source-control
  credentials. It alone calls `TaskProvider` and `SourceControlProvider`.
- The **sandbox** (container) receives only a host directory with the working
  copy, mounted at `/workspace`, plus the environment the coordinator chooses.
  `SourceControlProvider.checkout` and `publish_branch` therefore operate on a
  host path. The agent edits files and may commit locally. Pushing and opening
  change requests happen afterwards, from the host, only after VERIFYING
  passes.
- `ContainerSpec.network` defaults to `NONE`.
- **Known exception:** the coding agent needs a credential for its own model
  API. In M2 this will be the only secret passed into the sandbox, and the
  Claude Code adapter will document how it is supplied and limited.
- **Open item for M2:** the agent needs network access to its model API, but
  `UNRESTRICTED` is broader than needed. An allow-list mode (for example an
  egress proxy) will be designed together with the Podman adapter. Until then,
  running with `UNRESTRICTED` must be an explicit configuration choice.

## Consequences

- A compromised agent can at worst produce a bad diff. Verification and review
  then catch it, and it cannot publish anything by itself.
- The coordinator must treat the workspace as untrusted input: git hooks and
  config written by the agent must not run with host credentials. The M2
  `publish_branch` implementation must neutralise repository-local hooks and
  config (for example by pushing from a clean clone, or with hooks disabled).
  The M2 contract tests will cover this.
- Changes to this boundary are a sensitive area (AGENTS.md §6).

## Prior art

- [R1] Symphony, [R4] Open SWE and [R5] OpenHands all run agents in isolated
  workspaces or sandboxes (as summarised in MANIFEST §9). The credential split
  described here is Ilmar's own design. Not accessed during M1.
