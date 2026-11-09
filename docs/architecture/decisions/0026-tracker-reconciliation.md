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

**How a missing label is restored:**
1. *Through `claim`* (the previous draft). Rejected (final review). If the
   claim object vanished meanwhile, `claim` either posts a new claim,
   undoing a human's revocation, or reports `CLAIMED_BY_OTHER` because of
   Ilmar's own label. That turned into a cancellation on one read, with a
   false reason.
2. **A targeted `restore_label` on the exact object the `HELD` read
   returned.** Chosen. It only adds the label, and its result never decides
   anything (§5).

**Debouncing ineligibility:**
1. *Require every ineligibility to be seen on two passes.* Rejected. It adds
   latency to deliberate human acts, and the risk it guards against is only
   real for one case.
2. **One observation decides, except for a lost claim.** Chosen. A lost
   claim can be read-after-write lag on Ilmar's own write.

**How a lost claim is confirmed:**
1. *A minimum persisted wall-clock age* (the second draft: 300 s).
   Rejected. ADR-0022 §Time forbids comparing a persisted timestamp with the
   current time to make a decision, so that a clock stepping back, or a
   restored snapshot, can never change an outcome.
2. **Independent observations, ordered by the store, in separate passes.**
   Chosen. Each is a fresh read. Each comes from a pass that **follows**
   Ilmar's own last claim write, and the first and the confirming
   observation come from different passes. Order comes from the store's
   event sequence and the pass identity (ADR-0024), never from a clock.

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
| `HELD` | Open, still eligible-labelled, not excluded, no other claim, and **this instance's own claim is present**. It carries that claim as a `HeldClaim` (§5): the task, the token and the object id. `label_missing=True` when the claim comment is there but `claimed_label` is not. | — |
| `CLAIM_LOST` | Open and still wanted, but this instance's claim is gone, for example its comment was deleted. | `task-claim-lost` |
| `CLAIMED_BY_OTHER` | An assignee, a human claim label, or another instance's claim is present (ADR-0013). | `task-claimed-by-human`, `task-claimed-by-other-instance` |
| `INELIGIBLE` | Closed, the eligible label removed, or an excluded label added. | `task-closed`, `task-unlabelled`, `task-excluded` |
| `GONE` | The task **positively** no longer exists here: deleted, or transferred to another repository. | `task-gone` |

**Rules:**
- **Precedence of reasons.** When several apply, the most decisive one is
  reported, in this order: `GONE`, then `task-closed`, then
  `CLAIMED_BY_OTHER`, then the label reasons, then `CLAIM_LOST`.
- **Every read is fresh.** Each call issues its own requests to the
  tracker, in the current pass.
  - **No cache:** no client-side cache, memo or reuse of a response
    across calls or passes.
  - **Allowed:** conditional requests (ETags), because the tracker itself
    validates them.
  - **Why:** §3a's two observations are independent only because of this.
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

A lost claim cancels only after **two independent observations**, with no
sign of the claim between them, in **three distinct passes**:

| Pass | What happens there |
| --- | --- |
| `P0` | This execution's own latest claim write: the pass of the **latest commit of its `claim` operation**, whether that is its intent, a repeated intent or its outcome. A label restore (§5) is not a claim write: it never posts or removes a claim. |
| `P1` | The first observation, a fresh `CLAIM_LOST` read, is recorded. `P1` differs from `P0` and follows it. |
| `P2` | The confirming observation, a fresh `CLAIM_LOST` read. `P2` differs from `P1` and follows it. |

**Time is never consulted** (ADR-0022 §Time). Order comes from durable
facts only:
- **Event sequence.** The store assigns every event a `seq` that increases
  with every commit (ADR-0024). "Follows" means a greater `seq`.
- **Pass identity.** Every event carries the id of the pass that wrote it
  (ADR-0024). Passes of one instance never overlap, because they run under
  the instance lock (ADR-0012).

So a wall clock that steps back or forward, or a restored snapshot, can
neither shorten nor lengthen the confirmation, and cannot change its
outcome.

**Why this protects against read-after-write lag:**
- **No observation in the writing pass.** A read in `P0`, the pass that
  wrote the claim, is never recorded and never counts. That is the window
  where Ilmar's own write may not be visible yet.
