# ADR-0031: Candidates that touch safety controls are never pushed; candidate code never runs with secrets

- Status: Accepted
- Date: 2026-11-10
- Sources: none external · Findings: F-013
- Depends on: ADR-0010, ADR-0018

## Context and problem

The autonomy roadmap lets Ilmar change code and open pull requests,
eventually on its own repository. The maintainer's rule: **Ilmar never
autonomously weakens its safety controls and never promotes an executable
version.** Humans merge every pull request (ADR-0010). That alone is not
enough, for two reasons:

1. **Pushing is already an effect.** Workflows triggered by a pull request
   from a branch of the same repository run with that repository's secrets.
   A candidate that edits `.github/` could exfiltrate them before any human
   looks.
2. **Review fatigue.** A subtle change to a budget check, a fencing rule or
   an architecture test is easy to miss among routine changes.

Ilmar already keeps the verification policy out of the repository being
changed: it lives in the operator's configuration (ADR-0010, ADR-0018), and
`config.py` documents that it is never read from the repository.

## Considered options

1. *Rely on human review.* Rejected, for the reasons above.
2. *Allow such candidates, but open them as drafts.* Rejected. Workflows
   still run on push.
3. *Push them to a fork.* Rejected for now. It needs a second account or a
   GitHub App, and its secret scope would have to be proven first.
4. *Freeze such candidates with their evidence and never push them.*
   **Chosen.** A maintainer can apply the change by hand.

## Decision

### 1. The protected list

The list is code in the running release, in a module that is itself on the
list.

- **For every target repository:**
  - `.github/`;
  - any CI configuration the provider runs automatically.
- **When the target is Ilmar's own repository** (the configured
  `self_repository`):
  - `.github/`, `AGENTS.md`, `CLAUDE.md`, `MANIFEST.md`, `LICENSE`, `NOTICE`
    and `THIRD_PARTY_NOTICES.md`;
  - `tests/architecture/`, `tests/contracts/` and every `conftest.py`;
  - `pyproject.toml` (dependencies, and tool configuration that affects
    verification), and `scripts/`;
  - the code of credentials, the sandbox and container policy (the
    `ContainerRuntime` adapters), the gateway, fencing, ownership,
    quarantine, the journal, budgets, limits, suspension, time, restarts,
    configuration validation, verification policy and gate records;
  - the protected-list module itself.
- **Operator additions.** The operator may add patterns
  (`protected_paths_extra`). Nothing can remove a built-in entry.

The list names directories and modules. #92 maps it to exact paths and keeps
an architecture test that fails when a new module of one of those kinds
appears outside the list.

### 2. The check

The check runs on the frozen candidate in the coordinator's repository
(ADR-0010), not on anything the agent says:

- once after the freeze, before verification;
- again immediately before publication.

It covers:

- added, modified and deleted files;
- both sides of a rename;
- mode changes;
- symbolic links whose target is protected;
- `.gitattributes` and `.gitmodules`, wherever they are.

### 3. The outcome: a blocked task

If any protected path is touched:

- **Evidence:** the candidate and its evidence are kept, and an event names
  the paths.
- **No publication:** nothing is pushed and no change request is opened,
  now or by recovery after a crash.
- **Terminal:** the execution ends as FAILED with reason code
  `protected-path`.
- **Claimed and blocked:** the task keeps Ilmar's claim and is *blocked*.
  It is never retried, never picked again and never published
  automatically.

**Human resolution.** Only a maintainer resolves a blocked task. Two host
commands do it, in the same shape as #59's quarantine commands:

- `ilmar blocked release <task>` releases the claim, for example after
  applying the change by hand or rewording the issue, so the task may be
  picked again;
- `ilmar blocked abandon <task>` releases the claim and leaves the task
  out of Ilmar's backlog.

`ilmar blocked list` shows the blocked tasks with their paths and evidence.

**A release really makes the task eligible again** (maintainer requirement
at acceptance, 2026-11-10). Today the pick treats every task with a FAILED
execution as finished for good (`Coordinator._pick`). Releasing only the
tracker claim would therefore leave the task stuck. So `ilmar blocked
release` also records a durable *human release* of that FAILED execution,
in the same commit as its journaled claim release. The pick ignores released
FAILED executions. Every other reason a task counts as finished still holds:
an active execution, a published change request, an unreleased failure, or a
cancellation that cannot be re-picked. The next attempt is a new execution
with fresh reservations and limits. A regression test proves that a released
task is picked again and that an unreleased FAILED task is not.
Both resolving commands are journaled tracker effects with fencing, like
any other claim release (ADR-0026).

