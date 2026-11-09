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

**What an orphan release removes:**
1. *`release(ref)`: every claim of this instance on the task.* Rejected
   (review of the first draft). A retried or crash-resumed release could
   then delete a **newer** claim of this instance on the same task, made
   after the orphan was observed.
2. *Re-list the claims and delete those with the observed token.* Rejected.
   It still selects by content, which can be edited, rather than by an
   identity that cannot change.
3. **A targeted release of exactly the observed claim.** Chosen. It acts
   on the tracker's immutable object id (on GitHub, the comment id), and
   rechecks the token, marker and author right before deleting. A newer
   claim is always a **different object**, so it can never be selected.

**Debouncing ineligibility:**
1. *Require every ineligibility to be seen on two passes.* Rejected. It adds
   latency to deliberate human acts, and the risk it guards against is only
   real for one case.
2. **One observation decides, except for a lost claim.** Chosen. A lost
   claim can be read-after-write lag on Ilmar's own write.

**Where the first lost-claim observation lives:**
1. *In memory.* Rejected. A crash or restart forgets it, so a lost claim
   would either never be confirmed or be confirmed by a single read.
2. *A ledger row.* Rejected. It is not a resource Ilmar owns or must clean
   up.
3. **A fenced, durable execution event, recorded by the lease holder.**
   Chosen. It survives crashes, carries the pass, epoch and time, and is
   refused from a fenced-out coordinator (§3a).

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
| `CLAIM_LOST`, no open first observation | The first observation is recorded durably (§3a). Nothing else happens. |
| `CLAIM_LOST`, confirmed under §3a's rules | A cancellation is decided. |
| `CLAIM_LOST`, not yet confirmable | Nothing, and nothing new is recorded. |

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

### 3a. Confirming a lost claim: durable, fenced, revalidated

A lost claim cancels only after **two independent observations**:
- they are made in **two different passes**;
- they are at least **`CLAIM_LOST_CONFIRM_SECONDS` = 300** apart, on the
  coordinator's persisted, non-decreasing clock (I10);
- **no sign of the claim** appears between them.

That makes a single stale read, including lag on Ilmar's own write, harmless.