- **Two later observations must agree.** The first comes from a later pass
  and the confirmation from another later pass, each a fresh read. Between
  them lie a full commit cycle, the end of one pass, and the start of the
  next, with their own tracker reads.
- **A disagreeing read resets.** Any read in between that sees the claim
  resets everything (see "Reset").

A stale view would have to persist across two whole passes after Ilmar's
own write to cancel anything. If that is ever observed in practice, the
answer is more passes, never a timer (Revisit when).

**The record.** A new registered extra event, `execution.claim_lost`, is
written by the worker that holds the lease. It goes in a fenced commit,
through the same path as `execution.cancel_requested` (ADR-0025 §4, and the
store's checks on extra events). Its fields:
- `actor`: `coordinator`;
- `reason`: `task-claim-lost`;
- the envelope's `pass` (ADR-0024) and `epoch`;
- `data.state`: `observed` or `cleared`;
- `data.claim`: the id of the claim the execution holds. It is the opaque
  `claim_id` that the `claim` operation recorded in its result.

**The open observation.** It is the latest `execution.claim_lost` event of
the execution, if its state is `observed`. At most one is open at a time.
The predicate is read from the durable events, never from memory.

**Recording the first observation.** The worker records `observed` and
continues when **all** of the following hold:
- the standing is `CLAIM_LOST`;
- no observation is open;
- **the current pass is not `P0`.** `P0` is the pass of the latest
  `operation.*` event of this execution's `claim` operation;
- **that `claim` operation has succeeded.** While it is absent or
  unfinished, the execution has no claim to lose, so `CLAIM_LOST` is
  ignored.

Nothing else changes: no transition, no release, no effect. A `CLAIM_LOST`
read in the claim's own pass is ignored. It is neither recorded nor
counted.

**Confirming.** At a later safe point, the worker reads the standing
**afresh in this pass**. It decides a `task-claim-lost` cancellation only
when **all** of the following hold:
1. the fresh read is again `CLAIM_LOST`;
2. an observation is open;
3. it was recorded in **another pass**: its pass id differs from the
   current one, and its `seq` is lower than any event of the current pass;
4. it **follows this execution's latest claim write:**
   - its `seq` is greater than that of every `operation.*` event of the
     `claim` operation;
   - its pass differs from the pass of the latest one;
5. its `data.claim` equals the claim id in the `claim` operation's result;
6. the execution is not past a `publish-revision` intent (§2).

Rules 3 to 5 read only event order and identities from the store. No
timestamp takes part.

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
- **Epochs need no ordering of their own.** Every pass of a later epoch
  follows every pass of an earlier one in `seq`, because the store numbers
  events in commit order across epochs.

**Reset.** The open observation is closed with a `cleared` event, in the
same commit as what caused the reset, when any of these happen:
- a fresh read shows `HELD`, with or without `label_missing`: the claim
  reappeared;
- any commit of this execution's `claim` operation (an intent, a repeated
  intent, or an outcome): Ilmar's own claim write, so a later "lost" is a
  new observation. If the commit cannot carry the `cleared` event (a
  repeated intent takes no extra changes, see the journal), `cleared` is
  committed on its own just before it. A crash in between only clears,
  which is the safe direction;
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

**Restoring a missing label: targeted, and never a claim.**
- **When:** the standing is `HELD` with `label_missing`.
- **How:** the worker calls the new
  `TaskProvider.restore_label(claim) -> RESTORED | ABSENT | CHANGED`, with
  the `HeldClaim` that this `HELD` read returned.
- **The journal:** the new repeatable operation `restore-claim-label`,
  keyed `restore-claim-label:<execution>:<attempt>:<n>`. It records the
  claim's object id and token in its intent.
- **The effect** mirrors `release_claim`:
  1. Re-read **that object** by its id.
  2. If it is absent, the result is `ABSENT`.
  3. If it is no longer this adapter's authentic claim with that token, the
     result is `CHANGED`.
  4. Otherwise add `claimed_label`, and only that label. The result is
     `RESTORED`.

  It **never posts a claim, never deletes anything, and never removes a
  label.**
- **The result is never a standing decision:**
  - `RESTORED` and `ABSENT` settle the operation as succeeded. `ABSENT` is
    recorded with the observation `claim-absent`.
  - `CHANGED` settles it as failed (`claim-changed`).

  What happens to the execution is decided only by the next fresh
  `standing` read, under §3 and §3a. An `ABSENT` here is **not** a
  `CLAIM_LOST` observation.
