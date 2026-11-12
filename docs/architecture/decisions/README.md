# Architecture decision records

Each ADR records one significant decision. The format is a lightweight variant
inspired by MADR ([R10]): problem, considered options, decision, consequences,
sources, and the conditions under which to reconsider. ADRs are never deleted.
A changed decision gets a new ADR, and the old one is marked superseded.

| ADR | Title | Status |
| --- | --- | --- |
| [0001](0001-record-architecture-decisions.md) | Record architecture decisions | Accepted |
| [0002](0002-ports-and-adapters.md) | Ports and adapters with an enforced dependency rule | Accepted |
| [0003](0003-python-stdlib-first.md) | Python 3.11+, standard library first, no runtime dependencies in the core | Accepted |
| [0004](0004-execution-state-machine.md) | Explicit execution state machine with versioned records | Accepted |
| [0005](0005-sqlite-execution-store.md) | SQLite execution store with database-enforced single active execution | Accepted |
| [0006](0006-credentials-outside-the-sandbox.md) | Provider credentials stay outside the agent sandbox | Accepted |
| [0007](0007-configuration.md) | TOML configuration with per-port adapters and no inline secrets | Accepted |
| [0008](0008-research-knowledge-base.md) | In-repository research knowledge base and review process | Accepted |
| [0009](0009-issue-driven-workflow.md) | GitHub issues plus in-repository status as the development control plane | Accepted |
| [0010](0010-acceptance-integrity.md) | Verify and publish one immutable candidate revision; humans accept | Accepted |
| [0011](0011-sandbox-isolation-profile.md) | Sandbox isolation profile and egress allow-listing | Accepted |
| [0012](0012-recovery-safe-side-effects.md) | Recovery-safe external side effects | Accepted |
| [0013](0013-github-task-state.md) | GitHub task state under a single-coordinator deployment | Accepted |
| [0014](0014-podman-runtime-mapping.md) | Podman runtime mapping, workspace identity and command timeouts | Accepted |
| [0015](0015-egress-proxy-sidecar.md) | Egress allow-list as a per-execution proxy sidecar | Accepted |
| [0016](0016-host-git-and-github-publishing.md) | Plumbing-only host git and GitHub publishing | Accepted |
| [0017](0017-claude-code-agent-runner.md) | Claude Code agent runner | Accepted |
| [0018](0018-verification-stage-and-evidence.md) | Verification stage, policy and append-only evidence | Accepted |
| [0019](0019-adapter-registry-and-composition.md) | Adapter registry, two-phase construction and the `Components` bundle | Accepted |
| [0020](0020-fencing-journal-and-resource-ledger.md) | Fencing, the write-ahead operation journal and the resource ledger | Accepted |
| [0021](0021-coordinator-execution.md) | One coordinator execution: steps, failure classes, claims and task completion | Accepted |
| [0022](0022-crash-recovery.md) | Crash recovery: durable state is the only memory | Accepted |
| [0023](0023-live-testing-and-in-memory-credentials.md) | Live end-to-end testing, and credentials that never touch disk | Accepted |
| [0024](0024-structured-event-log.md) | A structured event log, written in the same commit as the change it describes | Accepted |
| [0025](0025-cancellation.md) | Cancellation through a request inbox, applied by the lease holder at safe points | Accepted |
| [0026](0026-tracker-reconciliation.md) | Tracker reconciliation: task standing, orphaned claims and tracker-driven cancellation | Accepted |
| [0027](0027-stray-claim-labels.md) | Stray claim labels, and two clarifications to ADR-0026 | Accepted |
| [0028](0028-time-bounded-scheduling.md) | A persisted time may only delay new work | Accepted |
| [0029](0029-supervised-mode-limits-and-budget.md) | Mode 1 limits, accounting reservations, suspension and one credential path | Accepted |
| [0030](0030-operating-modes-and-gates.md) | Operating modes, gate records and the supervised loop | Accepted |
| [0031](0031-protected-safety-controls.md) | Candidates that touch safety controls are never pushed; candidate code never runs with secrets | Accepted |
| [0033](0033-supervisor-and-restart-accounting.md) | A systemd supervisor with durable restart accounting | Proposed |
| [0035](0035-agent-progress-and-stall-detection.md) | Agent progress from stream events, and stall detection | Accepted |
| [0036](0036-sealed-agent-settings.md) | Seal the agent's own settings at the sandbox boundary | Proposed |
| [0037](0037-sidecar-model-gateway.md) | A model gateway in the egress sidecar, with worst-case cost reservations | Proposed |

Status values: Proposed, Accepted, Superseded by ADR-NNNN, Rejected. An ADR
is Proposed while its PR is open. It becomes Accepted when the maintainer
approves that PR. The status change is pushed to the PR as its own commit, and
the PR is merged only after CI passes on that commit (`docs/workflow.md`,
"Finish"). ADR-0001 to ADR-0008 were accepted with PR #1.

## Template

```markdown
# ADR-NNNN: <decision as a short phrase>

- Status: Proposed
- Date: YYYY-MM-DD
- Sources: [Rn], … (see docs/research/sources.md) · Findings: F-nnn, …

## Context and problem
What forces a decision, in a few sentences.

## Considered options
1. Option A
2. Option B

## Decision
Chosen option: "Option B", because …

## Consequences
- Good: …
- Bad: …

## Revisit when
Observable conditions or evidence that should reopen this decision.
```