**The record.** A new registered extra event, `execution.claim_lost`, is
written by the worker that holds the lease. It goes in a fenced commit,
through the same path as `execution.cancel_requested` (ADR-0025 §4, and the
store's checks on extra events). Its fields:
- `actor`: `coordinator`;
- `reason`: `task-claim-lost`;
- the envelope's `pass` (ADR-0024) and `epoch`;
- `data.state`: `observed` or `cleared`;
- `data.claim`: the id of the claim the execution last held. It is the
  opaque `claim_id` from the execution's latest successful `claim` or
  `restore-claim`, recorded in that operation's result.

**The open observation.** It is the latest `execution.claim_lost` event of
the execution, if its state is `observed`. At most one is open at a time.
The predicate is read from the durable events, never from memory.

**Recording the first observation.** When the standing is `CLAIM_LOST` and
no observation is open, the worker records `observed` and continues. Nothing
else changes: no transition, no release, no effect.

**Confirming.** At a later safe point, the worker reads the standing
**afresh in this pass**. It decides a `task-claim-lost` cancellation only
when **all** of the following hold:
1. the fresh read is again `CLAIM_LOST`;
2. an observation is open;
3. it was recorded in **another pass**: a different pass id than the
   current one;
4. it was recorded at least `CLAIM_LOST_CONFIRM_SECONDS` before now,
   measured on the persisted, non-decreasing clock;
5. its `data.claim` equals the execution's current claim id. No `claim` or
   `restore-claim` of this execution has succeeded since the observation.
6. the execution is not past a `publish-revision` intent (§2).

The confirmation and the decision are one fenced commit:
- a `cleared` event closing the observation;
- the `execution.cancel_requested` decision itself.

**What fencing guarantees:**
- **A fenced-out coordinator records nothing.** No observation, no
  clearing and no decision.
- **An observation from an earlier epoch still counts.** A restart is the
  case it exists for: the claim stayed lost across it. But it is
  **revalidated** by the fresh read and by rules 3 to 5. An old record is
  never enough on its own.

**Reset.** The open observation is closed with a `cleared` event, in the
same commit as what caused the reset, when any of these happen:
- a fresh read shows `HELD`, with or without `label_missing`: the claim
  reappeared;
- this execution's `claim` or `restore-claim` succeeds: Ilmar's own write,
  so a later "lost" is a new observation;
- any other standing decides the execution: it is `INELIGIBLE`,
  `CLAIMED_BY_OTHER` or `GONE`, which by §1's precedence outranks the lost
  claim and decides at once;
- the execution becomes terminal.

After a reset, a lost claim again needs two observations under these rules.

**Idempotency.** A pass records at most one `observed` per execution. A
second `CLAIM_LOST` read in the same pass changes nothing. An `observed`
with no open predecessor is the only way to open one, so a repeated
recording after a crash cannot open a second observation.

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
   authentic claim** (§6). A `HeldClaim` is a `TaskRef` plus two opaque,
   provider-made values:
   - the `claim_id`: the claim token;
   - the immutable id of the tracker object that holds the claim.

   Rule 4 uses both.
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
4. **Releasing an orphan: exactly the observed claim, and nothing newer.**
   - **`HeldClaim`.** It carries the task, the opaque `claim_id` (the claim
     token), and the tracker's **immutable object id** for the claim (on
     GitHub, the comment id, opaque to the core).
   - **The journal intent.** The new instance-scoped journaled operation
     `release-orphan-claim` records all three in its intent. Its key is the
     provider plus a digest of `(task key, object id, claim_id)`. Each
     observed claim object is therefore its own operation, and a newer
     claim is a different key.
   - **Revalidation before the effect,** after the intent commit and the
     fence check (ADR-0020, step 2):
     - the store still has nothing that backs the task (§5, rule 3);
     - the task has no execution created after the claim was listed.

     Under the instance lock, only this coordinator can create one, and
     the orphan step runs before pick. So a failure here is a
     `LedgerIntegrityError`, never a silent skip.
   - **The effect, `TaskProvider.release_claim(claim)`:**
     1. Re-read **that object** by its id.
     2. If it is absent, the result is `ABSENT`. Nothing is deleted.
     3. If it is present but its author is not this adapter's identity, or
        its marker no longer names this instance with this exact token
        (edited, replaced or forged), the result is `CHANGED`. Nothing is
        deleted.
     4. Otherwise, delete **that object by its id**. The result is
        `RELEASED`.
     5. Then re-read the task's claims. Remove `claimed_label` only if no
        claim of **anyone** remains: not this instance, not another
        instance, and not a human claim label or assignee. Otherwise the
        label stays.
   - **Never by task.** `release_claim` never deletes by task, by token
     match across objects, or by content search. **A newer claim of this
     instance, another instance's claim, and a human's claim are different
     objects: they are never selected.**
   - **The remaining race is on the label alone.** GitHub has no
     compare-and-set (ADR-0013). Between the claim re-check in step 5 and
     the label removal, a newer claim may be posted, and the label goes
     although a claim exists. The comments are the authoritative claims;
     the label is their projection (ADR-0013). The newer claim's owner sees
     `HELD` with `label_missing` at its next safe point, and restores the
     label (§5, "Restoring a missing label"). Nothing is deleted that was
     not observed.
   - **Results:**
     - `RELEASED` and `ABSENT` settle the operation as succeeded. `ABSENT`
       is recorded with the observation `claim-absent`.
     - `CHANGED` settles it as failed, with reason `claim-changed`. The
       object is no longer this instance's authentic claim, so there is
       nothing of ours to release. The pass reports it; it is never
       retried as a delete.
   - **Fencing.** The operation is fenced: the instance lock and the fence,
     not an execution lease, because there is no execution.

   **Normal releases keep `release(ref)`.** That is, the release at the end
   of an execution, after a merge, after a failure or after a cancellation.
   At most one execution of a task can hold a claim at a time, because:
   - the store allows one non-terminal execution per task (ADR-0005);
   - **a task is not picked while an earlier execution of it still has an
     unfinished claim release** (`needs_attention`, recovery before pick,
     ADR-0022). This ADR adds that rule to the pick explicitly. It already
     holds for failed and published tasks, which are never re-picked, and
     it now also covers tracker-cancelled tasks, which may be re-picked
     (§3).
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