- **Interrupted, it is simply performed again from its recorded intent.**
  That is safe: it can only re-add a label beside the one authentic claim
  object it names.
- **Why not reuse `claim`** (the previous draft):
  - **A false reason on one read:** if the claim comment vanished between
    the read and the effect, while Ilmar's own label was present (for
    example after an interrupted restore that had already re-added it),
    `claim` returns `CLAIMED_BY_OTHER`. The draft mapped that to an
    immediate cancellation, with a wrong reason and on a single read,
    bypassing §3a.
  - **Undoing a human's revocation:** without the label, `claim` posts a
    **new** claim, silently undoing a human's deletion of Ilmar's claim.

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
- two targeted writes, each acting on exactly one named claim object (§5):
  - `release_claim(claim) -> RELEASED | ABSENT | CHANGED`: used only by
    `release-orphan-claim`; it deletes at most that object;
  - `restore_label(claim) -> RESTORED | ABSENT | CHANGED`: used only by
    `restore-claim-label`; it only adds `claimed_label` beside that object.

`claim` returns the new claim's `claim_id` alongside its outcome, so that
the execution's `claim` result can record it (§3a). `claim` is called only
to claim a task (ADR-0021), never to repair one.
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
| During `restore-claim-label` | Performed again from its recorded intent: the same object id and token. It can only add the label beside that authentic object, and otherwise settles as `ABSENT` or `CHANGED`. It is never a standing decision; the next fresh `standing` read decides (§5). |
| `release-orphan-claim` intent recorded, effect not done | Performed again **from the recorded intent**: the same object id and token, never re-selected by task. If the object is gone, the result is `ABSENT`. A newer claim posted meanwhile is a different object and is untouched. |
| After the delete, before the label step or the record | Performed again: the object is `ABSENT`, the label step reruns its "no claim remains" check, and the operation settles as succeeded (`claim-absent`). |
| After the effect, before the outcome commit | It is ambiguous. Settled by observation: re-read the recorded object. If it is absent, succeeded. If it is present and unchanged, delete again. If it changed, failed (`claim-changed`). |
| Before recording a first `CLAIM_LOST` observation | Nothing is durable. The next pass reads again and records the first observation then. |
| After the first observation is recorded | It is open, and durable across the crash. Confirmation needs a fresh `CLAIM_LOST` read in a **later** pass, ordered by `seq` (§3a). The restarted coordinator's passes always follow it. |
| During `claim`, or after it, before any later read | Every commit of the `claim` operation carries its pass. `P0` is the pass of the latest one, so an interrupted claim's pass counts as a write pass too. An unfinished `claim` makes `CLAIM_LOST` ignored altogether. No observation can come from a claim write's lag window, even across a crash. |
| Between the confirming read and its commit | Nothing is durable. The next pass rereads and decides then, or resets if the claim is back. |
| After the confirming commit | The decision is a pending cancellation: ADR-0025 §5 applies. |

**A new invariant I13, "Claims follow the store",** checked at every crash
point in the crash suite:
- after a pass that completes, every authentic claim of this instance is
  backed (§5);
- an orphan release deletes only the object recorded in its intent, after
  revalidation;
