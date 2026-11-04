# ADR-0010: Verify and publish one immutable candidate revision; humans accept

- Status: Accepted (PR #34, 2026-11-04)
- Date: 2026-11-04
- Sources: [R3], [R7], [R9], [R17] · Findings: F-004, F-005, F-017, F-021, F-022
- Amends: ADR-0004 (adds the candidate revision), ADR-0006 (host-side git)

## Context and problem

MANIFEST §5 says acceptance must not rest on the agent's own claim, and §2.6
says untrusted work must not get around approval. The M1 ports leave five
gaps:

1. `publish_branch` commits *whatever is in the working copy at publish time*.
   The published code can therefore differ from the code that was verified,
   for example if the agent container is still writing.
2. Nothing stops verification from running agent-edited tests or
   configuration inside the agent's own workspace.
3. Running host-side git commands in an agent-written repository can execute
   code. Repository-local config such as `core.fsmonitor`, `core.hooksPath` or
   a pager, and hooks, all run commands (F-017).
4. No verification evidence is persisted (MANIFEST §5 step 9).
5. Human approval holds only by convention.

## Considered options

1. Keep the current flow: verify the working copy, then commit and push it.
2. Stop the agent, verify in its workspace, then push from that same
   directory.
3. Freeze the agent's output into an immutable commit created by the
   coordinator. Verify that exact commit in a fresh sandbox under a trusted
   policy, and publish that exact commit.

## Decision

Chosen option: 3. Options 1 and 2 cannot guarantee that what was verified is
what gets published, and both run git on agent-controlled repositories.
Promoting one immutable build artefact is long-standing practice (F-022).

1. **Freeze.** When RUNNING_AGENT ends, the agent container is stopped first.
   The coordinator then records the agent's working tree as a commit on top
   of the recorded `base_revision`, in a git directory the coordinator owns.
   The agent workspace is treated as plain file content: its `.git`
   directory, hooks and config are ignored, and symlinks are stored as links
   and never followed. Nested repositories and special files cause the
   candidate to be rejected. The resulting commit id, `candidate_revision`, is
   persisted before the execution enters VERIFYING.
2. **Hardened host git.** Every host-side git command runs against a
   coordinator-owned git directory with system and global config ignored and
   hooks, fsmonitor, pagers, external diff/textconv tools and foreign
   credential helpers disabled. The host never uses the agent's `.git` as a
   repository.
3. **Verify.** VERIFYING checks out `candidate_revision` from the
   coordinator's repository into a fresh directory and a fresh sandbox
   (ADR-0011: network `NONE`, no credentials). The required checks come from a
   verification policy in Ilmar's configuration, never from the candidate.
4. **Trust boundary.** The policy decides *what* runs and how results are
   read. It cannot make candidate code trustworthy. Every artefact is
   therefore in one of three classes:

   | Class | Examples | Trusted for |
   | --- | --- | --- |
   | **Ilmar-controlled** | Verification policy (commands, timeouts, required checks, protected and check-influencing paths), the sandbox, the harness that runs commands and reads exit status | Everything |
   | **Independent checks** | Checks Ilmar runs itself on the candidate's tree or diff as *data*, executing nothing from the candidate: protected-path and check-influencing-path detection, forbidden file types, size limits, licence/SPDX headers, secret scanning | Pass/fail on their own |
   | **Candidate-executed checks** | Policy commands such as `pytest`, `ruff` or `make test`. They run code the candidate controls: tests, `conftest.py`, plugins, tool configuration in `pyproject.toml`, `setup.cfg` or `tox.ini`, build scripts, dependency manifests, and the code under test, which can patch the test runner at import time | Detecting honest mistakes; **not** proof against a deliberately subverted candidate |

   Rules:
   - Pass/fail of a candidate-executed check comes only from the exit status
     and timeout measured by the harness. Reports written by the candidate
     (JUnit XML, coverage files) are informational, never a verdict.
   - Candidate-executed code runs only in the verification sandbox: no
     network, no credentials, discarded afterwards. The worst it can do is lie
     about its result.
   - Resistance to an adversarial candidate comes from the sandbox,
     independent checks, the CI-configuration rule below and human review.
     Checks the agent cannot see, such as holdout scenarios (F-005, [R7]),
     are the planned strengthening.
5. **How modified tests and CI configuration affect evidence and acceptance.**
   The policy lists **check-influencing paths**: by default tests, test and
   tool configuration, build scripts, dependency manifests and lock files,
   and any repository file that verification reads. It also lists **CI
   configuration paths**, by default `.github/`. The diff from
   `base_revision` decides the outcome:

   | Candidate diff | Required checks | Outcome | Evidence `independence` |
   | --- | --- | --- | --- |
   | Any | An independent or candidate-executed required check fails | Not published (RETRY_WAIT or FAILED) | — |
   | Touches no check-influencing or CI paths | All pass | Published | `unchanged-checks`: the candidate passed the base revision's own checks |
   | Touches check-influencing paths | All pass | Published, with `requires_human_review` and a change-request label | `modified-checks`, listing the paths: the checks were partly written by the agent |
   | Touches CI configuration paths | Any | **Not published in M2.** FAILED, with reason `ci-config-modified` and a note on the task; a human takes over | `ci-config-modified` |

   CI configuration is blocked rather than just flagged because pushing it
   would run agent-written workflows, which may have access to repository
   secrets, before any human looks. As defence in depth, the publishing
   credential lacks permission to write workflow files (#7). CI results on a
   change request are never verification evidence. They are recorded as
   advisory external signals.

   Each attempt produces an **evidence record** containing:
   - the base and candidate revisions and the policy's hash;
   - for each check: its class, command, exit status, duration and an output
     digest;
   - the check-influencing, CI and protected paths the change touched;
   - `independence`, one of the values in the table above;
   - `requires_human_review`, with its reasons;
   - the agent's self-reported outcome, labelled as reported.

   A change to a protected path from the policy, such as `AGENTS.md` or the
   verification policy itself, adds `requires_human_review` in every case.
6. **Publish.** PUBLISHING pushes exactly `candidate_revision` to the task
   branch, with no commit at publish time, using an expected-head lease. It
   then confirms that the remote head equals the candidate. If the remote
   branch moved unexpectedly, for example because a human pushed to it,
   publishing stops for human attention and never overwrites.
7. **Reported versus accepted.** `AgentResult` is advisory. It can end an
   attempt early but can neither skip VERIFYING nor stand in for it (F-004).
   Exit codes are not trusted on their own (F-021).
8. **Approval.** During bootstrap, accepting a change into the default branch
   is human-only. No port has a merge operation, and branch protection on
   `main` blocks direct pushes (#28). SUCCEEDED means "verified change request
   published", never "accepted". Configurable approval policies (#20) are a
   later decision. They will read the evidence record, and they must not
   change what SUCCEEDED means.

## Consequences

- Good: published bytes equal verified bytes, provable by commit ids. The host
  never runs agent-controlled git configuration. The evidence record gives
  future approval policies something to work from.
- Bad: one extra checkout and container per attempt. Host-side git needs
  careful implementation and tests. The protected-path and check-influencing
  path lists must be maintained.
- Bad (stated honestly): when the candidate's own tests pass, that proves
  little against a deliberately subverted candidate. Code under test can
  tamper with its test run even if no test file changed. `unchanged-checks`
  guards against careless test weakening, not against an adversary.
- Bad: in M2, any task that needs a CI change cannot be completed by Ilmar.
- Port and domain changes, implemented by issues rather than this ADR:
  - `SourceControlProvider.publish_branch` is replaced by operations that
    freeze a candidate, check out a revision, and publish an exact revision
    (#3, #7).
  - `Execution` gains `base_revision` and `candidate_revision` (schema v2). The
    state machine requires a candidate to enter VERIFYING or PUBLISHING and
    clears it for a new attempt. Evidence records are stored per attempt (#9).

## Revisit when

- Attestation or signed commits are needed (M6).
- Approval policies are designed (#20).
- Tests of protected paths need finer rules, such as running the base
  revision's tests against the candidate (a "baseline run").
- Holdout checks hidden from the agent become available (F-005). Reassess how
  much `unchanged-checks` should count.
- Self-hosting (M4) needs Ilmar to change its own CI. Revisit the
  `ci-config-modified` rule together with approval policies (#20).

## Clarification (2026-11-04, PR #36 review)

Publishing is **fast-forward only**. When the task branch already exists,
`candidate_revision` must descend from its current head as well as match the
expected-head lease. Otherwise `NonFastForwardError` is raised and the branch
is left unchanged. An interrupted or retried task resumes from the branch
head (`checkout` returns that head as the base), so legitimate publishes are
always fast-forwards. Rewriting published history, for example by rebasing
onto an updated `main`, is not something Ilmar does in M2. If it is ever
needed, it requires its own decision. This clarifies §6 ("never overwrites");
the decision is unchanged.
