# ADR-0010: Verify and publish one immutable candidate revision; humans accept

- Status: Proposed
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
   (ADR-0011). The required checks come from a verification policy outside
   the candidate, namely Ilmar's configuration. Each attempt produces an
   **evidence record** containing:
   - the base and candidate revisions and the policy's hash;
   - for each check: the command, exit status, duration and an output digest;
   - the protected paths the change touched;
   - the agent's self-reported outcome, labelled as reported.
4. **Protected paths.** The policy lists paths whose change needs human review,
   such as tests, CI configuration (`.github/`), the verification policy and
   `AGENTS.md`. A candidate that touches them can still be verified and
   published. Its evidence then carries `requires_human_review` with the
   reasons, and the change request is labelled. If CI configuration changed,
   CI results on the change request never count as verification.
5. **Publish.** PUBLISHING pushes exactly `candidate_revision` to the task
   branch, with no commit at publish time, using an expected-head lease. It
   then confirms that the remote head equals the candidate. If the remote
   branch moved unexpectedly, for example because a human pushed to it,
   publishing stops for human attention and never overwrites.
6. **Reported versus accepted.** `AgentResult` is advisory. It can end an
   attempt early but can neither skip VERIFYING nor stand in for it (F-004).
   Exit codes are not trusted on their own (F-021).
7. **Approval.** During bootstrap, accepting a change into the default branch
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
  careful implementation and tests. The protected-path list must be
  maintained.
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
  revision's tests against the candidate.
