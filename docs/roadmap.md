# Roadmap

Milestones come from MANIFEST §10. This file adds only **status** and the
**GitHub issues** that track approved work. An improvement is listed here once
a maintainer approves its proposal issue; until then it lives in
`research/findings.md` with status `open` or `proposed`.

## Milestones

| Milestone | Status | Tracking issue |
| --- | --- | --- |
| M1 Foundation | In review ([PR #1](https://github.com/Obtuse8053/Ilmar/pull/1)) | — |
| M2 End-to-end execution | Next | to be opened |
| M3 Reliability | Planned | — |
| M4 Self-hosting | Planned | — |
| M5 Portability | Planned | — |
| M6 Public stability | Planned | — |

## Approved improvements

| Improvement | Finding | Milestone | Issue | ADR |
| --- | --- | --- | --- | --- |
| *(none approved yet)* | | | | |

## Awaiting a decision

Open findings with a recommendation, grouped by suggested milestone. Each needs
a proposal issue and maintainer approval before it moves to the table above.

- **M2:** F-003 (workspace path safety), F-005 (checks the agent cannot edit),
  F-015 (GitHub tracker model)
- **M3:** F-001 (tracker reconciliation), F-002 (backoff and stall detection),
  F-006 (attempt handoff), F-009 (legible verification output)
- **M5:** F-010 (open-source second agent)
- **Maintenance:** F-007 (AGENTS.md size budget), F-016 (fix the MANIFEST §9
  URL for R3)

## How items get here

1. A finding is recorded in `research/findings.md`.
2. Someone opens an issue from the **Architecture proposal** template that
   links the finding and any affected ADR.
3. A maintainer approves it by applying the `approved` label or commenting.
4. The approver or implementer adds a row above with the issue link. The
   finding's status becomes `proposed`, then `accepted` once an ADR or merged
   PR reflects it.
