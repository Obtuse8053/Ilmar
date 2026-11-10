# ADR-0027: Stray claim labels, and two clarifications to ADR-0026

- Status: Accepted
- Date: 2026-11-10
- Sources: [R20] · Findings: F-001
- Amends (by addition only; no accepted text is edited):
  - ADR-0026 §5: a sweep for Ilmar's claim label left with no claim behind it;
  - ADR-0026 §2 and §4: clarifies that the tracker is not read while a
    cancellation is pending;
  - ADR-0026 §1, §3 and §6: clarifies what happens to a deleted or
    transferred task;
  - ADR-0020: a second instance-scoped journal operation (schema 6).

## Context and problem

ADR-0026 §5, step 5 removes `claimed_label` after an orphan release only if
no claim of anyone remains, **including a human claim label or assignee**.
That rule is right while the human claim exists: Ilmar must not act beside an
active human claim. But nothing removes the label later.

Once the human lets go of the task, the issue carries `ilmar:claimed` with no
claim comment behind it. The task is then stranded:

- `list_eligible` skips it, because the claim label is present;
- `claim` returns `CLAIMED_BY_OTHER`, because a claim label without a claim
  comment is treated conservatively (ADR-0013);
- `held_claims` never lists it, because no authentic claim remains, so the
  orphan step never sees it.

The review of PR #83 found the same strand in two more ways:

- **Store loss:** a person deleted Ilmar's claim comment, and the store that
  would have released the label is gone.
- **Leftover labels:** labels left by earlier versions, or by a crash before
  #83's `release` fix.

None of this violates a safety invariant. Ilmar does nothing on a stranded
task. But the task never becomes eligible again without someone who knows to
remove Ilmar's label by hand.

The same review recommended two clarifications, where the implementation of
ADR-0026 chose the conservative reading of text that is silent or ambiguous:
reads while a cancellation is pending, and deleted or transferred tasks.

## Considered options

**Removing a stray label:**

1. *Document the manual removal.* Rejected. The task must become eligible
   again on its own once no one claims it.
2. *Change ADR-0026 §5, step 5 to remove the label even beside a human
   claim.* Rejected:
   - it edits an accepted decision;
   - it does not cover store loss or leftover labels, where no orphan claim
     exists for `held_claims` to find.
3. *Let `claim` and `list_eligible` treat a label without a claim comment as
   unclaimed.* Rejected. It weakens ADR-0013's conservative rule for every
   path, including the pick, to fix a cleanup problem.
4. *A sweep for stray labels: a targeted, journaled removal of Ilmar's own
   label where no claim of anyone and no execution backs it.* **Chosen.**
   - It covers every source of the strand.
   - It leaves step 5 and every accepted rule unchanged.
   - It reuses the orphan step's ordering, fencing and revalidation.

## Decision

### 1. The stray-label sweep

**A stray label** is `claimed_label` on an issue in this repository, open or
closed, where:

- **no claim comment of anyone** exists: no marker of any instance, whether
  authentic or forged;
- **no human claim** exists: no human claim label, and no assignee where
  `skip_assigned` makes an assignee a claim (ADR-0013);
- **no execution backs the task** (ADR-0026 §5, rule 3).

**Port additions** (`TaskProvider`):

- `stray_labels() -> Sequence[TaskRef]`: a **read**, added to the
  architecture test's `READS`.
  - It lists the issues carrying `claimed_label`, then reads each issue's
    comments, as `held_claims` does.
  - It returns those with no claim comment of anyone and no human claim.
  - Every call reads afresh, and any uncertainty raises `ProviderError`
    (ADR-0026 §1).
- `clear_label(ref) -> CLEARED | KEPT | ABSENT`: a **targeted write**.
  1. Re-read the issue (labels, assignees) and its comments, without
     following a redirect.
  2. If the issue is gone, or `claimed_label` is absent: `ABSENT`. Nothing is
     written.
  3. If any claim comment or human claim (as defined above) is present:
     `KEPT`. Nothing is written.
  4. Otherwise remove `claimed_label`, and only that label: `CLEARED`.

  It never deletes a comment, and never touches another label or an
  assignee.

