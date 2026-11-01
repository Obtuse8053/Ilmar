# Research findings

Evidence-based observations about Ilmar's architecture, each tied to sources in
`sources.md`. A finding is not a decision. Decisions are recorded in ADRs, and
work is tracked in GitHub issues (see `docs/roadmap.md`).

**Evidence level**

- **Established:** supported by several independent primary sources or by
  long-standing engineering practice, or already verified in Ilmar.
- **Emerging:** reported by one credible primary source, such as an
  organisation's experience report or a reference implementation, but not
  independently replicated.
- **Experimental:** community implementations, anecdotes, secondary-only
  evidence or our own hypotheses. Needs a prototype or primary confirmation
  before it can justify a decision.

**Status:** `open` (not yet proposed), `proposed` (issue opened), `accepted`
(reflected in an ADR), `rejected` (with reason) or `superseded`.

Entries are paraphrased. Never paste source text here (MANIFEST §7).

---

### F-001: The tracker should stay the authority on eligibility; reconcile every tick

- Evidence: **Emerging** · Sources: [R1], [R11]
- Observation: R1 keeps scheduler state in memory on purpose. A restart does
  not restore retry timers, and each poll re-reads the tracker. Workers whose
  issue became terminal or inactive are stopped.
- Implication: ADR-0004/0005 make Ilmar's execution state durable, which R1 does
  not. That is a deliberate improvement, but it creates a second source of
  truth. Ilmar still needs R1-style reconciliation: an execution whose task
  was closed or unlabelled must move to CANCELLED.
- Recommendation: add a reconciliation step to the M3 coordinator loop and give
  `TaskProvider` a contract test for "task became ineligible".
- Status: open · Affects: ADR-0004, ADR-0005 · Milestone: M3

### F-002: Retries need backoff timing and stall detection

- Evidence: **Emerging** · Sources: [R1]
- Observation: R1 retries failed workers with capped exponential backoff,
  continues normally exited sessions, and kills workers that go silent past a
  stall timeout.
- Implication: Ilmar's RETRY_WAIT has no "not before" time, and nothing
  detects a hung agent apart from the overall agent timeout.
- Recommendation: in M3, add a durable `retry_not_before` to `Execution` (a
  schema change that needs an ADR update) and a heartbeat or stall timeout to
  `AgentRunner`/`ContainerRuntime`.
- Status: open · Affects: ADR-0004 · Milestone: M3

### F-003: Workspace path safety should be part of the contract

- Evidence: **Emerging** · Sources: [R1], [R11]
- Observation: R1 and its ports require a sanitised workspace key per issue,
  paths that stay inside the workspace root, the agent running with the
  workspace as its working directory, and lifecycle hooks with timeouts.
- Implication: `ContainerSpec` mounts one host directory but nothing checks how
  that directory is derived from a task key.
- Recommendation: in M2, derive workspace paths in the core from sanitised keys
  and add path-containment cases to the `ContainerRuntime` and
  `SourceControlProvider` contracts. Hooks are not needed yet: `checkout` and
  `publish_branch` cover the equivalent steps.
- Status: open · Affects: ADR-0006 · Milestone: M2

### F-004: An agent's own judgement of its work is not acceptance evidence

- Evidence: **Established** · Sources: [R2], [R3], [R7], [R9]
- Observation: four independent sources stress external checks. R3 has agents
  mark a feature done only after verifying it. R9 found agents grade their own
  work too generously and added a separate evaluator. R2 runs further agent
  reviews. R7 validates against scenarios.
- Implication: this confirms the mandatory VERIFYING state and the rule that
  `AgentOutcome.COMPLETED` proves nothing.
- Status: accepted · Affects: ADR-0004

### F-005: Verification must use checks the agent cannot edit

- Evidence: **Emerging** (R7 is a single organisation's report; R3 describes a
  similar safeguard) · Sources: [R3], [R7]
- Observation: R7 keeps scenarios outside the codebase the agent sees, as
  holdout sets, because agents can game tests they can read. R3 lets the agent
  change only the pass flag of a feature, not its definition.
- Implication: if VERIFYING runs tests from the agent's own branch, an agent
  can weaken the tests and still pass.
- Recommendation: in M2, VERIFYING should (a) run acceptance checks defined
  outside the workspace, or taken from the base branch, and (b) flag diffs that
  touch tests, CI or verification configuration for human review. R7's
  probabilistic "satisfaction" scoring stays **experimental**: do not adopt it
  before deterministic checks exist.
- Status: open · Affects: ADR-0004, ADR-0006 · Milestone: M2

### F-006: Retries and continuations need a structured handoff

- Evidence: **Emerging** · Sources: [R1], [R3]
- Observation: R3 bridges sessions with a progress file plus git history. R1
  starts continuation runs with the prior attempt's context.
