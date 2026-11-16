# Design: a one-time, manually dispatched provider cap-enforcement workflow

*Proposed 2026-11-16, for the maintainer's decision. Nothing here runs:* the
draft workflow below is kept in this document, not under `.github/`, until it
is approved and added in its own reviewed PR.

## Purpose

Gate 1 needs one observation that has never been made: the Anthropic
workspace spend cap refusing a request, and Ilmar classifying that refusal as
`provider-refused` and then suspending with `provider-cap` (runbook §2,
#93). Today that is a manual drill. This workflow makes it one recorded,
reproducible run, with a hard ceiling of **USD 0.50** of real spend.

## Facts it relies on, and what is not known ([R33])

- **The documented representation.** Console organizations set organization
  and workspace limits, which are monthly and always carry an amount. A limit
  is an integer number of cents. Setting them through the API is an
  early-access preview. A read API for limits exists behind a beta header.
- **Unknown: a minimum.** No documented minimum workspace limit was found, so
  the design does not assume that USD 0.50 can be set. If the Console will
  not accept USD 0.50 or less, **the drill stops before any paid request**
  (§3).
- **Unknown: how quickly the cap takes effect.** Enforcement may lag spend.
  The design therefore keeps its own spend ceiling and never relies on the
  provider stopping it in time (§4).

## 1. Isolation

- **A dedicated scratch workspace**, `ilmar-cap-drill`, never `ilmar-test`.
  It has its own key, created for the drill and deleted after it.
- **A GitHub environment `cap-drill`:**
  - a required reviewer (the maintainer) and no self-review bypass;
  - deployment only from `main`;
  - one secret, `CAP_DRILL_KEY`, used in one step only.
- **Optional read access:** a second secret, `CAP_DRILL_ADMIN_READ`, an Admin
  API key used only to *read* the workspace's limit. With it, the limit is
  checked against the provider, not only against the person's word (§3).
  Without it, the run relies on the attestation, and the record says so.

## 2. A one-time, persistent authorization

The run proceeds only if all of these hold, checked without secrets:

1. **Started by hand** (`workflow_dispatch`) on `main`, at the `expected_sha`
   the maintainer reviewed, with `main` still pointing there.
2. **`authorization`:** the URL of a maintainer comment on #93 that names
   `expected_sha`, the workspace and the attested limit. The guard fetches
   it and checks its author, its text and that it is newer than the commit.
3. **Once, ever.** Two durable records enforce this, checked before the
   environment is entered:
   - the workflow's own run history: any earlier run that reached the paid
     step, whatever its result, refuses this one;
   - a marker comment the workflow writes on #93 *before* the paid step:
     `cap-drill: started <run URL>`. A second run finds it and refuses.

   **No automatic retries:** `continue-on-error` is never used, no step
   retries, and a failed or cancelled run consumes the authorization. A
   second drill needs a new design decision, not a re-run.
4. **The environment's required reviewer approves the run** in GitHub.

## 3. Before any paid request: the cap must be ≤ USD 0.50

- **The attestation:** the dispatch input `attested_cap_cents` must be ≤ 50.
- **The provider's own record, if the read key is present:** the effective
  workspace limit is read through the Admin API.
  - The run stops when the read fails, when the limit is above 50 cents or
    when no limit is set.
  - The run also stops when the read value differs from the attestation.
- **If the Console would not accept 50 cents or less**, the maintainer
  attests the real figure. The guard then stops with the outcome
  `cap-too-high: no paid request made`, and that outcome is the record.

## 4. The drill itself, with its own ceiling

- **The model is pinned:** the cheapest current model, its ID given as an
  input and checked against an allow-list. `max_tokens` is small, and the
  prompt is fixed.
- **One request at a time, sequentially.** Each response's `usage` is priced
  at attested prices. The run keeps a running total and **stops itself
  before the next request would take the total above USD 0.50**, whatever
  the provider does.
- **The run stops at the first response that is not a success.**
  - A spend-limit refusal is the expected outcome.
  - A rate limit, an overload, a timeout or anything else is recorded as
    `inconclusive`, never retried, and not turned into a pass.
- **A hard cap on the number of requests,** derived from the price and the
  ceiling, as a second bound.
- **Then Ilmar's own classification:** the refusal is fed to Ilmar's
  classifier offline, from the recorded status and error type, with no
  further request. The record states whether Ilmar names it
  `provider-refused` and maps it to the `provider-cap` suspension.

## 5. Sanitized evidence

The record keeps only:
- the run URL and `expected_sha`;
- the workspace name;
- the attested cap, plus the cap read from the provider and its source;
- the model ID and the number of requests;
- the total spend Ilmar computed;
- the final HTTP status and the error `type`;
- the error `message`, after redaction, at most 300 characters;
- Ilmar's classification;
- the outcome: `cap-enforced`, `inconclusive`, `cap-too-high` or
  `refused-before-start`.

It never holds:
- a key or its prefix;
- request or response headers;
- request bodies;
- organization IDs.

The writer checks the record against both secrets' values, and against
patterns shaped like keys, before saving it. It is uploaded as an artifact
and posted on #93 as a comment.

## 6. Afterwards

- **Delete the scratch key** and the environment's secrets.
- **Review the record**, and confirm the refusal text on #93.
- **Gate 1's manual cap drill is then satisfied by this record**, if the
  maintainer agrees. The gate itself is still recorded only by `ilmar gate
  record`.

## Draft workflow (for review only, not installed)

```yaml
name: Provider cap drill (one time, manual)
on:
  workflow_dispatch:
    inputs:
      expected_sha: {required: true, type: string}
      authorization: {description: "URL of the maintainer's #93 comment", required: true, type: string}
      attested_cap_cents: {required: true, type: number}
      model: {required: true, type: string}
permissions: {contents: read}
concurrency: {group: cap-drill, cancel-in-progress: false}
jobs:
  guard:            # no secrets: §2 and the attestation part of §3
    if: github.ref == 'refs/heads/main'
    runs-on: ubuntu-24.04
    permissions: {contents: read, issues: write, actions: read}
    steps:
      - uses: actions/checkout@<pinned SHA>
      - run: python3 scripts/cap_drill.py guard   # refuses: wrong ref or SHA, bad
          # authorization, an earlier paid run or marker, a cap above 50 cents
      - run: python3 scripts/cap_drill.py mark    # the #93 marker, before the paid job
  drill:
    needs: guard
    runs-on: ubuntu-24.04
    environment: cap-drill                        # required reviewer
    timeout-minutes: 15
    steps:
      - uses: actions/checkout@<pinned SHA>
        with: {ref: "${{ inputs.expected_sha }}"}
      - name: Read the workspace limit (optional) and drill
        env:
          CAP_DRILL_KEY: ${{ secrets.CAP_DRILL_KEY }}
          CAP_DRILL_ADMIN_READ: ${{ secrets.CAP_DRILL_ADMIN_READ }}
        run: python3 scripts/cap_drill.py drill --ceiling-cents 50 --record cap-drill.json
      - uses: actions/upload-artifact@<pinned SHA>
        if: always()
        with: {name: cap-drill-record, path: cap-drill.json}
```

## Decisions for the maintainer

1. Approve this design, so that `scripts/cap_drill.py`, its offline tests and
   the workflow come in one sensitive PR.
2. Whether to provide the optional Admin read key. It is recommended: it
   turns the cap from an attestation into an observation.
3. The model to use. The drill takes the ID as an input, checked against an
   allow-list kept in the script.
