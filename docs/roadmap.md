# Roadmap

Milestones come from MANIFEST §10. Work is tracked in
[GitHub issues](https://github.com/Obtuse8053/Ilmar/issues), and current
position and order are in `STATUS.md`. This file maps milestones and approved
improvements to issues.

## Milestones

| Milestone | Status | Issues |
| --- | --- | --- |
| [M1 Foundation](https://github.com/Obtuse8053/Ilmar/milestone/1?closed=1) | Done (#1, merged 2026-11-01) | — |
| [M2 End-to-end execution](https://github.com/Obtuse8053/Ilmar/milestone/2) | In progress | #2–#11 |
| [M3 Reliability](https://github.com/Obtuse8053/Ilmar/milestone/3) | Planned | #12–#18 |
| [M4 Self-hosting](https://github.com/Obtuse8053/Ilmar/milestone/4) | Planned (epics) | #19–#21 |
| [M5 Portability](https://github.com/Obtuse8053/Ilmar/milestone/5) | Planned (epics) | #22–#24 |
| [M6 Public stability](https://github.com/Obtuse8053/Ilmar/milestone/6) | Planned (epic) | #25 |
| Maintenance (no milestone) | Open | #26–#31 |

Epics are split into issues when their milestone starts (`workflow.md`,
milestone close-out).

## Approved improvements from research

These are findings the maintainer approved as part of the backlog on
2026-11-01.

| Improvement | Finding | Milestone | Issue |
| --- | --- | --- | --- |
| Safe workspace path derivation | F-003 | M2 | #4 |
| Checks the agent cannot edit | F-005 | M2 | #9 |
| GitHub tracker state model | F-015 | M2 | #6 |
| Tracker reconciliation | F-001 | M3 | #12 |
| Backoff and stall detection | F-002 | M3 | #13 |
| Attempt handoff and legible verification output | F-006, F-009 | M3 | #14 |
| Second, open-source agent | F-010 | M5 | #24 |
| AGENTS.md size budget | F-007 | — | #30 |
| Fix the R3 URL in MANIFEST §9 | F-016 | — | #27 |

## How items get here

1. A finding is recorded in `research/findings.md`.
2. Someone opens an issue from the **Architecture proposal** template, labelled
   `proposal`.
3. The maintainer approves it, and the `proposal` label is replaced by
   `approved`.
4. A row is added above, and the finding's status becomes `proposed` with the
   issue number. It becomes `accepted` once the merged change or an ADR
   reflects it.