**No starvation.** A blocked task holds a claim but no execution slot.
Because its execution is terminal, it does not count against
`max_concurrent_executions`. The pick skips blocked tasks explicitly and
goes on to the next eligible task in the same pass, so any number of
blocked tasks never stops the rest of the backlog. Each pass emits an event
with the number of blocked tasks, so they stay visible.

### 4. Candidate code never runs where secrets are

Every candidate is untrusted code, not only those that touch protected
paths. Pushing a branch to the target repository runs its workflows. In a
pull request from a branch of the *same* repository, those workflows can
receive the repository's secrets and a writable token. Before Ilmar pushes
to a target repository, these must hold:

- **No secrets for candidate code.** No workflow that a pushed branch or
  its pull request triggers may receive secrets, other than a read-only
  `GITHUB_TOKEN`. That includes same-repository pull requests.
- **Read-only tokens.** Such workflows declare read-only `permissions`, and
  the repository's default token permission is read-only.
- **No privileged triggers on candidate code.** No `pull_request_target` or
  `workflow_run` workflow checks out or runs candidate code. No workflow
  with secrets runs on `push` to the branches Ilmar pushes.
- **No shared caches.** Workflows that run candidate code share no cache or
  artifact with a workflow that holds secrets, because a shared cache could
  carry poisoned content into the privileged workflow.
- **Manual privileged workflows run reviewed code only.** Workflows with
  secrets that are started by hand (`workflow_dispatch`) run only a reviewed
  commit on the default branch, as `live.yml` does today.

How each is checked:

- **Workflow files:** before every push, Ilmar inspects the target's
  workflow files at the base commit. If it finds a violation it can read
  from those files, it refuses to push.
- **Settings:** the repository and organisation settings Ilmar cannot read
  with its own token (secret scopes, the default token permission) are
  attested by the operator. Gate 1 verifies them for the test repository.
- **Ilmar's own repository:** its `ci.yml` already declares `contents: read`
  and uses no secrets, and `live.yml` is `workflow_dispatch` only. These
  rules are still checked again before the M4 gate that would allow Ilmar
  to target this repository.

### 5. Nothing a candidate writes configures the factory

The following are read only from the running release and the operator's
configuration, never from a candidate's tree:

- the verification policy;
- the protected list;
- budgets and limits;
- the gate records;
- supervisor files.

An architecture test asserts that their loaders take no path inside a
workspace or candidate.

### 6. Promotion

Promotion of an executable version is a human command in every mode
(ADR-0034, to come). No configuration value enables automatic promotion.

## Consequences

- Good:
  - a candidate cannot reach repository secrets through workflows, and cannot
    weaken a safety control unnoticed;
  - the guard does not depend on the agent's behaviour.
- Bad:
  - legitimate improvements to safety code are written by Ilmar but must be
    applied by a human;
  - the list must keep up with new modules, which the architecture test
    enforces.

## Tests required

- One candidate per protected entry: frozen, never pushed, reason
  `protected-path`.
- Renames into and out of protected paths, symbolic links to protected
  files, and mode changes.
- Candidates that edit the protected list or the verification policy.
- An operator extension takes effect; an attempt to remove a built-in entry
  through configuration is rejected.
- A crash between the check and publication: recovery never publishes a
  protected candidate.
- A blocked task is never retried or re-picked. A backlog with blocked
  tasks ahead of eligible ones still processes the eligible ones in the same
  pass, with `max_concurrent_executions` at 1.
- `ilmar blocked release` and `abandon` are fenced and crash-safe, and
  `list` reports every blocked task.
- The pre-push workflow inspection refuses each violation it can read from
  workflow files: `pull_request_target` or `workflow_run` running candidate
  code, secrets in workflows triggered by pushed branches, write
  permissions, and caches shared with privileged workflows.

## Revisit when

- Pull requests can be opened from a fork whose workflows provably get no
  secrets. Pushing could then be allowed, while merging stays human.
- F-013's condition is met (mature verification evidence). That could
  change the scope of human approval, never its existence.
