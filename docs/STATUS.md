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
| 5 | #5 Podman `ContainerRuntime` and sandbox profile | #4 |
| 6 | #7 GitHub `SourceControlProvider` | #3, #4 |
| 7 | #8 Claude Code `AgentRunner` | #3, #5 |
| 8 | #9 Verification stage | #5, #7 |
| 9 | #10 Adapter registry | before #11 |
| 10 | #11 Coordinator loop and end-to-end test (closes M2) | all of the above |

Steps 3 and 4 can run in parallel. Maintenance issues without a milestone can
fill any gaps between M2 steps.

## Recommended next task

#3, the contract suites and fakes, **waiting for the maintainer's explicit
go-ahead**. #3 is where ADR-0010's port changes become executable
contracts that every M2 adapter is then written against.

## Change log

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
