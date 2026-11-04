# ADR-0006: Provider credentials stay outside the agent sandbox

- Status: Accepted (PR #1, 2026-11-01); amended by ADR-0010 and ADR-0011
- Date: 2026-11-01
- Sources: [R1], [R4], [R5], [R7], [R11] · Findings: F-003, F-005

## Context and problem

MANIFEST §2.6: untrusted workloads must not bypass orchestration or approval.
The agent runs arbitrary commands and may be steered by untrusted task text. If
it held an SCM token it could push to protected branches, change CI, or merge
its own work.

## Considered options

1. The agent holds an SCM token and pushes or opens PRs itself from workspace
   hooks. This is common in community R1 setups ([R11]), and R1 describes its
   own posture as suited to trusted environments.
2. A narrowly scoped, short-lived token inside the sandbox.
3. Only the coordinator holds tracker and SCM credentials. The sandbox gets the
   working copy only.

## Decision

Chosen option: 3. Option 2 is still exposed to prompt injection within the
token's scope, and it cannot stop the agent from editing the verification it is
judged by (F-005).

- The coordinator alone calls `TaskProvider` and `SourceControlProvider`.
  `checkout` and `publish_branch` operate on a host path.
- The sandbox gets the workspace mounted at `/workspace`. Network access is
  `NONE` by default.
- Publishing happens only after VERIFYING passes.
- Known exception: the agent's model-API credential. It is the only secret
  allowed in the sandbox and is documented by the agent adapter.

## Consequences

- Good: a compromised agent can at worst produce a bad diff, which
  verification and review catch.
- Bad: the coordinator must treat the workspace as untrusted. Repository-local
  git hooks and config written by the agent must not run with host
  credentials. The M2 `publish_branch` must neutralise them (clean clone or
  hooks disabled) and test this.
- Workspace paths must be derived safely from task keys (F-003).

## Revisit when

- Egress allow-listing is designed with the Podman adapter (M2). Until then,
  `UNRESTRICTED` network access must be an explicit configuration choice.
- Platforms offer credentials that can be scoped to push only to one branch,
  with no CI or settings access. Then reassess option 2.