**The port gains:**
- two read-only methods, `standing(ref)` and `held_claims()`;
- one targeted write, `release_claim(claim) -> RELEASED | ABSENT | CHANGED`
  (§5). It is used only by `release-orphan-claim`, and it deletes at most
  the one object it names.

`claim` returns the new claim's `claim_id` alongside its outcome, so that
the execution's `claim` and `restore-claim` results can record it (§3a).
`release`, `get`, `list_eligible` and `post_update` keep their signatures.
`claim` and `release` gain the authenticity rule below.

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
- **The targeted release.** `release_claim` can only ever delete the one
  object whose id the journal recorded. Before deleting, it rechecks the
  author, marker and token. So even a compromised or confused store cannot
  make Ilmar delete a comment that is not its own, current, authentic
  claim.
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
| `release-orphan-claim` intent recorded, effect not done | Performed again **from the recorded intent**: the same object id and token, never re-selected by task. If the object is gone, the result is `ABSENT`. A newer claim posted meanwhile is a different object and is untouched. |
| After the delete, before the label step or the record | Performed again: the object is `ABSENT`, the label step reruns its "no claim remains" check, and the operation settles as succeeded (`claim-absent`). |
| After the effect, before the outcome commit | It is ambiguous. Settled by observation: re-read the recorded object. If it is absent, succeeded. If it is present and unchanged, delete again. If it changed, failed (`claim-changed`). |
| Before recording a first `CLAIM_LOST` observation | Nothing is durable. The next pass reads again and records the first observation then. |
| After the first observation is recorded | It is open, and durable across the crash. Confirmation needs a later pass, the elapsed time, and a fresh `CLAIM_LOST` read (§3a). |
| Between the confirming read and its commit | Nothing is durable. The next pass rereads and decides then, or resets if the claim is back. |
| After the confirming commit | The decision is a pending cancellation: ADR-0025 §5 applies. |

**A new invariant I13, "Claims follow the store",** checked at every crash
point in the crash suite:
- after a pass that completes, every authentic claim of this instance is
  backed (§5);
- an orphan release deletes only the object recorded in its intent, after
  revalidation;
- no `task-claim-lost` cancellation exists without an earlier `observed`
  event from another pass, at least `CLAIM_LOST_CONFIRM_SECONDS` before it,
  and not cleared in between;
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
- **One new event type:** the extra event `execution.claim_lost`.
  - Data: `state` is `observed` or `cleared`, and `claim` is the claim id.
  - It is recorded by the lease holder in a fenced commit (§3a). Like
    `execution.cancel_requested`, it changes no row in I11's fold.
- **More observations:** `claim-changed`, and `claim-present` (a reset by a
  fresh `HELD` read).
- **Instance-scoped operations** reuse `operation.*`, without an execution.

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
- **Orphan release, adversarial (mandatory).** A test hook acts exactly
  between the listing, the intent commit, the revalidation, the re-read of
  the object, the delete and the label step. Each concurrent change below
  is tried in each window:
  - a **newer claim of this instance** is posted on the same task;
  - another instance's claim is posted;
  - a human adds a claim label or an assignee;
  - the observed comment is **edited**: token changed, marker removed, or
    instance changed;
  - the observed comment is deleted;
  - a forged copy of the observed marker is posted by another author;
  - the issue is closed or transferred.

  For every window and change:
  - **only the observed object is ever deleted**, and only while it is
    still this instance's authentic claim with the recorded token;
  - every other comment survives byte for byte;
  - `claimed_label` is removed only when no claim of anyone remains at the
    final re-check, and any label lost to the race is restored by the newer
    claim's owner at its next safe point;
  - human labels and assignees are never touched;
  - the operation settles as `RELEASED`, `ABSENT` or `CHANGED`, as
    specified;
  - I11 and I13 hold.
