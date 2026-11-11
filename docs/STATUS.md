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

M3 now ends at Gate 1 of the autonomy roadmap (`autonomy-roadmap.md`,
approved 2026-11-10): Ilmar running continuously under supervision, within
MANIFEST §10's M3 scope (restart recovery, retries, cancellation, logs and
execution limits). This plan is owned here; issues do not repeat it. Skip any
step whose issue GitHub shows as finished.

| Step | Issue | Depends on |
| --- | --- | --- |
| 1 | #87 Stray claim label sweep (ADR-0027) | — |
| 2 | #89 ADRs 0028 to 0031: time-bounded scheduling, Mode 1 limits and budget, operating modes, protected safety controls | — |
| 3 | #13 Retry backoff, stall and wall-clock kill | #89 |
| 4 | #90 Execution limits and the single model-credential path | #89 |
| 5 | #18 Reservation ledger, suspension and kill switch | #13, #90 |
| 6 | #91 Loop mode with explicit opt-in and gate records | #18 |
| 7 | #92 Protected safety controls | #89 |
| 8 | #59 Human-controlled resolution of quarantined resources | — |
| 9 | #93 Gate 1: supervised live run | all of the above |

Critical path: #89, #13, #18, #91. Steps 1, 4, 7 and 8 run alongside it.

Why this order:
- The ADRs come first, because the time rule (ADR-0028) is needed by
  backoff and by budget days, and every later step implements one of the
  four ADRs.
- The ledger comes after backoff and limits, because it charges each
  attempt that those steps create and bound.
- Loop mode comes last, because it is the first caller that runs long
  enough to need all of them.
- Gate 1 runs on a dedicated test repository.

Unattended operation stays disabled until Gate 1 passes and the
maintainer records it. M4 order (Modes 2 and 3) is in
`autonomy-roadmap.md`.

## Recommended next task

