# Status

The resume point for any session (ADR-0009). This file holds only what GitHub
cannot: the planned order of work, the recommended next task, and a history of
changes. **Issue state** (open or closed, priority, labels, who is working on
what) **lives only on GitHub.** If this file and GitHub disagree about an issue,
GitHub is right; fix this file in your next PR. See `docs/workflow.md`,
"Sources of truth".

## Current milestone

**M2 End-to-end execution**
([milestone](https://github.com/Obtuse8053/Ilmar/milestone/2)). M1 was merged
in PR #1.

## Live state (query, don't copy)

- Work in progress:
  [open PRs](https://github.com/Obtuse8053/Ilmar/pulls) and
  [`status:in-progress` issues](https://github.com/Obtuse8053/Ilmar/issues?q=is%3Aopen+label%3Astatus%3Ain-progress)
- Waiting on the maintainer:
  [`needs:maintainer` issues](https://github.com/Obtuse8053/Ilmar/issues?q=is%3Aopen+label%3Aneeds%3Amaintainer)
- Milestone progress: the milestone page linked above

## Planned order (M2)

Dependency order for M2. This plan is owned here; issues do not repeat it.
Skip any step whose issue GitHub shows as finished.

| Step | Issue | Depends on |
| --- | --- | --- |
| 1 | #2 Session-resume workflow | — |
| 1a | #33 M2 design review (ADR-0010, ADR-0011, ADR-0012) | #2 |
| 2 | #3 Contract suites and fakes for `SourceControlProvider` and `AgentRunner` | #33 |
| 3 | #4 Safe workspace path derivation | #3 |
| 4 | #6 GitHub `TaskProvider` | #3 |
| 5 | #5 Podman `ContainerRuntime` and sandbox profile (network `NONE`) | #4 |
| 5a | #40 Sandbox egress allow-list and the ADR-0011 §9 suite | #5 |
| 6 | #7 GitHub `SourceControlProvider` | #3, #4 |
| 7 | #8 Claude Code `AgentRunner` | #3, #40 |
| 8 | #9 Verification stage | #5, #7 |
| 9 | #10 Adapter registry | before #11 |
| 10 | #11 Coordinator loop and end-to-end test (closes M2) | all of the above |

Steps 3 and 4 can run in parallel. Maintenance issues without a milestone can
fill any gaps between M2 steps.

## Recommended next task

#7, the GitHub `SourceControlProvider` with hardened host git and
exact-revision publishing (plan step 6). With #40 the sandbox is complete,
and #8 and #9 build on #7's candidate revisions. **Waiting for the
maintainer's explicit go-ahead.**

## Change log

- 2026-11-04: #40 finished (closes #5 together with PR #39):
  `NetworkPolicy.ALLOWLIST` via a per-execution proxy sidecar on an owned
  `--internal` network (ADR-0015, proposed; amends where ADR-0011's proxy
  runs). The standard-library proxy allows `CONNECT <listed-host>:443` only.
  The full ADR-0011 §9 suite runs on real Podman in CI with nonce controls
  (63 of 63). ICMP sockets are now explicitly disabled as defence in depth.
  Next recommended: #7.

- 2026-11-04: #5 part 1 merged (PR #39); #5 stays open until #40 completes
  the remaining sandbox requirements. Rootless Podman `ContainerRuntime` with
  the ADR-0011 profile and network `NONE`. ADR-0014 (accepted) records the
  Podman mapping: a `keep-id` non-root user, the runtime enforcing the
  recorded workspace identity before and after start, and a timed-out command
  removing its container (a port contract change). The contract, profile and
  network `NONE` checks run against real Podman in a new CI job.
  `UNRESTRICTED` needs a deployment opt-in; an unconfirmed removal raises
  `ContainmentError` and quarantines the workspace (restart-safe quarantine is
  part of #12 and #15). The allow-list proxy and the full §9 suite were split
  into #40. Next recommended: #40.

- 2026-11-04: #6 finished: GitHub `TaskProvider` on a standard-library REST
  client (REST only; token kept to the API origin, including on redirects).
  ADR-0013 (accepted) fixes the M2 threat model: one instance per repository
  and one coordinator per instance. Labels are workflow state, assignees mean
  human responsibility, and claim comments (instance plus unique token) are an
  audit record, not a lock. `ilmar:claimed` is owned by Ilmar; human claim
  labels such as `status:in-progress` are respected and never touched. A second instance is detected and both back off.
  Recovery is reconciled through the durable store (#12, #15). `integration`
  test marker introduced. Next recommended: #5.

- 2026-11-04: #4 finished: deterministic, collision-resistant names
  (branch, container); host layout with separate mountable and
  coordinator-only roots; symlink-safe directory creation, verification and
  descriptor-bound deletion; an `authorize_mount` gate before every mount; containment cases in the ContainerRuntime and
  SourceControlProvider contracts. Next recommended: #6.

- 2026-11-04: #3 finished: contract suites for all five ports, with
  ADR-0010/0012 port changes (immutable candidate revisions, expected-head
  leases with fast-forward-only publishing, owner-checked containers,
  structured agent outcomes). Next
  recommended: #4.

- 2026-11-04: M2 design review (#33). Proposed ADR-0010 (verify and publish
  one immutable candidate revision; humans accept), ADR-0011 (sandbox isolation
  and egress allow-listing) and ADR-0012 (recovery-safe side effects).
  Registered sources R13–R19 and findings F-017–F-022. Added the review's
  acceptance criteria to #3, #4, #5, #7, #8, #9, #11, #15, #18, #20 and #28,
  pending maintainer approval. Review round 2: ADR-0010 trust boundary and
  acceptance table (CI-config changes not published in M2); ADR-0011 per-execution
  networks and required negative network tests; ADR-0012 ownership-verified
  cleanup, resource ledger and fencing (#3, #4, #5, #7, #9, #11, #15, #16).
  Final clarifications: three-level fencing (instance lock held for the
  coordinator's lifetime, execution lease, immutable resource identity) and
  three-way outcomes (pass/fail/inconclusive) for network tests. ADR-0010 to
  ADR-0012 approved by the maintainer and accepted with PR #34.
- 2026-11-04: Independent review of M1 and PR #32. Status data was
  de-duplicated: issue state lives only on GitHub, the plan only here, and
  roadmap/finding links are checked by tests. ADR-0001 to ADR-0008 marked
  Accepted. Added the `needs:maintainer` label. Second review round:
  `STATUS.md` is edited only when the plan changes; ADR status flips get their
  own CI run before a SHA-pinned merge; the limits of the offline doc tests are
  documented, and a live check is proposed (#35).
- 2026-11-01: M1 merged (#1). Backlog approved and opened as issues #2–#31
  with milestones and labels. Started #2.