**Journal:** a second instance-scoped, repeatable kind, `clear-stray-label`
(the ADR-0020 amendment of ADR-0026, extended).

- **Key:** `clear-stray-label:<instance>:1:<provider>.<digest(task key)>.<n>`,
  where `n` is one more than the number of operations of this kind already
  recorded for the task.
  - An unfinished one is always resumed first, from its recorded intent
    (the task), so `n` is stable across crashes.
  - A label that is cleared, re-added and stranded again later gets a new
    operation.
- **Intent:** the task.
- **Revalidation:** right before the effect, in the later pass, after the
  fence check:
  - nothing in the store backs the task (ADR-0026 §5, rule 3);
  - no execution of the task was created after the intent, by event `seq`.

  A failure is a `LedgerIntegrityError`, and the operation fails. Nothing is
  written to the tracker.
- **Results:**
  - `CLEARED` and `ABSENT` settle the operation as succeeded. `ABSENT` is
    recorded with the observation `claim-label-missing`.
  - `KEPT` also settles as succeeded, with the observation `claim-present`.
    It is never retried as a removal, but a later sweep may record a new
    operation.
- **Reason code:** `stray-label`, added to the ADR-0024 registry.
- **Schema 6:** widens schema 5's check to
  `kind IN ('release-orphan-claim', 'clear-stray-label')` with attempt 1.
  - The table is rebuilt as in schema 5, under the instance lock, with no
    new event baseline.
  - The set of instance-scoped kinds stays closed, and is pinned by the
    architecture test.

**When:** in the orphan step, after the orphan releases, before the
change-request follow-up and the pick. It always takes **two passes**:

1. **Pass N: the intent only.** For each task `stray_labels()` lists, the
   coordinator commits the `clear-stray-label` intent and stops. Nothing is
   written to the tracker in the pass that recorded the intent. The intent
   is the durable first observation.
2. **A later pass: the effect.** Unfinished `clear-stray-label` operations
   are resumed, the same way as after a crash:
   - the revalidation runs;
   - then `clear_label` re-reads the issue and its comments afresh and
     decides.

   The effect never runs in the pass of its own intent. The order of
   passes comes from the store (event `seq` and pass ids), never from a
   clock (ADR-0022 §Time).

**No pick in the pass of the effect.** A task whose label was cleared in a
pass is not picked in that pass.

**The label removal is not atomic.** GitHub has no compare-and-set for
labels or comments (ADR-0013). Between `clear_label`'s final re-read and its
`DELETE` of the label, the issue can change. ADR-0027 does not try to close
that window. It makes every outcome of it safe, and repaired by a fresh
read:

| What happens in the window | Effect of the removal | Why it is safe |
| --- | --- | --- |
| Another instance posts a claim comment | That instance's label goes; its comment stays | That instance reads `HELD` with `label_missing` at its next safe point and restores the label (ADR-0026 §5). Ilmar never acts on the task on the strength of the removal (below). |
| A human adds a claim label, or an assignee where that is a claim | Only Ilmar's label goes | The human claim alone keeps the task out of `list_eligible`, and `claim` and `standing` read it. The sweep never touches it. |
| A human removes a claim label or an assignee | Nothing more | The task stays out until the next fresh `list_eligible` and `claim` read decide. |
| A forged marker is posted | Ilmar's label goes; the forged comment stays | A forged marker is another party's claim (ADR-0026 §6). `claim` backs off from it. |
| A person re-adds `ilmar:claimed`, closes, deletes or transfers the issue | At most Ilmar's label goes | Nothing else is written. A closed or vanished task is not eligible. |
| Ilmar itself claims the task | Cannot happen | Under the instance lock only this coordinator claims, and it does not pick in the orphan step (below). |

