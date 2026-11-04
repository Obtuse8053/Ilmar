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
| 2 | #3 Contract suites and fakes for `SourceControlProvider` and `AgentRunner` | #2 |
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

#3, the contract suites and fakes, once the maintainer approves. Every M2
adapter is written against these contracts.

## Change log

- 2026-11-04: Independent review of M1 and PR #32. Status data was
  de-duplicated: issue state lives only on GitHub, the plan only here, and
  roadmap/finding links are checked by tests. ADR-0001 to ADR-0008 marked
  Accepted. Added the `needs:maintainer` label.
- 2026-11-01: M1 merged (#1). Backlog approved and opened as issues #2–#31
  with milestones and labels. Started #2.
