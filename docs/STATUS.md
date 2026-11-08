# Status

The resume point for any session (ADR-0009). This file holds only what GitHub
cannot: the planned order of work, the recommended next task, and a history of
changes. **Issue state** (open or closed, priority, labels, who is working on
what) **lives only on GitHub.** If this file and GitHub disagree about an issue,
GitHub is right; fix this file in your next PR. See `docs/workflow.md`,
"Sources of truth".

## Current milestone

**M3 Reliability**
([milestone](https://github.com/Obtuse8053/Ilmar/milestone/3)). M2 ended on
2026-11-08, when the live end-to-end tests passed (ADR-0023, accepted).

## Live state (query, don't copy)

- Work in progress:
  [open PRs](https://github.com/Obtuse8053/Ilmar/pulls) and
  [`status:in-progress` issues](https://github.com/Obtuse8053/Ilmar/issues?q=is%3Aopen+label%3Astatus%3Ain-progress)
- Waiting on the maintainer:
  [`needs:maintainer` issues](https://github.com/Obtuse8053/Ilmar/issues?q=is%3Aopen+label%3Aneeds%3Amaintainer)
- Milestone progress: the milestone page linked above

## Planned order (M3)

Dependency order for M3 (MANIFEST §10: restart recovery, duplicate-work
prevention, retries, cancellation, structured logs and execution limits).
This plan is owned here; issues do not repeat it. Skip any step whose issue
GitHub shows as finished.

| Step | Issue | Depends on |
| --- | --- | --- |
| 1 | #15 Restart recovery: check against ADR-0022 and the #11c tests; close it as delivered, or narrow it to what is missing | — |
| 2 | #17 Structured event logging (ADR for the event schema) | — |
| 3 | #16 Cancellation of running executions (ownership-verified, ADR-0012 rule 3) | #17 |
| 4 | #12 Reconcile executions against tracker state every cycle | #16 |
| 5 | #13 Retry backoff and stall detection (first schema migration) | #17 |
| 6 | #18 Execution limits and budgets, including the F-019 gateway evaluation | #13, #16 |
| 7 | #14 Attempt handoff and legible verification output | #13 |
| 8 | #51 Stateful GitHub emulator over real git, with fault injection | can run in parallel; before #12 if possible |

Why this order:
- Events come first, because cancellation, stall detection and budgets all
  need to say what they did.
- Cancellation comes before reconciliation, because a task that becomes
  ineligible while running has to be cancelled.
- Limits come last among the core items, because they build on backoff and
  cancellation.

Unattended operation stays disabled throughout M3. Enabling it is #20 (M4),
which revisits ADR-0023.

## Recommended next task

#15 (plan step 1): a short audit, not new code. ADR-0022 and the #11c
crash-injection tests already define and test recovery for every
non-terminal state.
- **Close #15** if its criteria (including the ADR-0012 per-state table)
  are met, linking the evidence.
- **Otherwise narrow #15** to what is missing.

Then #17.

**Waiting for the maintainer's explicit go-ahead.**

## Change log

- 2026-11-08: **M2 complete.** The live workflow passed in full
  ([run 42465684121](https://github.com/Obtuse8053/Ilmar/actions/runs/42465684121)
  at `f9078dc`). ADR-0023 is accepted.
  - **#44 passed:** Claude Code 2.1.318 contacted only `api.anthropic.com`,
    and nothing was denied.
  - **#11d passed:**
    - the preflight passed all 12 checks;
    - one task ran end to end in one attempt;
    - in the crash drill, killed mid-agent, the run recovered with exactly
      one pull request.
  - **Records:** on #44 and #11. The research access log has the #44 result
    under R13.
  - **How the live tests run** (PRs #54 and #55): in GitHub Actions, through
    a manual workflow.
    - Supply chain: the wheels are hash-locked and the agent base image is
      pinned by digest.
    - Before any secret, it checks that exactly `expected_sha` is checked
      out and that `main` still points at it. Nothing is installed after
      the secrets.
    - Approval: the guard requires required reviewers with Prevent
      self-review on. The approver must be an account no automation can
      use.
  - **Fixes from the first two runs:**
    - PR #56: the seeded issue is proven labelled and listed, and the
      preflight proves Issues write.
    - PR #57: log followers close their pipes, and the workspace check
      follows ADR-0021.
  - Unattended operation stays disabled. Next recommended: #15 (audit),
    then #17.

- 2026-11-05: #11d harness, merged in PR #50 (after an adversarial review):
  live end-to-end tests and the #44 record (ADR-0023, then Proposed).
  - Run locally with two commands: `scripts/live_setup.py` once, then
    `scripts/live_run.py` (`docs/operations/live-testing.md`).
  - Found and fixed:
    - Podman records a `podman exec`'s environment in its state database on
      disk, so every agent run had written the model key there. Per-command
      values now go through stdin to an in-container loader.
    - A killed coordinator left the key in a temporary env file. Container
      environments now go through a memfd.
    - Both are verified on real Podman in CI.
  - Unattended operation is explicitly refused.

  #11 stays open until the live tests have passed.

- 2026-11-05: #11c finished: crash recovery (ADR-0022, accepted after an
  adversarial review on PR #49).
  - Every worker step is decided from the journal, the ledger and trusted
    external observations, so recovery and normal running are one path.
  - Recovery runs before new work. Each execution has its own boundary:
    one that cannot be proven safe, or hits an unexpected error, is blocked
    alone; others continue.
  - New port methods: `ContainerRuntime.remove_owned` and
    `SourceControlProvider.load_candidate`. Evidence format 2 records its
    verification run; format-1 digests are unchanged.
  - An interrupted agent run gets a new sandbox and a new attempt. The
    agent never runs after a freeze. A moved branch blocks and is never
    overwritten. A fenced-out process stops.
  - Crash injection at every store write and before every external effect,
    and repeated crashes, on six paths. Real-Podman recovery tests in CI.
  - The review added:
    - recorded security and policy failures are never retried after a
      crash;
    - interrupted provisioning stays transient through a second crash;
    - restart-safe, non-decreasing timestamps;
    - publishing requires evidence for the policy in force;
    - Podman networks are removed by inspected ID;
    - `reconcile` checks the lease;
    - candidate parents are read from the raw commit;
    - the architecture test derives the mutating port methods from the
      ports.

  #11 stays open for #11d. Next recommended: #11d.

- 2026-11-05: #11b finished: one coordinator execution end to end
  (ADR-0021, accepted after an adversarial review on PR #48).
  - `ilmar run --once --config PATH`.
  - The `Coordinator`, `ExecutionWorker` and `Effects` (the only core code
    that changes the outside world; architecture-tested).
  - Every step is journaled and fenced, and the published revision is
    exactly the verified frozen candidate.
  - Transient failures only are retried, with at most 3 attempts.
  - The claim is kept while the change request is open, and released after
    a merge or close (task completion) or on terminal failure.
  - Successful workspaces are deleted; failed ones are kept.
  - Exit codes 0, 1, 2, 3.
  - New read-only port method `change_request_state`; a `run-agent`
    operation kind; the Verifier writes evidence fenced.
  - Real-Podman end-to-end test in CI.
  - The review added:
    - security, policy and containment failures are never retried;
    - failed tasks are never re-run automatically;
    - late-stage failures re-verify or re-push the same frozen candidate
      instead of re-running the agent, and block after three tries;
    - branch-head checks around the change request;
    - interrupted releases are resumed;
    - only non-repeatable unfinished operations block new work.

  #11 stays open for #11c and #11d. Next recommended: #11c.

- 2026-11-05: #11a finished: fencing, the write-ahead operation journal and
  the resource ledger (ADR-0020, accepted after an adversarial review on PR
  #47). #11 is split into #11a–#11d.
  - An exclusive instance lock plus a store fencing epoch: a superseded
    coordinator can write nothing.
  - Every side effect gets a durable intent, an ownership re-check right
    before it, and a recorded outcome. A deterministic idempotency key per
    step; per-kind reconciliation rules.
  - A resource ledger from intent to release or quarantine.
  - A candidate can be recorded only with its freeze and changed-paths
    digest.
  - Store schema v3, with triggers.
  - `ExecutionStore` port: new methods and an optional `fence` keyword.
  - The review added:
    - per-execution leases, so executions can run in parallel under one
      coordinator;
    - stale-view checks right before every effect;
    - candidate integrity on every path (no direct, swapped, re-based or
      reconciled candidates);
    - results kept when a follow-up is refused;
    - `synchronous = FULL`.

  #11 stays open for #11b–#11d. Next recommended: #11b.

- 2026-11-05: #10 finished: adapter registry (ADR-0019, accepted after an
  adversarial review on PR #46).
  `(port, kind)` maps to lazily imported parsers. `plan()` validates every
  adapter's options and the rules between ports without secrets or side
  effects (`ilmar config validate`, plus `--check-environment`). `build()`
  creates `Components`, which owns cleanup, including after a partial
  failure. Secrets are redacted from every error. Nothing that weakens the
  sandbox is configurable, the agent sandbox's hosts come from the agent
  adapter, and `[verification]` is required. Configuration: `[ilmar]
  instance_id`; the agent image moved to `[adapters.agent_runner]`.
  The review added four rules. Every creation error is redacted. The model
  credential may not share a variable or a value with tracker or SCM
  credentials. Host executables come only from absolute `PATH` entries and
  never from `workspaces/`. The configuration file must not be writable by
  others. Ports are now checked by signature, not just member names. #11
  must take an explicit configuration path. Next recommended: #11.

- 2026-11-05: #9 finished: verification stage (ADR-0018, accepted after an
  adversarial review on PR #45). The policy lives in `[verification]` of
  `ilmar.toml`; its path classes can only be extended. Independent path
  checks run on the diff, then candidate-executed checks run per **job**:
  each job gets its own checkout and `NONE` sandbox, and a job's commands
  share them, as CI steps do. Checks are judged only by the
  harness-measured exit status. A candidate that kills its sandbox is
  rejected with evidence; sandboxed output is bounded. #11 and #12/#15 must
  pass `changed_paths` from `freeze_candidate` (or recompute them), never
  rebuild a candidate with an empty path list. The outcome table follows ADR-0010 §5.
  Evidence records are append-only and digest-checked in store schema v2 (the
  first migration), and executions carry their base and candidate revisions.
  Change requests can be labelled for review. Next recommended: #10.

- 2026-11-04: #8 finished: Claude Code `AgentRunner` (ADR-0017, accepted).
  It uses documented print mode with a JSON result. The model credential is
  passed only in the agent command's environment and redacted from
  summaries. `COMPLETED` needs a clean exit, a parsed result and
  `is_error == false`. There is an optional version pin and an operator-built
  image recipe. The contract runs on the in-memory runtime and on real Podman;
  an opt-in live test records the hosts contacted. It must pass before any
  unattended operation (#44, blocks #20). Next recommended: #9.

- 2026-11-04: #7 finished: GitHub `SourceControlProvider` on plumbing-only,
  hardened host git (ADR-0016, accepted). Workspaces are read as data into
  `fast-import` and written back from `ls-tree`/`cat-file`. The token is a
  scoped header passed through the environment only. Publishing uses a lease,
  is fast-forward only and confirms the remote head; CI-configuration changes
  are refused. Pull requests carry a marker. The full contract runs against
  real git. Next recommended: #8.

- 2026-11-04: #40 finished (closes #5 together with PR #39):
  `NetworkPolicy.ALLOWLIST` via a per-execution proxy sidecar on an owned
  `--internal` network (ADR-0015, accepted; amends where ADR-0011's proxy
  runs). The standard-library proxy allows `CONNECT <listed-host>:443` only.
  The full ADR-0011 §9 suite runs on real Podman in CI with nonce controls
  (63 of 63). ICMP sockets are now explicitly disabled as defence in depth.
  Next recommended: #7.

- 2026-11-04: #5 part 1 merged (PR #39); #5 stays open until #40 completes
  the remaining sandbox requirements. Rootless Podman `ContainerRuntime` with
  the ADR-0011 profile and network `NONE`. ADR-0014 (accepted) records the
  Podman mapping: a `keep-id` non-root user, the runtime enforcing the
  recorded workspace identity before and after start, and a timed-out command
  removing its container (a port contract change). The contract, profile and
  network `NONE` checks run against real Podman in a new CI job.
  `UNRESTRICTED` needs a deployment opt-in; an unconfirmed removal raises
  `ContainmentError` and quarantines the workspace (restart-safe quarantine is
  part of #12 and #15). The allow-list proxy and the full §9 suite were split
  into #40. Next recommended: #40.

- 2026-11-04: #6 finished: GitHub `TaskProvider` on a standard-library REST
  client (REST only; token kept to the API origin, including on redirects).
  ADR-0013 (accepted) fixes the M2 threat model: one instance per repository
  and one coordinator per instance. Labels are workflow state, assignees mean
  human responsibility, and claim comments (instance plus unique token) are an
  audit record, not a lock. `ilmar:claimed` is owned by Ilmar; human claim
  labels such as `status:in-progress` are respected and never touched. A second instance is detected and both back off.
  Recovery is reconciled through the durable store (#12, #15). `integration`
  test marker introduced. Next recommended: #5.

- 2026-11-04: #4 finished: deterministic, collision-resistant names
  (branch, container); host layout with separate mountable and
  coordinator-only roots; symlink-safe directory creation, verification and
  descriptor-bound deletion; an `authorize_mount` gate before every mount; containment cases in the ContainerRuntime and
  SourceControlProvider contracts. Next recommended: #6.

- 2026-11-04: #3 finished: contract suites for all five ports, with
  ADR-0010/0012 port changes (immutable candidate revisions, expected-head
  leases with fast-forward-only publishing, owner-checked containers,
  structured agent outcomes). Next
  recommended: #4.

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