**Removing a stray label never implies eligibility.**

- No result of `clear-stray-label` is an input to the pick, to `claim`, or
  to any decision about an execution.
- A task becomes work again only through the ordinary path, each step with
  its own fresh tracker read:
  1. a later pass's `list_eligible`;
  2. ADR-0026 §3's re-pick rules;
  3. `claim`, which re-reads the issue and its comments before posting, and
     backs off if any other claim appears after posting (ADR-0013);
  4. the execution's own standing reads, at the pass start and before the
     new attempt, the agent run, the freeze, each verification run and the
     first push (ADR-0026 §2).

  A claim or human claim hidden by the race, or by delayed visibility, is
  therefore seen before any agent work starts. At the latest, it is seen at
  the next safe point. The task then ends `CANCELLED` with the tracker's
  reason, and is never frozen or published (I12).

**Clearing never interferes with an active execution of this instance.**
The protection comes from the store, never from what GitHub shows:

- **Revalidation:** the effect runs only if no execution backs the task
  (ADR-0026 §5, rule 3) and none was created after the intent. Its own
  claim comment being invisible, delayed, deleted or forged changes
  nothing.
- **Created concurrently:** under the instance lock, only this coordinator
  creates executions. It does so only in the pick, after the orphan step,
  never between a revalidation and its effect.
- **After a store restore:** the restored store decides.
  - **An execution in the restored store backs the task:** the label stays.
  - **One created after the restore:** it is created after the intent, so
    the revalidation refuses the effect.
  - **One lost with the restore:** its process is gone. It is handled as
    store loss: its own claim comment is an orphan for
    `release-orphan-claim`, and its label is not stray while that comment
    exists.
- **A lost claim still in progress** (ADR-0026 §3a) is backed by its
  non-terminal execution. Its label is untouched until the ending.

**Delayed visibility** (ADR-0026 §6):

- **A claim comment or a human claim not yet visible when `stray_labels`
  lists the task:** the effect waits for a later pass, which re-reads
  afresh. Anything visible by then makes the result `KEPT`.
- **One still invisible at that later re-read:** it is a removal raced as in
  the table above, and repaired the same way.
- **A stale listing** (an issue listed by label after the label is gone):
  `clear_label` re-reads the issue itself and settles `ABSENT`.

**I13 ("Claims follow the store") gains one clause.** The sweep removes
`claimed_label` only if, at its final re-check, there was:

- no claim comment of anyone;
- no human claim label or assignee;
- no execution backing the task, under revalidation.

It never removes a comment or another label, never runs in the pass of its
own intent, and no execution is picked or claimed on the strength of it.

### 2. Clarification: no tracker read while a cancellation is pending

ADR-0026 §2's reads at a safe point are skipped once a cancellation (a
person's or the tracker's) is **accepted and not yet ended** for the
execution. The safe point goes straight to ADR-0025's ending.

- A read there could only record a redundant `duplicate`; it cannot decide
  anything.
- ADR-0026 §4's "later ones are recorded `duplicate`" applies to requests
  that arrive. A person's request after a tracker decision is still recorded
  `duplicate`, or `already-terminal` once the ending is done.
- An open lost-claim observation is closed by the ending's terminal
  transition, in the same commit (ADR-0026 §3a, "Reset").

### 3. Clarification: deleted and transferred tasks

- **Never started again automatically.** `task-gone` is not among ADR-0026
  §3's re-pickable reasons. A task cancelled with `task-gone` is not picked
  again, even if the issue reappears, for example when it is transferred
  back. A human resets it, as for `cancel-requested` and `task-claim-lost`.
- **Never followed.** No task-provider read follows a redirect into another
  repository: `standing`, `get`, `claim`, `release`, `held_claims` and
  `stray_labels`.
  - `get` returns `None` and `claim` returns `NOT_ELIGIBLE` for a deleted or
    transferred issue.
  - `release` treats "not found" or "moved" as done.
  - No note is posted on a task that no longer exists.
