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
| 1a | #33 M2 design review (ADR-0010, ADR-0011, ADR-0012) | #2 |
| 2 | #3 Contract suites and fakes for `SourceControlProvider` and `AgentRunner` | #33 |
| 3 | #4 Safe workspace path derivation | #3 |
| 4 | #6 GitHub `TaskProvider` | #3 |
| 5 | #5 Podman `ContainerRuntime` and sandbox profile (network `NONE`) | #4 |
| 5a | #40 Sandbox egress allow-list and the ADR-0011 §9 suite | #5 |
| 6 | #7 GitHub `SourceControlProvider` | #3, #4 |
| 7 | #8 Claude Code `AgentRunner` | #3, #40 |
| 8 | #9 Verification stage | #5, #7 |
| 9 | #10 Adapter registry | before #11 |
| 10 | #11 Coordinator loop and end-to-end test (closes M2), in four PRs: | all of the above |
| 10a | #11a Instance lock, fencing, operation journal, resource ledger (ADR-0020) | #10 |
| 10b | #11b One execution end to end with write-ahead ordering; `ilmar run --once --config PATH` | 10a |
| 10c | #11c Crash recovery for every state; `load_candidate`; crash-injection harness | 10b |
| 10d | #11d Opt-in live test on a disposable repository; M2 close-out | 10c, #44 |

Steps 3 and 4 can run in parallel. Maintenance issues without a milestone can
fill any gaps between M2 steps.

## Recommended next task

#11c (plan step 10c): crash recovery for every non-terminal state.
- Reconcile unfinished operations by kind (ADR-0020, ADR-0021).
- Add `SourceControlProvider.load_candidate`, checked against the freeze's
  digest.
- Clean up resources recorded as `INTENDED` or `ACTIVE`.
- Lift ADR-0021's "unfinished operations block new work" rule for what it
  reconciles.

Only after #11b (ADR-0021) is merged. **Waiting for the maintainer's
explicit go-ahead.**

## Change log

- 2026-11-05: #11b finished: one coordinator execution end to end
  (ADR-0021, proposed).
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

  Next recommended: #11c.

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
