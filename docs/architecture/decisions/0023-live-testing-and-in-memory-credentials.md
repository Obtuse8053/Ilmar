# ADR-0023: Live end-to-end testing, and credentials that never touch disk

- Status: Proposed
- Date: 2026-11-05
- Sources: [R13] · Findings: F-018, F-022
- Implements: #11d (harness), #44 (record); amends ADR-0014 (environment
  values), builds on ADR-0006, ADR-0010, ADR-0015, ADR-0017, ADR-0022

## Context and problem

M2 ends with one real task end to end: a GitHub issue, real Claude Code in the
allowlisted Podman sandbox, verification, a pull request, and a merge that
completes the task. That run uses two real credentials: a GitHub token, and
the model key that is the documented exception allowed into the sandbox
(ADR-0006).

The maintainer decided:
- the run uses only a private `ilmar-disposable` repository with the
  `ilmar-disposable` topic;
- the GitHub token is fine-grained, scoped to that repository, expires
  within 7 days, and has no Workflows, Actions or Administration
  permission;
- the model key is dedicated and spend-capped;
- the run is local and attended, with credentials only in environment
  variables, never printed, persisted or exposed;
- it includes a crash drill;
- unattended operation stays disabled.

Preparing the crash drill showed a gap. ADR-0014 passes container
environment values in a temporary `--env-file` that is deleted "right after
use". That `finally` never runs when the coordinator is killed, so a
`SIGKILL` during the agent run left **the model key in `$TMPDIR`**. A test
reproduced it.

## Considered options

**Environment values for `podman`:**
- A temporary file, as before. It persists after a crash.
- `--env NAME`, with the value in `podman`'s own environment. That collides
  with the variables `podman` itself needs, such as `HOME`, and widens what
  the `podman` process sees.
- **A memfd passed as `--env-file /dev/fd/N`.** It has no path, lives only
  in memory, is inherited only by that `podman` call, and the kernel frees
  it when the last descriptor closes, including on `SIGKILL`.

**Checking the token's restrictions:**
- trust the instructions;
- **probe them.** GitHub has no endpoint that lists a fine-grained token's
  permissions, but the response headers give its expiry and type.
  Administration and Actions are probed with read requests that need them.
  Workflows is a write-only permission, so it is probed with one write to a
  throwaway branch of the disposable repository, which must be refused.

**Making live tests safe to run:**
- a script;
- **pytest tests marked `integration`, plus an offline-tested harness.**
  The guards and leak detection are code, and code has tests.

## Decision

### Credentials reach `podman` through memory only (amends ADR-0014)

`PodmanRuntime` writes `ContainerSpec.env` and per-command `env` into a
`memfd_create` descriptor (`MFD_CLOEXEC`). It passes
`--env-file /dev/fd/N`, and the executor gives only that `podman` call the
descriptor (`pass_fds`). The descriptor is closed right after the call. No
value is ever on disk or on a command line. The `Executor` protocol gains
an optional `pass_fds`. The rest of ADR-0014's environment rules stand.

### Unattended operation stays disabled

`ilmar run` without `--once` exits 3 and explains why. Changing that needs
#44 recorded and #20 designed and approved. There is no configuration
switch for it.

### The live harness (`tests/live_harness.py`)

- **Guard (`preflight`), before anything is written.** The run is refused
  unless:
  - the repository is `<owner>/ilmar-disposable`, private, unarchived and
    has the topic;
  - the token is fine-grained (`github_pat_`, no `X-OAuth-Scopes`) and
    expires within 7 days (`GitHub-Authentication-Token-Expiration`);
  - the token gets 404 for a private outside repository;
  - the token gets 403 or 404 for the Administration and Actions probes;
  - the token cannot create a workflow file.
- **Isolation per run:** a new base branch `ilmar-live/<run>`, whose commit
  has the default branch as its parent, plus its own label and issue.
  Nothing existing is moved or force-pushed. Everything the run made is
  closed or deleted afterwards.
- **Secrets:**
  - read from the environment into a `Secret`, whose `repr` and `str` are
    redacted;
  - handed to the CLI only through its environment, together with the
    variables `podman` and git need;
  - leak findings name a place and a variable, never a value;
  - credential searches run outside `assert` statements, because pytest
    prints the arguments of calls inside a failing assertion. An offline
    test checks the live test files for this.
- **Leak detection:**
  - each credential is searched for in its raw, base64, URL-encoded and git
    Basic-auth (`x-access-token:`) forms;
  - in the state directory (database, coordinator repository, workspaces),
    the configuration, the CLI output and the evidence;
  - in every file of the frozen candidate;
  - in the pull request, its commits and the issue comments;
  - in files in the temporary directories;
  - in every readable process's argv, polled during the run;
  - in every container's definition. The model key may appear only in the
    agent's sandbox.
- **Containment:**
  - a watcher records every container of the run's instance as it appears,
    and follows each egress proxy's log until the proxy is removed;
  - the proxy's `--allow` must be exactly the configured endpoint
    (`api.anthropic.com`);
  - its log must show only that host, at least once, and no `DENY`;
  - verification sandboxes must have network `none`.
- **Verified equals published:**
  - the pull request's head is the branch head, the execution's candidate
    and the evidence's candidate, with the evidence digest equal to the one
    the verify step recorded;
  - its files are exactly `git diff base candidate` in the coordinator's
    repository;
  - there is exactly one pull request into the run's base branch.
- **Completion:** after a pinned merge, the next `run --once` reports the
  task complete, releases the claim and comments on the issue. The
  change-request row is released.
- **Crash drill:**
  - `SIGKILL` the CLI's process group while the agent operation is
    `INTENDED`;
  - the sandbox must still exist and no pull request may be open;
  - the next pass publishes from attempt 2: attempt 1's run is `FAILED` and
    attempt 2's `SUCCEEDED`;
  - one pull request; nothing of the instance is left in Podman.
- **Records:** JSON files without credentials (checked before writing) for
  #44 and for each run, to post on the issues.

## Consequences

- A crash can no longer leave a credential on disk through Ilmar. A
  regression test kills a process while it holds the descriptor.
- `memfd_create` is Linux-only. So is rootless Podman as Ilmar uses it.
- The live run costs four short Claude Code sessions: #44, the end-to-end
  run, and two in the crash drill.
- The guard relies on GitHub's response headers and on 403/404 answers.
  If GitHub changes them, the guard refuses to run rather than passing
  silently. The outside-repository probe needs a private repository: the
  Ilmar repository is public.
- #11 closes only after the live tests have passed and their records are on
  the issue.

## Revisit when

- GitHub offers an endpoint that describes a fine-grained token's
  permissions: replace the probes.
- A runtime other than Podman, or a non-Linux host, is supported.
- Unattended operation (#20) is proposed.
