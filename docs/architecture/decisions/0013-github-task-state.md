# ADR-0013: GitHub task state under a single-coordinator deployment

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
unavailable in some environments Ilmar runs in.

GitHub REST has no compare-and-set for labels, assignees or comments. Any claim
built from them is check-then-act: two writers can both pass the check before
either writes. A first draft of this ADR resolved races by "earliest comment
wins". Review of PR #38 showed that this is not an exclusive lock:
- reads after a write are not guaranteed to be consistent;
- a loser that crashes before withdrawing leaves two claims;
- an instance does not know whether a claim it sees is still backed by a live
  execution.

So the question is not "which lock?" but "which threat model does M2 have to
meet?".

ADR-0009's "Revisit when" also asks for the labels to be aligned with this
state model.

## Threat model

**In scope.** One Ilmar instance per repository, and one coordinator process
per instance. ADR-0012 already enforces the second half with the instance OS
lock, held for the coordinator's lifetime.

Within that deployment, duplicate execution is prevented by the coordinator's
durable store, not by GitHub:
- the in-process execution lease (ADR-0012);
- the write-ahead execution record;
- reconciliation (#12, #15).

GitHub state is a projection of that store, made visible to humans.

Other actors are also in scope. Ilmar must not take work that is visibly
claimed by:
- human maintainers, or sessions following `docs/workflow.md`, who use labels
  and assignees;
- a second, misconfigured Ilmar instance.

**Out of scope for M2:** several instances deliberately sharing one repository.
The adapter **detects** a second instance and **backs off**, but it does not
guarantee mutual exclusion between instances. A deployment with several
instances per repository needs an authoritative lease (see Revisit when) and a
new ADR.

## Considered options

1. **Comments as an authoritative lock** (the first draft). Rejected: there is
   no atomicity, and the outcome depends on read-after-write consistency.
2. **Assignee as the claim.** Assignment is not atomic either, and instances
   usually share one bot account, so instances cannot be told apart.
   Assignment also signals *human* responsibility.
3. **A Projects v2 status field.** GraphQL only, so it is ruled out.
4. **A git ref as a lease.** Create a ref such as `refs/ilmar/claims/<issue>`;
   creating a ref that already exists fails. This may be the only atomic
   create-if-absent primitive in GitHub REST. It is Experimental: unverified,
   it needs `contents: write`, and it adds cleanup and expiry problems.
   It is deferred until several instances are in scope.
5. **Single-coordinator constraint, with GitHub as a visible projection.**
   Each GitHub mechanism has one meaning:
   - labels are workflow state;
   - assignees are human responsibility;
   - comments are an audit record.

   Cross-instance claims are detected and Ilmar backs off.

## Decision

Chosen option: 5.

- **Meaning of each mechanism:**
  - *Labels* carry workflow state: `eligible_label` (default `ilmar:ready`),
    `claimed_label` (default `ilmar:claimed`), `human_claim_labels` and
    `excluded_labels`.
  - *Label ownership.* A label records no author, so a shared label could not
    tell Ilmar whether it may remove it. The rule is therefore conservative:
    `claimed_label` is **owned by Ilmar**. It is the only label Ilmar ever adds
    or removes, and humans never use it. Humans claim work with their own
    labels (`human_claim_labels`, e.g. `status:in-progress`) or by assigning
    someone. Ilmar reads those as "claimed by someone else" and never adds,
    removes or reinterprets them. The configuration rejects a `claimed_label`
    that is also a human claim or excluded label.
  - *Assignees* mean a human is responsible. Ilmar never assigns anyone, and
    with `skip_assigned` (on by default) it treats any assignee as "claimed by
    someone else".
  - *Claim comments* are an audit record, not a lock. They carry
    `<!-- ilmar:claim instance=<id> token=<128-bit random hex> -->`. The
    instance identifies the owner. The token identifies this particular claim,
    so audits and reconciliation can tell one claim from another, including a
    re-claim after a release.
- **Eligible:**
  - an open issue, not a pull request (the issues API returns both);
  - with `eligible_label`;
  - with none of `excluded_labels`;
  - without `claimed_label`;
  - with no assignees and none of `human_claim_labels`.
- **Claim:**
  1. Read the issue and its comments. If another instance's claim, an
     assignee or a human claim label is present, the result is
     `CLAIMED_BY_OTHER`, even when we also hold a claim (a human started
     while we held it). This check has no side effects. If only our own
     instance's claim is present, re-apply the label and return `CLAIMED`.
     If `claimed_label` is present without a claim comment, it is a leftover.
     The result is conservatively `CLAIMED_BY_OTHER` until reconciliation or
     a human clears it.
  2. Otherwise, post our claim comment and add `claimed_label`.
  3. Re-read the comments. If **any** other instance's claim is visible,
     withdraw ours and return `CLAIMED_BY_OTHER`. Both sides of an overlap
     therefore back off. Nobody "wins", and the issue becomes eligible again.
     That is the safe outcome for what is, by this ADR, a misconfiguration.
- **Ownership is per instance, and recovery runs through the durable store.**
  A restarted coordinator of the same instance recognises its own claims.
  Whether a claim is still *backed* is decided by the store, not by GitHub.
  Reconciliation (#12, #15) must:
  - release claims held by this instance that have no non-terminal execution
    record;
  - re-apply missing labels for claims that are backed.
- **Release:** delete only our own instance's claim comments. Remove
  `claimed_label` only if no claim remains. Never touch a claim held by a human
  or another instance. Human claim labels survive every release and back-off.
- **Alignment with ADR-0009:** for Ilmar's own repository,
  `claimed_label = "ilmar:claimed"` and
  `human_claim_labels = ["status:in-progress"]`, the label sessions already
  use. `excluded_labels` include `needs:maintainer`, `status:blocked` and
  `proposal` (`ilmar.example.toml`). `docs/workflow.md` tells sessions to
  treat `ilmar:claimed` as in progress and never to write it.
- **Transport credential boundary:** a standard-library REST client.
  - The token comes from `token_env`.
  - Only `https` API URLs and non-empty tokens are accepted.
  - Pagination links to another host are refused.
  - Redirects are followed only within the same origin (scheme, host and
    port), at most three times. A redirect to any other origin is refused
    before a request is sent, so credentials never leave the API origin.
  - Error messages never contain the token.

## Verification

- The shared `TaskProviderContract` runs against the adapter over an in-memory
  REST fake with pagination.
- A controlled overlapping schedule forces both instances past the initial
  check before either posts. Both must back off, the label must be cleaned up,
  and the issue must be eligible again (`tests/unit/test_github_tasks.py`).
- Human label ownership: a human adds `status:in-progress` while Ilmar holds
  a claim. Releasing, and the overlap back-off path, leave it in place, and
  across a whole cycle Ilmar writes no label other than `claimed_label`.
- Redirect regression tests run against the real `urllib` transport with
  local servers. A cross-origin 302 is refused and the other origin receives
  no request at all. A same-origin redirect is followed with credentials
  (`tests/unit/test_github_client.py`).
- An opt-in live test (`pytest -m integration`) exercises a real repository.

## Consequences

- Good:
  - No GraphQL and no extra dependency.
  - Each GitHub mechanism has one meaning that humans can read.
  - Humans' claims are respected.
  - The guarantee Ilmar relies on comes from its own store, which it controls.
- Bad:
  - Running two instances against one repository is unsupported. Overlapping
    claims back off, but detection depends on GitHub's read consistency, so
    even detection is not guaranteed. Configuration documentation must say
    this.
  - Ilmar's claims and humans' claims use different labels, so there are two
    "in progress" labels to look for. A human who applies `ilmar:claimed`
    anyway is overriding the documented rule, and Ilmar may remove that label.
  - A claim costs several REST calls.
  - A claim left by a crashed instance blocks the issue until reconciliation
    or a human removes it.
  - On a public repository anyone can post a comment containing a claim
    marker. That can only make Ilmar step back (denial of progress, never
    duplicate work), and a maintainer can delete the comment. Filtering
    markers by comment author is a possible hardening (Revisit when).

## Revisit when

- More than one instance per repository is required. Evaluate an
  authoritative lease, for example git-ref create-if-absent (option 4), with
  expiry and fencing. That needs a new ADR.
- Forged claim markers cause real disruption. Then count only markers from the
  instance's own bot login, or from trusted author associations.
- GitHub offers conditional (compare-and-set) label or assignee updates over
  REST.
- A tracker that supports atomic transitions (#23) shows whether this
  protocol should become a shared helper.
