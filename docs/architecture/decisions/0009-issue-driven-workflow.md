# ADR-0009: GitHub issues plus in-repository status as the development control plane

- Status: Proposed
- Date: 2026-11-01
- Sources: [R1], [R2], [R3], [R11] · Findings: F-006, F-007, F-015

## Context and problem

Ilmar is developed across many short sessions, each starting without the
previous conversation. The maintainer wants to steer all work from Claude Code
and approve every merge. Eventually Ilmar must run its own development
workflow. Plans, progress and handoffs therefore need a durable, shared home
that both a fresh agent session and Ilmar's own adapters can read.

## Considered options

1. Conversation history and chat-side project notes.
2. A GitHub Projects (v2) board. Its API is GraphQL-only, and GraphQL is
   blocked in Claude Code cloud sessions.
3. GitHub issues, labels and milestones, used through the REST API, plus a
   short `docs/STATUS.md` and `docs/roadmap.md` versioned with the code.
4. Repository files only, with no issue tracker.

## Decision

Chosen option: 3. The process is described in `docs/workflow.md`.

- Issues are the units of work. Milestones follow MANIFEST §10. Labels encode
  priority, type, approval, sensitivity and status.
- `docs/STATUS.md` is the resume point on `main`. It holds only what GitHub
  cannot: the current milestone, the planned order with dependencies, the
  recommended next task, and a change log. Issue state is never copied into it.
  Each fact has a single owner (`docs/workflow.md`, "Sources of truth"), and
  stable IDs that are repeated, such as finding-to-issue links, are checked by
  tests.
- Handoff comments on issues bridge interrupted sessions. Durable progress
  artefacts are what let long-running agent work resume ([R3]). Keeping plans
  and status in the repository follows [R2]'s practice of treating the
  repository as the agent's system of record.
- The tracker is the control plane, as in [R1]. The same issues and the
  `ilmar:ready` label are what Ilmar's GitHub `TaskProvider` will read (#6,
  #20). Label-based state is a known pattern in community ports ([R11], F-015).
- Agents never merge on their own initiative. A merge requires the
  maintainer's explicit instruction for that specific PR.

Option 1 does not survive a new session. Option 2 cannot be driven from cloud
sessions and ties planning to one provider's board. Option 4 loses
notifications, discussion and the tracker integration that Ilmar itself will
use.

## Consequences

- Good: any session can resume by reading `STATUS.md`, open PRs and issues. The
  workflow the humans and agents use now is the specification for Ilmar's own
  workflow later.
- Bad: `STATUS.md` is maintained by hand. Only PRs that change the plan, the
  milestone or the recommended next task edit it, so routine parallel PRs do
  not conflict there. Two plan-changing PRs still can.
- Bad: the documentation tests are offline. They prove internal consistency,
  not agreement with GitHub. Sessions compare the plan with live issue state
  when they start, and automating that is proposed in #35.

## Revisit when

- The GitHub `TaskProvider` (#6) settles its state model. Align the labels
  with it.
- `STATUS.md` conflicts or drift become frequent. Then generate the plan from
  issue data, for example milestone order plus dependency fields.
- The GitHub `TaskProvider` (#6) exists. Then reconsider #35 (a live reference
  check).
- Ilmar runs its own backlog (#20). Decide which steps it takes over.