- **`GONE` needs the repository.** As ADR-0026 §1 already requires, a missing
  or moved issue reads `GONE` only once the repository itself reads back.

## Crash recovery

| Crash point | Recovery |
| --- | --- |
| Before the `clear-stray-label` intent | Nothing is durable. A later pass lists the stray label again and records the intent. |
| After the intent (in its own pass, or in a later pass before revalidation or the effect) | Resumed in a later pass, never in the pass of the intent: revalidated, then `clear_label` re-reads and decides. |
| A store restored to before the intent | The intent is not there. The label is listed again, and the two passes start over. |
| A store restored to after the intent, but before an execution it lacks | The restored store decides: no backing execution and none created after the intent, so the fresh re-read decides. An execution created after the restore is created after the intent, and the revalidation refuses the effect. |
| During or after the label removal, before the outcome | Ambiguous, and settled by repeating it. `clear_label` re-reads and settles `ABSENT` (removed) or `CLEARED`. A claim or human claim that appeared meanwhile makes it `KEPT`. |
| A second crash during that recovery | The same, from the same intent and the same `n`. |
| The schema 6 migration | One lock-held transaction, as for schema 5. A failure changes nothing, and the next claim migrates. |

## Tests the implementation must add

**Contract** (fake and GitHub):

- `stray_labels`:
  - lists exactly label-only issues;
  - never lists one with any claim comment (authentic, another instance's,
    or forged) or with a human claim label or assignee;
  - is read-only;
  - reads afresh;
  - raises on uncertainty.
- `clear_label`:
  - `CLEARED` removes only `claimed_label`;
  - `KEPT` when a claim comment, a human claim label or an assignee is
    present;
  - `ABSENT` when the label or the issue is gone;
  - writes nothing but that one label removal.

**The stranding scenario itself:**

1. An orphan claim beside a human claim: the orphan release keeps the label.
2. While the human claim exists, the sweep never removes it.
3. The human removes their label or assignee. The next pass records the
   intent only; a pass after it re-reads and clears Ilmar's label.
4. The task is not picked in the pass of the removal. It is picked in a
   later pass only if `list_eligible`, the re-pick rules and `claim` allow
   it.

**Concurrent modifications:** a hook acts between the listing, the intent
commit, the revalidation, the re-read and the removal. The changes are:

- a human adds a claim label or an assignee;
- another instance posts a claim;
- a forged marker is posted, naming this or another instance;
- a person removes the label;
- the issue is closed, deleted or transferred;
- an execution of the task is created in the store.

In every case:

- no comment, human label or assignee is touched;
- the label is removed only when the final re-check found nothing;
- the revalidation stops a newly backed task;
- a label lost to the remaining race is restored by the claim's owner.

**The non-atomic removal** (a hook acts between `clear_label`'s final
re-read and its label `DELETE`). For each row of the race table:

- the removal outcome is as stated;
- nothing but Ilmar's label is written;
- the next fresh read repairs it: the other instance restores its label, or
  `claim` or `standing` sees the claim.

**Never eligibility by removal:**

- `CLEARED` is never followed by a pick in the same pass.
- In a later pass the task is picked only through `list_eligible`, the
  re-pick rules and `claim`. A test makes each of those three see a claim
  that the sweep's own re-read missed, and nothing is claimed or started.
- A claim hidden from `claim`'s pre-check but visible to its post-check:
  the instance backs off.
- A claim hidden from both but visible at the pass start or the new-attempt
  safe point: the execution ends `CANCELLED` (`task-claimed-by-other-instance`)
  before any agent run, and I12 holds.

**Concurrent claim creation:**

- another instance posts a claim in each window (listing, intent, the pass
  boundary, revalidation, re-read, removal);
- this coordinator creates an execution of the task between the intent and
  the effect (`LedgerIntegrityError`; nothing written);