- **Orphan release, crash recovery (mandatory):**
  - a crash at every write and every effect point of an orphan release,
    with a **newer claim of this instance posted before recovery** on the
    same task;
  - a second crash during recovery.

  In each case:
  - recovery acts only on the recorded object id;
  - the newer claim survives;
  - nothing is deleted twice;
  - an ambiguous outcome is settled by observation;
  - I11 and I13 hold.
- **Revalidation:** an execution of the task appearing before the effect is
  a `LedgerIntegrityError`, and nothing is deleted.
- **Picking after a cancellation:** a tracker-cancelled task whose claim
  release is still unfinished is never picked until that release completes.
- **Lost-claim confirmation, adversarial (mandatory):**
  - **A single observation never cancels:**
    - one `CLAIM_LOST` read records `observed` only;
    - a second read **in the same pass** decides nothing and records
      nothing;
    - a second read in a later pass **before** `CLAIM_LOST_CONFIRM_SECONDS`
      decides nothing.
  - **Confirmation:**
    - in a later pass, after the interval, with a fresh `CLAIM_LOST` read,
      it decides exactly once;
    - the `cleared` event and the decision are one commit.
  - **Reset:**
    - the claim reappears (`HELD`) between the observations: `cleared`
      with `claim-present`, and a later loss needs two observations again;
    - this execution's own `claim` or `restore-claim` succeeds after the
      observation: `cleared`, and the old observation can never confirm;
    - an observation whose `data.claim` differs from the current claim id
      never confirms.
  - **Read-after-write lag:** a just-posted own claim, invisible for
    several reads within the interval, never cancels.
  - **Precedence:** an `INELIGIBLE`, `CLAIMED_BY_OTHER` or `GONE` read
    while an observation is open decides at once with that reason, and
    clears the observation.
  - **Publish intent:** past it, nothing is observed or decided.
  - **Fencing:**
    - a fenced-out coordinator records neither an observation, nor a
      clearing, nor a decision;
    - an observation from an earlier epoch still needs the fresh read and
      rules 3 to 5.
  - **The clock:** confirmation uses the persisted, non-decreasing clock.
    A clock set back never shortens the interval.
- **Lost-claim confirmation, crash recovery (mandatory):** a crash at every
  write and every effect point, and a second crash during recovery, around:
  - recording the first observation;
  - the confirming read;
  - the confirming commit;
  - a reset.

  In each case:
  - at most one open observation;
  - at most one decision;
  - never a decision from one observation;
  - I11, I12 and I13 hold.
- **Crash suite:** new scenarios run at every write and every effect crash
  point, and again with a second crash during recovery. They check I11, I12
  and **I13**:
  - an issue closed during the agent run;
  - a human claim added mid-pass;
  - an orphan release;
  - a label restoration;
  - a lost claim confirmed across passes.
- **Store:** the migration, and instance-scoped operations:
  - only the registered kinds may omit the execution;
  - fenced;
  - I11 holds over them.
- **Architecture:**
  - only `standing`, `held_claims`, `get` and `list_eligible` are callable
    outside the journal; `release_claim` is called only by the journaled
    `release-orphan-claim`;
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
- **A lost claim cancels slowly:** at least `CLAIM_LOST_CONFIRM_SECONDS`
  after the first observation, and only in a later pass. Until then, the
  execution continues without a visible claim. That is accepted, because a
  lost claim is rare (a human deleted Ilmar's own comment), and acting on
  one stale read would be worse.
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