#13, second part: stall detection (ADR-0035). Backoff and durable
scheduling are merged (#103).

## Change log

- 2026-11-10: **#13 stall detection submitted** (ADR-0035). #13 stays open
  until it is audited.
  - **Stream:** Claude Code runs with `--output-format stream-json --verbose`.
    Every event is validated incrementally, with bounded memory and no stdout
    kept or persisted. The first violation ends the run, which fails without
    retry.
  - **Stall:** `[limits] stall_timeout_seconds` (default 900, at most
    `agent_timeout_seconds`) ends a run with no validated agent event for
    that long. It is retried after backoff with code `agent-stalled`.
  - **Port:** `ContainerRuntime.run` gains a stdout consumer and an idle
    timeout; Podman and the fakes pass the new contract tests.
  - **Gate:** no mode above manual until a pinned-version live run records the
    event types (the live test writes them, marked `recorded`). The checked-in
    `2.1.302-synthetic.json` is not evidence. A live run under
    `ILMAR_LIVE_REQUIRED=1` fails without an exact `ILMAR_TEST_CLAUDE_VERSION`.
    Enforcing `expected_version` belongs to #91.
  - **Not yet shown:** that a container stops by itself when the coordinator
    is SIGKILLed (ADR-0035 "Crashes"). The crash test in this PR is simulated.
    The real-Podman SIGKILL test needs the runtime-side `--timeout` from #90
    and is a blocking #90 and Gate 1 (#93) requirement. It has not been run.
- 2026-11-10: **#13 backoff merged** (#103). **ADR-0035 accepted** (#104):
  stall detection through stream events. Corrections at acceptance:
  - the post-result `rate_limit_event` is cited from upstream issue R28;
  - the stream-json runner may not run above manual until a pinned-version
    live test is recorded.

  Stall detection is next; #13 stays open until it is implemented and
  audited.

- 2026-11-10: **#13 retry backoff and durable scheduling submitted** (schema 7).
  - **Backoff:** capped exponential, configured with
    `[limits] retry_backoff_base_seconds` and `retry_backoff_max_seconds`.
  - **Durable:** `retry_not_before` is stored with the move into RETRY_WAIT;
    the state machine refuses an earlier attempt, and a restart never shortens
    the wait.
  - **Clock:** a wall clock behind the latest persisted time waits
    (ADR-0028 §3). This holds for every admission of new work: a new execution
    and the start of any attempt. The store also refuses such a commit
    (`ClockBehindError`).
  - **Not in this PR:** stall detection, which needs a decision on how
    progress is observed (ADR-0017).

- 2026-11-10: **ADR-0028 to ADR-0031 accepted** (#100); #87's
  implementation merged (#88). At acceptance, ADR-0031 gained one
  requirement: `ilmar blocked release` records a human release of the FAILED
  execution, so the task becomes eligible again (tested in #92).

- 2026-11-10: **Autonomy roadmap approved** (`autonomy-roadmap.md`).
  M3 ends at Gate 1 (supervised autonomy); M4 holds Modes 2 and 3.
  - **New issues:** #89 to #98.
  - **#18** re-scoped to the reservation ledger, suspension and kill switch;
    its gateway item moved to #94.
  - **#14 and #51** moved out of M3.
  - **#85** merged (ADR-0027 accepted).

- 2026-11-10: **#12 closed** after its acceptance audit against ADR-0026.
  **ADR-0027 implementation submitted** (#87):
  - **Port:** `stray_labels` and `clear_label`.
  - **Sweep:** the instance-scoped `clear-stray-label` (intent in one pass,
    effect in a later one, after a store revalidation).
  - **Store:** schema 6.
  - **Tests:** every test ADR-0027 requires, with two new crash-suite
    scenarios.

- 2026-11-10: **ADR-0027 accepted** (maintainer approval at `8ba90a9`, merged
  in #84 as `19f76b3`). Its implementation starts in a separate pull request.

- 2026-11-10: **ADR-0027 proposed** (documentation only): a sweep for
  Ilmar's claim label left with no claim behind it, and two clarifications to
  ADR-0026 (no tracker read while a cancellation is pending; deleted and
  transferred tasks). It follows the #12 implementation (#83) as a separate
  change and is not implemented before approval.
- 2026-11-10: **#12 implementation merged** in #83 (merge commit
  `3f7c682`, approved head `a5cf89c`, all five checks green). #12 stays
  open until its acceptance audit.
- 2026-11-09: **#12 implementation submitted** (ADR-0026), stacked on #82:
  - **Store and journal:** schema 5 makes an operation's execution optional
    for the one instance-scoped kind, `release-orphan-claim`; the journal
    performs it fenced by the instance lock (`perform_instance`).
  - **Port and GitHub adapter:** `standing`, `held_claims`, `release_claim`,
    `restore_label`; `claim` returns its claim id; only authentic own claims
    (marker and author) count; `bot_login` for tokens that cannot read
    `GET /user`; a transfer is never followed.
  - **Coordinator:** standing reads at the pass start and the safe points;
    tracker decisions through ADR-0025's machinery with their own reason
    codes; lost claims confirmed only across three ordered passes; label
    restore; orphan release before the follow-up and the pick; re-pick rules.
  - **Tests:** contract, adversarial adapter, coordinator, crash suite (six
    new scenarios with I13) and a real-Podman scenario.

- 2026-11-09: **ADR-0026 accepted** (tracker reconciliation, for #12),
  merged in #81 after three review rounds:
  - **Orphan release:** acts on exactly the observed claim object.
  - **Lost-claim confirmation:** durable, fenced, and decided in three
    ordered passes with no time-based rule (ADR-0022 §Time).
  - **Label repair:** a targeted restore that never claims and never decides.

  #12 implementation started.

- 2026-11-09: **#16 finished** (cancellation, ADR-0025 with the #76
  clarification).
  - **Implementation:** #74 (inbox, CLI, durable events), #75 (inbox read
    race), #77 (stop signal in `ContainerRuntime` and `AgentRunner`), #78
    (watcher, coordinator integration, crash recovery, I12, stranded
    requests, the full ending on every exception path). #79 completed the
    tests ADR-0025 requires.
  - **Final acceptance audit:** every criterion passes; the evidence matrix
    is
    [on #16](https://github.com/Obtuse8053/Ilmar/issues/16#issuecomment-6748276550).
    #16 was closed after #79 merged
    ([closing comment](https://github.com/Obtuse8053/Ilmar/issues/16#issuecomment-6749515510)).
  - **Verified on `82c7f67`** (the merge of #79, so the whole of #16 is on
    `main`):
    - CI green, including real Podman: 106 passed, 1 skipped by design;
    - 2385 tests pass on Python 3.11, 3.12 and 3.13.
  - **Use:** `ilmar cancel` (`docs/operations/cancel.md`). Unattended
    operation stays disabled.
  - **Next:** #12, starting with its ADR.

- 2026-11-09: **#17 finished** (structured event logging):
  - **Implementation:** PRs #64, #65, #66, #68 and #71, plus the ADR-0024
    clarification, #67.
  - **Final acceptance audit:** all 12 criteria pass; the evidence is
    [on #17](https://github.com/Obtuse8053/Ilmar/issues/17#issuecomment-6735109957).
    Verified on `09fe56d`: CI green, including real Podman; 2063 tests on
    Python 3.11, 3.12 and 3.13.
  - **Deferred:** the optional diagnostic stream, to #70.
  - **Next:** #16, starting with ADR-0025, now proposed.

- 2026-11-09: #17 completion PR, for the close-out audit's three gaps:
  - a merged or closed change request is recorded as the observation on the
    events of the claim release it decides, in the same commits;
  - `ilmar run --once` shows each stable reason code beside the readable
    detail, read back from the durable events;
  - the optional diagnostic stream is deferred to #70.

  #17 closes after the final acceptance audit.

- 2026-11-09: **#17 implementation merged; close-out found three gaps,** so
  #17 stays open.
  - **The four parts:**
    - #64: the domain, with the schema, reason codes, derivation and the I11
      fold;
    - #65: the SQLite event store, with events in the same fenced
      transaction as every write, the lock-held migration with its baseline,
      and the read-only open;
    - #66: the coordinator's event context (pass, durations, error classes,
      codes, reconciliation observations), with a fail-closed reason-code
      boundary (ADR-0024 clarification, #67);
    - #68: `ilmar events` and `ilmar events verify`
      (`docs/operations/events.md`).
  - **Evidence:**
    - on `053da9d`, CI is green, including real Podman;
    - pytest passes on Python 3.11, 3.12 and 3.13: 2048 tests;
    - I11 is checked after every coordinator pass in every test and at
      every crash point, including recovery of stores from the previous
      schema.
  - **Gaps, in the [audit](https://github.com/Obtuse8053/Ilmar/issues/17#issuecomment-6734099160):**
    - the change-request observation is not in the events;
    - the reason code is not in the `run` output;
    - the diagnostic stream needs a decision.

- 2026-11-08: **#15 finished** (restart recovery): audited against ADR-0012,
  ADR-0020, ADR-0022, the code and the tests. The evidence matrix is on #15.
  - Recovery, fencing and ownership were already complete from #11c. No
    production code changed.
  - PR #60 added tests:
    - a container swapped for a stranger's right after its labels were read
      is untouched;
    - a failed proxy or network removal stays quarantined and blocked across
      restarts, each with a fresh runtime, and is never retried
      (ADR-0020);
    - every non-terminal state has a crash point.
  - Maintainer decisions:
    - quarantine stays final for automation; human resolution is #59 (new,
      approved);
    - orphaned-claim release moved to #12, with an ADR for the
      `TaskProvider` extension;
    - the stable `resource-ownership-conflict` reason code moved to #17.
  - Next recommended: #17.

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
