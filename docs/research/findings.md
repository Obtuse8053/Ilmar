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

**Status:** `open` (not yet proposed), `proposed (#n)` (an issue exists; it
also says whether the issue is approved), `accepted` (reflected in a merged
change or ADR), `rejected` (with reason) or `superseded`.

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
- Status: proposed (#12, approved) · Affects: ADR-0004, ADR-0005 · Milestone: M3

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
- Status: proposed (#13, approved) · Affects: ADR-0004 · Milestone: M3

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
- Status: proposed (#4, approved) · Affects: ADR-0006 · Milestone: M2

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
- Status: proposed (#9, approved) · Affects: ADR-0004, ADR-0006 · Milestone: M2

### F-006: Retries and continuations need a structured handoff

- Evidence: **Emerging** · Sources: [R1], [R3]
- Observation: R3 bridges sessions with a progress file plus git history. R1
  starts continuation runs with the prior attempt's context.
- Implication: an Ilmar retry starts the agent cold. The only memory is
  `last_error`.
- Recommendation: in M3, persist a short attempt summary (agent summary,
  verification output) and pass it to the next attempt's `AgentRequest`.
- Status: proposed (#14, approved) · Affects: ADR-0004 · Milestone: M3

### F-007: Keep AGENTS.md a short map; put detail in linked docs

- Evidence: **Emerging** · Sources: [R2]
- Observation: R2 found that a large instruction file crowds the agent's
  context and works better as an index into structured, catalogued docs.
- Implication: AGENTS.md is the entry point for every agent session.
- Recommendation: keep AGENTS.md around 200 lines, move procedures into
  `docs/`, and consider a size check in `tests/architecture/`.
- Status: proposed (#30, approved) · Affects: ADR-0008

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
- Status: proposed (#14, approved) · Milestone: M3–M4

### F-010: A minimal open-source agent is a credible second AgentRunner

- Evidence: **Emerging** (benchmark scores are self-reported) · Sources: [R6]
- Observation: R6 reaches competitive benchmark results with a bash-only agent
  whose actions are independent subprocess calls, which is easy to run inside
  a container.
- Implication: this fits `AgentRunner.run(runtime, container, request)` as is.
  A second adapter would test the portability of the port and reduce reliance
  on one proprietary agent.
- Recommendation: candidate second agent for M5.
- Status: proposed (#24, approved) · Affects: ADR-0002 · Milestone: M5

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
- Status: proposed (#6, approved) · Milestone: M2

### F-016: The MANIFEST §9 URL for [R3] appears to be wrong

- Evidence: **Experimental** (secondary only) · Sources: [R3]
- Observation: Anthropic's engineering index lists the article, which suggests
  it lives under `/engineering/` rather than `/research/`.
- Recommendation: a maintainer should verify the URL and correct MANIFEST §9.
  The manifest is a sensitive file, so the agent did not change it.
- Status: proposed (#27, approved)

### F-017: Running git on an agent-written repository can execute code on the host

- Evidence: **Established** · Sources: [R17]
- Observation: Git trusts repository-local configuration and hooks. Settings
  such as `core.fsmonitor`, `core.hooksPath`, pagers and diff textconv drivers
  run commands during ordinary operations such as `status`, `diff` and
  `commit`. Git maintainers describe this as outside Git's security boundary.
- Implication: if the coordinator runs git in the agent's workspace, a
  prompt-injected agent can run code on the host, where the credentials are.
- Recommendation: the host never uses the agent's `.git`. It freezes the
  working tree as plain content into a coordinator-owned repository, using
  hardened git invocations.
- Status: accepted (ADR-0010; implemented by #3, #7) · Affects: ADR-0006,
  ADR-0010

### F-018: Claude Code needs only a small, documented egress set when preinstalled

- Evidence: **Established** (vendor's primary documentation) · Sources:
  [R13], [R15]
- Observation: model calls, feature flags and the WebFetch preflight go to
  `api.anthropic.com`. Login, installer, plugin and telemetry hosts are needed
  only for those features, and non-essential traffic can be switched off.
  `HTTPS_PROXY` and `NO_PROXY` are honoured; SOCKS is not.
- Implication: a preinstalled, API-key-authenticated agent behind an HTTPS
  proxy that allows only the model endpoint should work. The documented list
  changes over time.
- Recommendation: allow only the model endpoint, disable non-essential
  traffic, and record the hosts actually contacted in an opt-in integration
  test (#8).
- Status: accepted (ADR-0011; verified by #8) · Affects: ADR-0011

### F-019: An LLM gateway can keep the real model credential out of the sandbox

- Evidence: **Emerging** (the gateway capability is documented in [R14];
  using it to isolate credentials is our own inference) · Sources: [R14]
- Observation: Claude Code can send all model traffic to an operator-run
  gateway, authenticated with a gateway-issued token.
- Implication: a host-side gateway could issue per-execution, budget-limited,
  revocable tokens. That would remove ADR-0006's model-key exception and
  provide cost metering.
- Recommendation: evaluate with execution budgets in M3. Not needed for M2.
- Status: open (criterion added to #18) · Affects: ADR-0006, ADR-0011

### F-020: Enforce egress outside the sandbox rather than with an in-container firewall

- Evidence: **Emerging** · Sources: [R16], [R18]
- Observation: the reference devcontainer enforces default-deny egress with
  iptables inside the container, which needs `NET_ADMIN`/`NET_RAW`. Container
  runtimes also offer internal networks with no external route.
- Implication: an internal network plus a host-side filtering proxy gives the
  same allow-list with no network capabilities in the sandbox.
- Status: accepted (ADR-0011; implemented by #5) · Affects: ADR-0011

### F-021: Agent CLI exit status may not reflect failure

- Evidence: **Experimental** (one secondary report, from an older version) ·
  Sources: [R15], [R19]
- Observation: one report found non-interactive Claude Code exiting with 0 on
  errors. The documented JSON output carries a result type and an error flag.
- Implication: `AgentRunner` must derive `AgentOutcome` from the structured
  result as well as the exit status. Either way the outcome is only advisory
  (F-004).
- Recommendation: cover this in the #8 contract and integration tests.
- Status: open (criterion added to #8) · Affects: ADR-0010

### F-022: Accept and publish one immutable artefact

- Evidence: **Established** (long-standing build-and-release practice: build
  once, then promote the same artefact) · Sources: none external
- Observation: verifying one thing and shipping another, such as a rebuilt or
  re-committed working copy, voids the verification.
- Implication: M1's `publish_branch` commits at publish time, so verification
  and publication are not bound to the same bytes.
- Recommendation: bind verification and publication to a commit id (ADR-0010).
- Status: accepted (ADR-0010; implemented by #3, #7, #9) · Affects:
  ADR-0004, ADR-0010

### F-023: Rootless Podman trades workspace ownership against escape containment

- Evidence: **Established** for the mechanism (primary Podman documentation,
  [R21], and observed in CI on Podman 4.9); the trade-off is our assessment ·
  Sources: [R21]
- Observation: rootless Podman maps container root to the invoking user. A
  non-root container user is a sub-UID on the host unless `keep-id` maps the
  invoking user onto it. Without `keep-id`, the host account cannot manage the
  files the sandbox writes; with it, a full escape lands as that account.
- Implication: an orchestrator that reads and deletes sandbox output needs
  `keep-id` or idmapped mounts. Idmapped mounts would combine both properties
  but are not dependable in rootless Podman 4.9.
- Status: accepted (ADR-0014; implemented by #5) · Affects: ADR-0011, ADR-0014

### F-024: `podman exec` cannot bound a command's lifetime

- Evidence: **Established** (primary Podman documentation lists no exec
  timeout or exec-session kill; [R21]) · Sources: [R21]
- Observation: killing the `podman exec` client leaves the process inside the
  container running. Exit status 125 can come from Podman or from the command.
- Implication: a runtime that must guarantee nothing keeps writing after a
  timeout has to stop the whole container.
- Status: accepted (ADR-0014; port contract changed in #5) · Affects:
  ADR-0014, the `ContainerRuntime` port

