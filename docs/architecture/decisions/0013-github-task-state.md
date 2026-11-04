# ADR-0013: GitHub task state: labels for visibility, claim comments for ownership

- Status: Proposed
- Date: 2026-11-04
- Sources: [R1], [R11], [R20] · Findings: F-015

## Context and problem

The GitHub `TaskProvider` (#6) must:
- discover eligible issues;
- claim one without duplicate execution (MANIFEST §5 step 2);
- release it;
- respect work humans have claimed.

It may use the REST API only, because GraphQL, and with it Projects v2, is
unavailable in some environments Ilmar runs in. GitHub has no compare-and-set
for labels or assignees, so two Ilmar instances could race for the same issue.
ADR-0009's "Revisit when" asks for the labels to be aligned with this state
model.

## Considered options

1. **Assignee as the claim.** Assignment is not atomic either, and all
   instances usually share one bot account, so instances cannot be told apart.
2. **One `state:<name>` label per issue** ([R11]). Visible, but a label carries
   no owner identity and cannot resolve a race.
3. **A Projects v2 status field.** GraphQL only, so it is ruled out.
4. **Labels for visibility, plus claim comments carrying an instance marker
   for ownership.** Races are resolved by comment order, which GitHub assigns.

## Decision

Chosen option: 4.

- **Eligible:**
  - an open issue, not a pull request (the issues API returns both);
  - with `eligible_label` (default `ilmar:ready`);
  - with none of `excluded_labels`;
  - without `claimed_label` (default `ilmar:claimed`);
  - with no assignees (`skip_assigned`, on by default).
- **Claim:**
  1. Post a comment with `<!-- ilmar:claim instance=<id> -->` and add
     `claimed_label`.
  2. Re-read the comments: the **earliest** active claim wins.
  3. The loser deletes its own comment and gets `CLAIMED_BY_OTHER`.

  Claiming again as the holder is idempotent.
- **Claimed by someone else:** `claimed_label` without our marker (a human, or
  a session following `docs/workflow.md`), another instance's claim comment, or
  an assignee.
- **Release:** delete only our own claim comments. Remove `claimed_label` only
  if no other claim remains. Never touch a claim we do not hold.
- **Alignment with ADR-0009:** for Ilmar's own repository,
  `claimed_label = "status:in-progress"`, the label sessions already use, and
  `excluded_labels` include `needs:maintainer`, `status:blocked` and
  `proposal`. Human sessions and Ilmar then see the same claims
  (`ilmar.example.toml`).
- **Transport:** a standard-library REST client. The token comes from
  `token_env`. The client sends the token only to the configured HTTPS API
  host, refuses pagination links to other hosts, and keeps the token out of
  error messages.

## Consequences

- Good: no GraphQL and no extra dependency. Claims are visible to humans and
  carry an owner. Concurrent instances converge on one owner. Human claims are
  respected.
- Bad:
  - A claim costs several REST calls.
  - A claim comment left by a crashed instance blocks the issue until it is
    reconciled (#12) or a human removes it.
  - Anyone with comment access could forge a claim marker. That can only make
    Ilmar step back, or take a claim it would otherwise race for, so it never
    leads to duplicate work by different instances.

## Revisit when

- Reconciliation (#12) needs to expire stale claims, for example by age plus
  liveness.
- GitHub offers conditional (compare-and-set) label or assignee updates over
  REST.
- A tracker that supports atomic transitions (#23) shows whether this
  protocol should become a shared helper.