- an execution created after a store restore.

**Assignment changes** in each window: an assignee added or removed, with
`skip_assigned` on and off. An assignee that is a claim is never touched,
and keeps the task out. One that is not a claim neither keeps the label nor
makes the task eligible by itself.

**Delayed visibility:**

- a claim comment invisible to `stray_labels` but visible in the later
  pass: `KEPT`;
- one invisible for both passes: the race repair above;
- a human claim label applied but not yet listed: `KEPT` or race repair;
- a stale label listing: `ABSENT`;
- an own execution's claim comment invisible: never swept, because the
  revalidation decides.

**The effect never runs in the pass of its intent,** including when the
intent and the listing both happen during recovery.

**Forged claims:** a forged own marker, a forged other-instance marker, and a
marker by a non-member each keep the label. Removing the forged comment lets
the next sweep clear it.

**Store loss:** a fresh store over a tracker with:

- a label-only leftover: cleared, then picked in a later pass;
- a label beside a human claim: untouched until the human claim goes;
- an authentic own orphan: released by `release-orphan-claim`, then its
  label is handled by the sweep if a human claim kept it.

**Crash recovery:** a crash at every write and effect point of the sweep (the
intent, the revalidation's failure record, the removal, the outcome), and a
second crash during recovery, are added to the crash suite as the scenarios
"stray label after a human claim" and "stray label raced by a new claim".
I11 and the extended I13 are checked at every exit. Restored snapshots
taken before the intent, after the intent, and after the effect each end in
the same tracker state.

**Store:** the schema 6 migration:

- from schema 5 and from earlier schemas;
- rows and events unchanged, triggers and indexes kept;
- a failed migration changes nothing, and is replayed by the next claim;
- only the two instance-scoped kinds may omit the execution.

**Clarification tests:**

- **Pending cancellation:**
  - once a cancellation is accepted, no further standing read happens at any
    safe point;
  - a person's request after a tracker decision is recorded `duplicate`.
- **Deleted and transferred tasks:**
  - a `task-gone` cancellation is never picked again, including after the
    issue is transferred back;
  - no read follows a transfer, for each task-provider method;
  - releasing a vanished task is done.

**Architecture:**

- `clear_label` is called only inside `Effects.clear_stray_label`, through
  `perform_instance` with `clear-stray-label`;
- the instance-scoped kinds are exactly `release-orphan-claim` and
  `clear-stray-label`;
- `stray_labels` is in `READS`.

## Consequences

**Good:**

- No path strands a task behind Ilmar's own label any more. Once no one
  claims a task, it becomes eligible again on its own.
- Active human claims, other instances' claims and forged markers are never
  touched. The sweep removes only Ilmar's label, and only behind a fresh
  re-check and revalidation.
- The two clarifications put the conservative behaviour already implemented
  in #83 into the ADR's own words.

**Bad:**

- **One more tracker listing per pass** (`stray_labels`), plus one re-read
  per stray task.
- **Two passes per removal.** A stray label goes at the earliest in the
  pass after it was listed, and the task is picked in a pass after that.
- **The removal window stays open.** A claim posted in it loses its label
  until its owner's next safe point. That is the same accepted race as
  ADR-0026 §5, and it never leads to work on a claimed task.
- **A third schema migration in this milestone** (schema 6), for one more
  instance-scoped kind.
- **A forged marker keeps a task out of the queue** until a person removes
  it. That is ADR-0013's conservative rule, unchanged.
- **A deleted or transferred task needs a human to start again,** even if it
  comes back.

## Revisit when

- GitHub offers conditional label or comment writes (compare-and-set). The
  remaining label race could then close.
- Ilmar supports several instances per repository (ADR-0013). "No claim
  comment of anyone" would then need ownership by instance.
- A human-reset mechanism for `cancel-requested`, `task-claim-lost` and
  `task-gone` is decided. §3 would then point to it.