- a label restore never posts a claim and never decides a cancellation;
- no `task-claim-lost` cancellation exists without an earlier, uncleared
  `observed` event. That event must come from a pass that differs from the
  decision's pass and from the pass of the latest claim write before it,
  and must follow that write in `seq`;
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
  - **Freshness:** each `standing` call issues its own tracker requests. A
    change made between two calls, including between two passes, is always
    seen by the second. No response is reused.
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
  - the label is restored through `restore-claim-label`, and only beside
    the authentic claim object that the `HELD` read returned.
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
    - a `CLAIM_LOST` read in the **pass of the claim write itself** is
      neither recorded nor counted, including in a pass that both claims
      and reaches later safe points;
    - an observation and a confirmation in the same pass never decide.
  - **Confirmation:**
    - it decides exactly once, with a fresh `CLAIM_LOST` read, in a pass
      after the observation's pass, which itself followed the claim write's
      pass (three distinct passes, ordered by `seq`);
    - the `cleared` event and the decision are one commit.
  - **Reset:**
    - the claim reappears (`HELD`) between the observations: `cleared`
      with `claim-present`, and a later loss needs two observations again;
    - any commit of this execution's `claim` operation after the
      observation (intent, repeated intent, outcome): `cleared`, and the old
      observation can never confirm;
    - an observation whose `data.claim` differs from the current claim id
      never confirms.
  - **Read-after-write lag:**
    - a just-posted own claim that stays invisible for every read of its
      own pass never cancels;
    - one that is invisible for that pass plus one more pass, then seen,
      resets and never cancels;
    - while the `claim` operation is unfinished, `CLAIM_LOST` is ignored;
    - the pass of an **interrupted** `claim` counts as `P0`.
  - **Label restore is never a decision (regressions for the review of
    `cc295d7`):**
    - an interrupted label restore that had already re-added the label,
      then the claim comment deleted by a human, then the restore performed
      again: the restore settles `ABSENT`. There is **no** cancellation in
      that pass, **no** `CLAIMED_BY_OTHER`, and nothing is posted. The lost
      claim is then confirmed only under §3a: two later fresh reads in two
      passes, with reason `task-claim-lost`.
    - the claim comment deleted between the `HELD` read and the restore's
      effect, with the label missing: `ABSENT`. No new claim is ever posted,
      and the human's deletion stands.
    - the claim comment edited (token, marker or instance), or replaced by
      a forged copy, before the effect: `CHANGED`. No label is added.
    - `restore_label` performs no write other than adding `claimed_label`
      to that one task.
  - **Precedence:** an `INELIGIBLE`, `CLAIMED_BY_OTHER` or `GONE` read
    while an observation is open decides at once with that reason, and
    clears the observation.
  - **Publish intent:** past it, nothing is observed or decided.
  - **Fencing:**
    - a fenced-out coordinator records neither an observation, nor a
      clearing, nor a decision;
    - an observation from an earlier epoch still needs the fresh read and
      rules 3 to 5.
  - **Time is never consulted (ADR-0022 §Time):**
    - with the wall clock frozen, stepped back by hours, or stepped forward
      by days between and within passes, every decision is identical to a
      run with a steady clock;
    - a restored store snapshot does not change the outcome of rules 3
      to 5;
    - a test asserts that the confirmation code reads no timestamp.
- **Lost-claim confirmation, crash recovery (mandatory):** a crash at every
  write and every effect point, and a second crash during recovery, around:
  - the claim write itself;
  - recording the first observation;
  - the confirming read;
  - the confirming commit;
  - a reset.

  Also with a coordinator restart (a new epoch) between `P0`, `P1` and
  `P2`.

  In each case:
  - at most one open observation;
  - at most one decision;
  - never a decision from one observation, or from observations whose
    passes are not distinct and ordered after the claim write as §3a
    requires;
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
- **A lost claim cancels slowly:** only in the second pass after the one
  that wrote the claim. How long that takes depends on how often passes
  run, not on a timer. Until then, the execution continues without a
  visible claim. That is accepted, because a lost claim is rare (a human
  deleted Ilmar's own comment), and acting on one stale read would be
  worse.
- **A schema migration and an ADR-0020 amendment,** for one instance-scoped
  operation kind.
- **A new configuration need:** `bot_login` is required where the adapter
  cannot learn its own identity.

## Revisit when

- **Agent runs grow long enough** that ineligibility during the run wastes
  significant model spend. Then consider letting the watcher also poll
  `standing` through a pre-bound, read-only callable. That needs an
  ADR-0025 amendment and a change to its architecture test.
- **Tracker reads are observed to stay stale across two whole passes.**
  Then require more independent observations, or a read-your-writes check
  that the adapter can prove (for example, the claim object read directly
  by its id). Never a wall-clock threshold (ADR-0022 §Time).
- **Rate limits are hit in practice.** Consider conditional requests
  (ETags) for the standing reads, or webhooks feeding a durable queue.
- **Several instances per repository become a requirement** (ADR-0013's
  Revisit when). Authentic claims become the basis for an authoritative
  lease.
- **A second tracker adapter (#23)** shows that `TaskStanding`'s outcomes do
  not fit a tracker with native states. Then generalize the outcomes, not
  the core's decisions.