- Implication: an Ilmar retry starts the agent cold. The only memory is
  `last_error`.
- Recommendation: in M3, persist a short attempt summary (agent summary,
  verification output) and pass it to the next attempt's `AgentRequest`.
- Status: open · Affects: ADR-0004 · Milestone: M3

### F-007: Keep AGENTS.md a short map; put detail in linked docs

- Evidence: **Emerging** · Sources: [R2]
- Observation: R2 found that a large instruction file crowds the agent's
  context and works better as an index into structured, catalogued docs.
- Implication: AGENTS.md is the entry point for every agent session.
- Recommendation: keep AGENTS.md around 200 lines, move procedures into
  `docs/`, and consider a size check in `tests/architecture/`.
- Status: open (applied informally in this change) · Affects: ADR-0008

### F-008: Mechanical enforcement of layering works well with agents

- Evidence: **Established** · Sources: [R2]; verified in Ilmar
- Observation: R2 enforces dependency layers with structural tests. Ilmar's
  `tests/architecture/` does the same.
- Status: accepted · Affects: ADR-0002

### F-009: Make verification output legible to the agent

- Evidence: **Emerging** · Sources: [R2]
- Observation: R2 gives agents direct access to logs, metrics and the running
  app for each change, so they can reproduce and fix failures themselves.
- Implication: when VERIFYING fails and the execution retries, the agent should
  see the failing check output, not just "failed".
- Recommendation: combine with F-006 in M3. Full app observability belongs to
  M4 (self-hosting).
- Status: open · Milestone: M3–M4

### F-010: A minimal open-source agent is a credible second AgentRunner

- Evidence: **Emerging** (benchmark scores are self-reported) · Sources: [R6]
- Observation: R6 reaches competitive benchmark results with a bash-only agent
  whose actions are independent subprocess calls, which is easy to run inside
  a container.
- Implication: this fits `AgentRunner.run(runtime, container, request)` as is.
  A second adapter would test the portability of the port and reduce reliance
  on one proprietary agent.
- Recommendation: candidate second agent for M5.
- Status: open · Affects: ADR-0002 · Milestone: M5

### F-011: Pluggable sandbox providers are common practice

- Evidence: **Established** · Sources: [R4], [R5]
- Observation: R4 ships several interchangeable sandbox backends, and R5 has a
  runtime abstraction.
- Implication: this confirms the `ContainerRuntime` port. Remote sandboxes are
  plausible adapters after Podman and Docker.
- Status: accepted · Affects: ADR-0002

### F-012: Prior-art repositories have licence boundaries that matter

- Evidence: **Established** (from the projects' own licence statements) ·
  Sources: [R1], [R4], [R5], [R12]
- Observation: R5's `enterprise/` directory is source-available and needs a
  paid licence. R4's claimed `vendor/` exclusion is unverified. R1, including
  SPEC.md, is Apache-2.0. R12's licence is unknown.
- Recommendation: never consult `enterprise/` code when implementing. Treat R1's
  spec as a reference for behaviour, not text to copy. Verify R4 and R12 before
  looking at their code.
- Status: accepted (policy) · Affects: AGENTS.md §7

### F-013: "No human review" is an upstream trend that Ilmar does not adopt

- Evidence: **Emerging** · Sources: [R2], [R7]
- Observation: R2 moved most review to agents. R7 forbids humans from writing
  or reviewing code.
- Implication: this conflicts with MANIFEST §6, which requires human approval
  for sensitive changes. The manifest wins.
- Recommendation: keep human approval. Revisit the scope of approval (not its
  existence) once verification evidence from F-005 is mature.
- Status: rejected for now · Affects: future approval-policy ADR

### F-014: Strict versus forward-compatible configuration

- Evidence: **Experimental** · Sources: [R1], [R11]
- Observation: R1's workflow configuration ignores unknown keys for forward
  compatibility. Ilmar rejects them to catch typos.
- Recommendation: keep strict. Revisit when configuration files need to work
  across Ilmar versions (add a `config_version`).
- Status: accepted (keep strict) · Affects: ADR-0007

### F-015: Modelling GitHub Issues as a tracker

- Evidence: **Experimental** · Sources: [R11]
- Observation: community R1 ports use exactly one `state:<name>` label per
  issue, or GitHub Projects v2 status fields.
- Recommendation: use as input for the M2 GitHub `TaskProvider` design.
  Prototype before deciding.
- Status: open · Milestone: M2

### F-016: The MANIFEST §9 URL for [R3] appears to be wrong

- Evidence: **Experimental** (secondary only) · Sources: [R3]
- Observation: Anthropic's engineering index lists the article, which suggests
  it lives under `/engineering/` rather than `/research/`.
- Recommendation: a maintainer should verify the URL and correct MANIFEST §9.
  The manifest is a sensitive file, so the agent did not change it.
- Status: open
