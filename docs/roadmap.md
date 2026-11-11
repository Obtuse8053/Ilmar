# Roadmap

Milestone scope is defined in MANIFEST §10. The current milestone and the order
of work are in `STATUS.md`. Issue state lives on GitHub. This file links
milestones to their GitHub pages and approved research improvements to issues.

## Milestones

- [M1 Foundation](https://github.com/Obtuse8053/Ilmar/milestone/1)
- [M2 End-to-end execution](https://github.com/Obtuse8053/Ilmar/milestone/2)
- [M3 Reliability](https://github.com/Obtuse8053/Ilmar/milestone/3)
- [M4 Self-hosting](https://github.com/Obtuse8053/Ilmar/milestone/4)
- [M5 Portability](https://github.com/Obtuse8053/Ilmar/milestone/5)
- [M6 Public stability](https://github.com/Obtuse8053/Ilmar/milestone/6)
- [Maintenance (no milestone)](https://github.com/Obtuse8053/Ilmar/issues?q=is%3Aopen+no%3Amilestone)

M3 and M4 follow the autonomy roadmap in `autonomy-roadmap.md` (approved
2026-11-10): three gated operating modes, from supervised autonomy (M3) to
controlled self-hosting (M4).

Credential-containment requirements by gate (maintainer decisions,
2026-11-11; details in `autonomy-roadmap.md` and ADR-0036):

- **Before Gate 1:**
  - #110, the candidate secret scan;
  - the pinned-version compatibility test;
  - a controlled, credential-free test repository with trusted tasks and a
    capped workspace.
- **Before Mode 2, untrusted repositories or self-hosting:** #111, the
  credential-injecting egress proxy.

Epics (`type:epic`) are split into issues when their milestone starts (see
`workflow.md`, "Milestone close-out").

## Approved improvements from research

Findings the maintainer approved, with the issue that implements each.
`tests/architecture/test_docs.py` checks that this table matches the finding
statuses in `research/findings.md`.

| Improvement | Finding | Issue |
| --- | --- | --- |
| Tracker reconciliation | F-001 | #12 |
| Backoff and stall detection | F-002 | #13 |
| Attempt handoff | F-006 | #14 |
| Legible verification output | F-009 | #14 |
| Second, open-source agent | F-010 | #24 |
| AGENTS.md size budget | F-007 | #30 |
| Fix the R3 URL in MANIFEST §9 | F-016 | #27 |
| Stateful GitHub emulator over real git, with fault injection | F-027 | #51 |

## How items get here

1. A finding is recorded in `research/findings.md`.
2. Someone opens an issue from the **Architecture proposal** template, labelled
   `proposal`.
3. The maintainer approves it, and the `proposal` label is replaced by
   `approved`.
4. In the same PR, a row is added above and the finding's status is set to
   `proposed (#n, approved)`. The status becomes `accepted` once the merged
   change or an ADR reflects it, and the row is removed in that same PR.
