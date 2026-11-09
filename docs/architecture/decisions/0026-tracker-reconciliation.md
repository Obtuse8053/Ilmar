# ADR-0026: Tracker reconciliation: task standing, orphaned claims and tracker-driven cancellation

- Status: Proposed
- Date: 2026-11-09
- Sources: [R1], [R11], [R20] · Findings: F-001
- Amends (by addition only; no accepted text is edited):
  - ADR-0013: own-claim authenticity, and the reconciliation it defers to #12;
  - ADR-0020: one instance-scoped journal operation;
  - ADR-0024: new reason codes and observations;
  - ADR-0025: a second, non-human source of cancellation.

## Context and problem

ADR-0004 and ADR-0005 make execution state durable, and the tracker stays
the authority on *which work is wanted*. F-001 records the gap: R1 re-reads
the tracker on every tick and stops work whose issue became inactive. Ilmar
reads the tracker only when it **picks** a task and when it **recovers** an
execution (`TaskProvider.get`). And `get` returns the task's text and
labels, not whether the task is still wanted or still held by this instance.

Five problems follow (#12):

1. **Ineligibility is ignored.** A human closes the issue, removes
   `ilmar:ready`, adds an excluded label such as `status:blocked`, assigns
   someone, or adds a human claim label. The execution keeps going, and can
   even publish.
2. **A vanished task blocks forever.** `require_task` raises "the task no
   longer exists", and the execution is BLOCKED on every pass.
3. **Orphaned claims stay.** A claim is orphaned when no execution record
   backs it: the state directory was lost or restored from an older copy,
   or the store was replaced. Such a claim keeps the issue out of
   everyone's queue (ADR-0013, "Consequences"). The #15 audit found this
   reconciliation unimplemented.
4. **A missing `ilmar:claimed` label is never re-applied** for a backed
   claim (ADR-0013, "Decision").
5. **Anyone can forge a claim marker.** On a public repository, anyone can
   post a comment containing `ilmar:claim instance=<ours>`. Today the
   adapter would treat it as Ilmar's own. Once reconciliation *releases*
   own claims, that would let anyone make Ilmar delete their comment, or
   treat an issue as held. ADR-0013's "Revisit when" anticipated this.

This ADR also decides how a tracker-driven cancellation relates to ADR-0025's
human cancellation, and how all of it survives a crash.

## Considered options

**Where the coordinator reads the task's standing:**
1. *Only at the start of each pass.* Rejected. One pass drives an
   execution from claim to publish, so an issue closed mid-pass would still
   be published.
2. **At the start of each pass and before each costly or irreversible
   step:** a new attempt, the agent run, the freeze, a verification run and
   the first push. Chosen.
3. *Also during the agent run, in ADR-0025's watcher.* Rejected for now. It
   would make the watcher call a port method, which ADR-0025 and its
   architecture test forbid. The run is bounded by the agent timeout, and its
   output is never frozen (Revisit when).

**How the port exposes it:**
1. *Make `get` richer.* Rejected. `get` feeds the agent's instructions, and
   mixing that payload with control decisions blurs a security boundary:
   task text is untrusted input.
2. **A new read-only `standing(ref)` with a closed set of outcomes.** Chosen.
3. *Let the core interpret labels and comments.* Rejected. They are
   provider-specific, and ADR-0002 keeps them in the adapter.

**How the decision takes effect:**
1. *The coordinator writes an inbox request.* Rejected. The inbox is
   human-only, and only the CLI writes it (ADR-0025, architecture test).
2. *A new terminal path beside cancellation.* Rejected. It would duplicate
   ADR-0025's safe points, ending, I12 and crash rules.
3. **Record an accepted cancellation with the coordinator as actor, and end
   it through ADR-0025's machinery.** Chosen.

**How to find orphaned claims:**
1. *The search API.* Rejected: it is eventually consistent, more strictly
   rate-limited, and indexes comments with delay.
2. **List the issues carrying `claimed_label`, then read their comments.**
   Chosen. Comment-only claims without the label are a documented limit
   (see Consequences).

**How to journal an orphan release (it has no execution):**
1. *Not journaled.* Rejected. It is an external side effect (ADR-0012,
   ADR-0020), and the side-effect architecture test requires the journal.
2. *Under a synthetic execution id.* Rejected. It would pollute the ledger,
   break I11's execution fold, and could collide with real ids.
3. **One instance-scoped operation kind.** Chosen.

**Debouncing ineligibility:**
1. *Require every ineligibility to be seen on two passes.* Rejected. It adds
   latency to deliberate human acts, and the risk it guards against is only
   real for one case.
2. **One observation decides, except for a lost claim, which needs two
   observations in two different passes.** Chosen, because a lost claim
   can be read-after-write lag on Ilmar's own write.

## Decision

### 1. Task standing: a closed set of outcomes, read-only

`TaskProvider.standing(ref) -> TaskStanding`. It reads the tracker and never
writes. The result is one of:

| Standing | Meaning | Reason code |
| --- | --- | --- |
| `HELD` | Open, still eligible-labelled, not excluded, no other claim, and **this instance's own claim is present**. `label_missing=True` when the claim comment is there but `claimed_label` is not. | — |
| `CLAIM_LOST` | Open and still wanted, but this instance's claim is gone, for example its comment was deleted. | `task-claim-lost` |
| `CLAIMED_BY_OTHER` | An assignee, a human claim label, or another instance's claim is present (ADR-0013). | `task-claimed-by-human`, `task-claimed-by-other-instance` |
| `INELIGIBLE` | Closed, the eligible label removed, or an excluded label added. | `task-closed`, `task-unlabelled`, `task-excluded` |
| `GONE` | The task **positively** no longer exists here: deleted, or transferred to another repository. | `task-gone` |

**Rules:**
- **Precedence of reasons.** When several apply, the most decisive one is
  reported, in this order: `GONE`, then `task-closed`, then
  `CLAIMED_BY_OTHER`, then the label reasons, then `CLAIM_LOST`.
- **Fail closed on uncertainty.** A failed read, a rate limit, a 5xx, or a
  403 (which can mean the token lost access) raises `ProviderError`. Any
  other unexpected response does the same. **Uncertainty never cancels
  anything.**
- **What `GONE` requires.** The adapter must confirm that the repository
  itself is readable and that the issue is not: a 404 or 410, or a
  same-origin redirect to another repository. A redirect is never followed
  into another repository's issue. Without that confirmation, the result is
  `ProviderError`.

### 2. When the coordinator reads the standing

- **When:**
  - at the start of every pass, for every **non-terminal** execution
    (recovery already reads the tracker there);
  - then by the worker, at the safe points before:
    - a new attempt (CLAIMED or RETRY_WAIT to PROVISIONING);
    - the agent run;
    - the freeze;
    - each verification run;
    - the **first** push.
- **Never read:**
  - for terminal executions;
  - after a `publish-revision` intent exists. From then on it is too late:
    ADR-0025 §3 and ADR-0021 keep the claim while the change request is
    open, and the change-request follow-up decides the claim from there.
- **During the agent run** the tracker is not read. The run ends normally or
  times out. The safe point before the freeze then sees the standing, and
  a cancelled run is never frozen (I12).
- **Reads are not journaled.** A read is not a side effect (ADR-0012). The
  observation that decided something is recorded on the decision's event
  (ADR-0024 §1).

### 3. What each standing does

| Standing | Action |
| --- | --- |
| `HELD` | Nothing. With `label_missing`, the label is restored (see §5). |
| `INELIGIBLE`, `CLAIMED_BY_OTHER`, `GONE` | A cancellation is **decided** (see §4), then ended by ADR-0025's machinery. |
| `CLAIM_LOST`, first observation | Recorded as an observation only. Nothing else happens. |
| `CLAIM_LOST`, again in a **later pass** | A cancellation is decided. |

**The decision record.** The worker holds the lease and records
`execution.cancel_requested` in a fenced commit, exactly as it applies an
inbox request (ADR-0025 §1, §4). The fields:
- `actor`: `coordinator` (not a human);
- `reason`: the standing's reason code;
- `data`:
  - `decision`, which follows ADR-0025's rule: `accepted`, `too-late`,
    `already-terminal` or `duplicate`;
  - `observed`, the registered observation (§8).

  There is no `request` id: there was no request file. ADR-0024's data
  schema changes so that `request` is required for a human actor and absent
  for the coordinator.

  One recording per pass and decision. A pending cancellation is the same
  predicate as in ADR-0025, `decision == "accepted"`, so I12, the safe
  points, the ending and crash recovery apply unchanged.

**The ending,** as in ADR-0025 §3:
- **Transition reason.** The `CANCELLED` transition carries **the accepted
  decision's reason code**: `cancel-requested` for a human, `task-closed`
  and so on for the tracker.
- **Note and release.** The note on the task says why. The claim is released
  through the journaled release, step `cancelled`, which removes only this
  instance's claim (ADR-0013). If quarantine remains, the execution is
  BLOCKED and the claim kept.
- **`GONE`.** There is nothing to post or release on a task that no longer
  exists. The release is attempted, is idempotent, and its "not found"
  counts as done.

**Starting the task again:**
- **Human or lost claim** (`cancel-requested`, `task-claim-lost`): **never
  automatically**, as ADR-0025 decides. A human resets it.
- **`INELIGIBLE` or `CLAIMED_BY_OTHER`:** the task becomes eligible again
  only through a deliberate human act (re-labelling, reopening,
  unassigning). `list_eligible` shows that, and a **new** execution may then
  start. The cancelled execution itself is never resumed.

### 4. Precedence

- **The first accepted cancellation wins**, whether human or tracker. Later
  ones are recorded `duplicate`, and the ending is not repeated.
- **The first committed transition wins:**
  - a failure recorded before a tracker decision stays a failure;
  - a cancellation never turns into a failure (ADR-0025 §3).
- **After a `publish-revision` intent, a tracker decision is not made at
  all.**
  - The standing is not read there (§2).
  - A closed issue with an open change request keeps its claim until that
    request is merged or closed (ADR-0021).
  - If a human pushes the issue through to closed, the existing follow-up
    still releases the claim when the change request is resolved.
- **A tracker cancellation never touches what is not Ilmar's:**
  - human claim labels, assignees and other instances' claims stay
    (ADR-0013);
  - only this instance's claim comment and `claimed_label` are removed.

### 5. Claims: labels, and orphaned claims

**Restoring a missing label.**
- `HELD` with `label_missing`: the worker calls the existing `claim`, which
  in the "only our claim present" path re-applies `claimed_label` and
  returns `CLAIMED` (ADR-0013).
- It is journaled as a new repeatable operation `restore-claim`, keyed
  `restore-claim:<execution>:<attempt>:<n>`.
- Any other result of that `claim` is treated as the corresponding standing
  in §3.

**Orphaned claims.**

1. **The port read.** `TaskProvider.held_claims() -> Sequence[HeldClaim]`
   is read-only. It lists every task on which **this instance holds an
   authentic claim** (§6). A `HeldClaim` is a `TaskRef` plus an opaque,
   provider-made `claim_id`, which is the claim token.
2. **When.** Each pass, after recovery and **before** the change-request
   follow-up and before picking new work (ADR-0022, decision 4).
3. **What backs a claim.** A held claim is **backed** if any execution of
   that task:
   - is non-terminal; or
   - is `SUCCEEDED` with its change-request resource still `ACTIVE` or
     `RELEASING` (ADR-0021); or
   - is terminal with quarantined resources (the claim is kept for a
     human); or
   - still `needs_attention`, in which case its own worker will release
     it.

   **Anything else is orphaned.**
4. **Releasing an orphan.** It goes through the new **instance-scoped**
   journaled operation `release-orphan-claim`, keyed by the provider and a
   digest of `(task key, claim_id)`.
   - The effect is the existing `release(ref)`, which removes only this
     instance's claims (ADR-0013).
   - It is repeatable and idempotent.
   - It is fenced: the instance lock and the fence, not an execution lease,
     because there is no execution.
5. **The ADR-0020 amendment.** An operation may name no execution only when
   its kind is in a closed set of instance-scoped kinds. Today that set is
   `release-orphan-claim` alone.
   - **Store:** a migration (ADR-0024's migration rules) makes the
     operation's execution reference optional for those kinds only. Every
     other kind still requires one, and the store and the journal enforce
     it.
   - **Events:** `operation.*` events of instance-scoped operations carry no
     execution. I11's fold accounts for them by operation key.

The #15 scenario of a claim posted before its record exists cannot occur,
because the record is written first (ADR-0021). A test keeps that order.

### 6. Port changes and their security

**The port** gains two read-only methods: `standing(ref)` and
`held_claims()`. `claim`, `release`, `get`, `list_eligible` and
`post_update` keep their signatures. `claim` gains the authenticity rule
below.

**Authentic own claims, hardening ADR-0013.** A claim comment counts as
*this instance's* only when:
- its marker names this instance; **and**
- its author is this adapter's own identity.

That rule applies to `claim`, `release`, `standing` and `held_claims`.

- **How the identity is found.** The adapter learns it once from
  `GET /user`, which is expected to work for user and fine-grained tokens.
  Where that is unavailable, as expected for GitHub App installation
  tokens, it comes from a configured `bot_login`.
  - Both expectations rest on documentation snippets ([R20]). The
    implementation must verify them with read-only calls and record the
    result in `docs/research/sources.md`, as ADR-0013 did. An unknown identity fails closed:
  - `held_claims` and `standing` raise `ProviderError`;
  - so nothing is released and nothing is cancelled.
- **What it prevents.** A marker naming this instance but written by
  someone else is **another party's claim**. It means back off, which is
  denial of progress at worst, as ADR-0013 already accepts. It can never
  make Ilmar treat the issue as its own, delete that comment, or remove a
  label.

**Security implications:**
- **No new credential and no new scope.** Both reads use the existing token
  and Issues read access. Re-applying a label and releasing use the writes
  Ilmar already has. Credentials stay in the coordinator (ADR-0006); the
  sandbox is unchanged.
- **Origin rules unchanged.** The API origin, the same-origin redirect rule
  and the pagination-host check (ADR-0013) apply to the new reads as well.
  A transferred issue is reported `GONE`, never followed into another
  repository.
- **Untrusted input.** Task text and labels stay untrusted:
  - the standing is a closed enum, plus registered reason codes;
  - nothing the core decides depends on free text from the tracker;
  - error messages never contain the token (ADR-0013).
- **The blast radius of a wrong store.** A replaced or empty store under the
  same instance id makes **this instance's** claims look orphaned, and they
  are released. That is exactly the lost-state recovery this ADR wants. It
  can never touch a human's or another instance's claim, because release is
  limited to authentic own claims. Each release is journaled and evented.
  Each pass reports the count, so an operator sees a mass release.
- **Rate limits.** `standing` costs an issue read plus its comment pages.
  `held_claims` costs one paginated issue listing plus each listed issue's
  comments. Both are bounded by the number of executions and safe points.
  A rate-limit response is `ProviderError` (§1): the pass reports it, and
  nothing is decided.
- **Architecture test.** `standing` and `held_claims` join the set of
  **read** port methods in `tests/architecture/test_side_effects.py`. That
  is a change to a sensitive test, and it needs explicit approval: it widens
  what the core may call outside the journal **only by these two reads**.

### 7. Crash recovery

| Crash | Recovery |
| --- | --- |
| Before a decision is recorded | Nothing durable happened. The next pass reads the standing again and decides from the tracker as it is then. |
| After the decision, before the ending completes | It is a pending cancellation: ADR-0025 §5 applies unchanged. |
| During `restore-claim` | It is repeatable: performed again. An unclear outcome is settled by the next standing read. |
| During `release-orphan-claim` | It is repeatable and idempotent: performed again by the next pass, which lists held claims again. |
| After an orphan release, before its record | The next pass no longer lists the claim. The intent is settled as succeeded by that observation (`claim-absent`). |

**A new invariant I13, "Claims follow the store",** checked at every crash
point in the crash suite:
- after a pass that completes, every authentic claim of this instance is
  backed (§5);
- a claim is removed only by this instance releasing **its own** claims;
- human claim labels, assignees and other instances' claims are never
  changed.

### 8. Events (ADR-0024 registry, additive)

- **Reason codes:**
  - `task-closed`, `task-unlabelled`, `task-excluded`;
  - `task-claimed-by-human`, `task-claimed-by-other-instance`;
  - `task-gone`, `task-claim-lost`;
  - `orphaned-claim` (the reason of a `release-orphan-claim`).
- **Observations:**
  - one per standing reason above;
  - `claim-label-missing`;
  - `claim-absent` (settling an interrupted orphan release).
- **The `execution.cancel_requested` data schema:** `request` is required
  only for a human actor. `observed` is required for the coordinator actor.
- **No new event type.** Instance-scoped operations reuse `operation.*`
  without an execution.

### 9. Should #51 (the stateful GitHub emulator) come first?

**Recommendation: no. #51 is not a prerequisite for #12's implementation,
but it must precede #20** (unattended operation, which already depends on
#59).

**Why not first:**
- **What #12 depends on is in the issues API alone:** issue state, labels,
  assignees, comments with authors, pagination, transfers and 404 or 410
  handling. The adapter is already tested against an in-memory REST fake
  with pagination, plus a live opt-in test (ADR-0013 "Verification").
- **The fake can model every case this ADR needs:**
  - comment authors and forged markers;
  - deleted comments;
  - transferred issues (same-origin redirects to another repository);
  - 404 versus 403;
  - rate-limit responses;
  - read-after-write lag, by injecting a stale read.
- **#51's distinct value lies elsewhere:**
  - real git over HTTPS for the source-control adapter;
  - coordinator end-to-end runs against realistic GitHub state;
  - drift detection against real GitHub.

  Neither #12 path needs real git.
- **#51 is large and sensitive:** a new `ca_bundle` credential-path ADR,
  and a vendored OpenAPI extract under the MANIFEST §7 process. Putting it
  first would delay F-001, the M3 core item, without lowering #12's risk
  much.

**Conditions:**
1. #12's implementation must extend the in-memory REST fake with each case
   above, and the opt-in live test must exercise `standing` and
   `held_claims` against a real repository.
2. When #51 lands, `TaskProviderContract` (including the new standing
   cases) must also run against the emulator. Any drift is a bug in the
   fake or in the adapter.
3. If the maintainer wants reconciliation verified end to end against
   realistic GitHub state **before** #12 merges, #51 goes first. That is a
   schedule choice, not a safety requirement.

### Tests the implementation must add

- **Contract (`TaskProviderContract`; the fake and the GitHub adapter):**
  - each standing and each reason: a task becomes closed, unlabelled,
    excluded, assigned, human-claimed, claimed by another instance, gone, or
    loses our claim;
  - the precedence of reasons;
  - `held_claims` lists exactly the authentic own claims;
  - read-only: no write is made by `standing` or `held_claims`.
- **The GitHub adapter (in-memory REST fake):**
  - **Forged markers:** a forged own-instance marker is another party's
    claim. It is never released, never deleted, and never counted as ours.
  - **Identity:**
    - `GET /user` identity, and the `bot_login` fallback;
    - an unknown identity fails closed.
  - **`GONE`:** confirmed only with a readable repository. A 403 is never
    `GONE`, and a transfer is never followed.
  - **Errors:** rate limits and 5xx are `ProviderError`.
  - **Pagination:** of issues and of comments.
- **Coordinator:**
  - each standing at each safe point (§2):
    - the effect;
    - the decision record (actor, reason, observation);
    - the `CANCELLED` reason code;
    - the release (only our claim);
    - human labels untouched.
  - the reads (§2):
    - nothing is read or decided after a publish intent;
    - a closed issue with an open change request keeps its claim;
    - terminal executions are not read.
  - `CLAIM_LOST` decides only on a second pass; a single observation only
    records.
  - the label is restored through `restore-claim`.
  - human and tracker precedence, in both orders:
    - the first accepted cancellation wins;
    - a failure first stays a failure.
  - starting again (§3):
    - re-pick is allowed after `INELIGIBLE` or `CLAIMED_BY_OTHER` once the
      task is eligible again, as a **new** execution;
    - never after a human cancellation or a lost claim.
  - orphaned claims:
    - orphans are released before new work, and every backing rule keeps
      its claim;
    - another instance's claim and a human claim are never touched;
    - a claim of a lost state directory is released before the first pick.
  - a test pinning that the execution record is written before the claim.
- **Crash suite:** new scenarios run at every write and every effect crash
  point, and again with a second crash during recovery. They check I11, I12
  and **I13**:
  - an issue closed during the agent run;
  - a human claim added mid-pass;
  - an orphan release;
  - a label restoration.
- **Store:** the migration, and instance-scoped operations:
  - only the registered kinds may omit the execution;
  - fenced;
  - I11 holds over them.
- **Architecture:**
  - only `standing`, `held_claims`, `get` and `list_eligible` are callable
    outside the journal;
  - only the worker records tracker decisions;
  - `release-orphan-claim` is the only instance-scoped operation kind.
- **Live (opt-in):** `standing` and `held_claims` against a real repository,
  including a forged-marker comment written by a second account where one
  is available.

## Consequences

**Good:**
- The tracker is the authority on whether work is wanted, as F-001 asks.
  Closing, unlabelling, excluding or claiming an issue stops Ilmar at the
  next safe point. This happens before freeze or publish, and without
  touching anyone else's markers.
- A vanished task ends `CANCELLED` (`task-gone`) instead of staying BLOCKED
  forever, but only when its absence is confirmed.
- Lost or restored state no longer strands claims, and missing labels are
  restored.
- Forged markers can no longer make Ilmar act on comments it did not write.
- Every decision reuses ADR-0025's safe points, ending, I12 and crash rules.
  There is one cancellation machine with two sources.

**Bad:**
- **More tracker reads:** a few per safe point. On large backlogs that
  counts against REST rate limits.
- **A lag for ineligibility during an agent run.** It takes effect only
  after the run, bounded by the agent timeout, and the output is not
  frozen.
- **A label-less orphan is invisible.** An orphaned claim without
  `claimed_label`, its comment only, is not found by `held_claims`. A human
  removing Ilmar's label is overriding ADR-0013's ownership rule. Such a
  claim still never blocks this instance, because an own claim reads as
  "ours" and is re-claimed.
- **A schema migration and an ADR-0020 amendment,** for one instance-scoped
  operation kind.
- **A new configuration need:** `bot_login` is required where the adapter
  cannot learn its own identity.

## Revisit when

- **Agent runs grow long enough** that ineligibility during the run wastes
  significant model spend. Then consider letting the watcher also poll
  `standing` through a pre-bound, read-only callable. That needs an
  ADR-0025 amendment and a change to its architecture test.
- **Rate limits are hit in practice.** Consider conditional requests
  (ETags) for the standing reads, or webhooks feeding a durable queue.
- **Several instances per repository become a requirement** (ADR-0013's
  Revisit when). Authentic claims become the basis for an authoritative
  lease.
- **A second tracker adapter (#23)** shows that `TaskStanding`'s outcomes do
  not fit a tracker with native states. Then generalize the outcomes, not
  the core's decisions.
