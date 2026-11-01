# Architecture decision records

Each ADR records one significant decision: its context, the decision itself,
its consequences, and any prior art that informed it. ADRs are numbered and
never deleted. To change a decision, add a new ADR that supersedes the old one
and update the old one's status.

| ADR | Title | Status |
| --- | --- | --- |
| [0001](0001-record-architecture-decisions.md) | Record architecture decisions | Proposed |
| [0002](0002-ports-and-adapters.md) | Ports and adapters with an enforced dependency rule | Proposed |
| [0003](0003-python-stdlib-first.md) | Python 3.11+, standard library first, no runtime dependencies in the core | Proposed |
| [0004](0004-execution-state-machine.md) | Explicit execution state machine with versioned records | Proposed |
| [0005](0005-sqlite-execution-store.md) | SQLite execution store with database-enforced single active execution | Proposed |
| [0006](0006-credentials-outside-the-sandbox.md) | Provider credentials stay outside the agent sandbox | Proposed |
| [0007](0007-configuration.md) | TOML configuration with per-port adapters and no inline secrets | Proposed |

Status values: Proposed, Accepted, Superseded by ADR-NNNN, Rejected. The ADRs
in the M1 foundation change stay Proposed until a maintainer approves that
change.

## Template

```markdown
# ADR-NNNN: Title

- Status: Proposed
- Date: YYYY-MM-DD

## Context
## Decision
## Consequences
## Prior art
Reference IDs from MANIFEST §9 ([R1]...), what was used (ideas only, or
material that is recorded in THIRD_PARTY_NOTICES.md), and access dates.
```
