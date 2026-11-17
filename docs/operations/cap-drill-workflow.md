# Design: a one-time, manually dispatched provider cap drill through Ilmar

*Proposed 2026-11-16, revised 2026-11-17 after review. For the maintainer's
decision. Nothing here runs:* the draft workflow is kept in this document,
not under `.github/`, until it is approved and added in its own reviewed PR.

## Purpose

Gate 1 requires one observation that has never been made: a real Anthropic
workspace spend cap refusing Claude Code **inside Ilmar's own coordinator**,
and Ilmar then:

1. records the outcome `provider-refused`;
2. suspends with `provider-cap`;
3. reports it through `ilmar budget` with exit 78;
4. **never retries**: the next pass starts nothing and calls nothing.

Today that is a manual drill (runbook §2, #93). This workflow makes it one
recorded run on the disposable test environment, with **at most USD 0.50**
of real spend. **It replaces no Gate 1 requirement.** It is one way to
perform the cap drill, which the maintainer reviews like every other piece
of Gate 1 evidence. The gate is still recorded only by `ilmar gate record`.

## What is documented and what is not ([R33])

- **Workspace limits:** Console organizations set organization and workspace
  limits, monthly and always with an amount. Amounts are integer cents.
  Setting limits through the API is an early-access preview. Reading them
  goes through the Admin API's spend-limit reads, behind a beta header.
- **Not found: a minimum workspace limit.** So the design never assumes
  USD 0.50 can be set. It **reads** the limit from the provider and stops
  when it is above 50 cents (§3).
- **Not known: how quickly the cap takes effect after spend.** So the
  design's own ceiling never relies on the provider stopping it (§4).

## 1. Isolation

- **A dedicated scratch workspace**, `ilmar-cap-drill`, never `ilmar-test`.
  It has its own model key, created for the drill and deleted after.
- **The disposable test environment of Gate 1:**
  - the `ilmar-disposable` repository;
  - the Gate 1 configuration (`deploy/gate1/ilmar.gate1.toml`), with a fresh
    state directory, the disposable repository's ID and the pinned Claude
    Code 2.1.318;
  - real rootless Podman and the digest-pinned agent image, built as
    `live.yml` builds it.
- **A protected GitHub environment `cap-drill`:**
  - a required reviewer (the maintainer), deployment from `main` only;
  - three secrets, each read by the steps that need it and no others:

| Secret | What it is | Steps that read it |
| --- | --- | --- |
| `CAP_DRILL_MODEL_KEY` | the scratch workspace's model key | §4 and §5 |
| `CAP_DRILL_ADMIN_READ` | an Admin API key used only for reads: spend limits and the cost report; **required** | §3 and §6 |
| `CAP_DRILL_GITHUB_TOKEN` | a fine-grained token for `ilmar-disposable` only, valid for a short time | §5 |

## 2. A one-time, persistent authorization (no secrets needed to check it)

The run proceeds only if all of these hold:

1. **Started by hand** on `main`, at the `expected_sha` the maintainer
   reviewed, with `main` still there.
2. **`authorization`:** a maintainer comment on #93 naming that SHA, the
   workspace and the cap. Its author, text and date are checked.
3. **Once, ever:**
   - any earlier run of this workflow that reached §3 refuses this one;
   - a marker comment `cap-drill: started <run URL>` is posted on #93
     **before** §3, and a second run finds it.

   Nothing retries: no `continue-on-error`, no step retries. A failed or
   cancelled run consumes the authorization.
4. **The environment's reviewer approves.**

## 3. Verified cap before any paid request

Using `CAP_DRILL_ADMIN_READ` only, the run reads from the provider:

- **the workspace's effective monthly limit.** It must exist, be enabled and
  be **≤ 50 cents**;
- **the workspace's month-to-date cost.**

**It stops, making no paid request,** if:
- the read fails or is not understood;
- there is no limit, or the limit is above 50 cents;
- the attested cap (a dispatch input) differs from the read limit;
- the month-to-date cost cannot be read.

That stop is recorded as `cap-unverified` or `cap-too-high`. An attestation
alone never suffices.

## 4. Reaching the cap: tiny direct requests, with a conservative ceiling

The cap must be used up before Ilmar runs, so that Claude Code's very first
request is refused and costs nothing. Using `CAP_DRILL_MODEL_KEY`:

- **The model is the cheapest on an allow-list in the script.** The prompt is
  fixed, `max_tokens` is 8, and requests go one at a time.
- **A worst-case cost bound per request**, computed before sending it: the
  fixed prompt's input tokens counted with the free token-count endpoint,
  plus 8 output tokens, at the attested prices, rounded up.
- **The ceiling.** Before each request, the run requires:

  *month-to-date cost read in §3, plus the estimated spend so far, plus the
  next request's worst case ≤ 50 cents.*

  Otherwise it stops. The provider's limit is at most 50 cents, so it should
  refuse first; if it does not, the ceiling stops the run anyway, as
  `inconclusive`.
- **The first non-success ends §4.**
  - A spend-limit refusal is the expected end.
  - A rate limit, an overload, a timeout or an authentication error is
    `inconclusive`. Nothing is retried, and Ilmar does not run.
- **A hard cap on the number of requests,** derived from the ceiling and the
  worst case, as a second bound.

## 5. The real coordinator, against the disposable environment

With the cap reached, Ilmar runs as it will in Gate 1:

1. Label one trusted issue in `ilmar-disposable` with `ilmar:ready`.
2. **`ilmar run --once --config <gate1 config>`.** Required, from Ilmar's own
   records:
   - the execution's agent outcome is `provider-refused`;
   - the store's suspension is `provider-cap`;
   - the run's exit code is the one the runbook documents for a provider
     refusal;
   - no change request was opened.
3. **`ilmar budget`:** exit **78**, reason `provider-cap`.
4. **No retry.** `ilmar run --once` again: exit 78, **no new execution, no
   new attempt, and no agent container started**, all checked against the
   store's executions and events.
5. Remove the label, and close the issue with a comment linking the run.

Claude Code's own internal retries of a refused request cost nothing. The
record notes how many attempts the agent's stream shows.

## 6. Costs: estimated and actual, reported apart

- **Estimated:** the sum of §4's per-request costs, computed from each
  response's `usage` at the attested prices.
- **Actual:** the provider's month-to-date cost for the workspace, read
  again with `CAP_DRILL_ADMIN_READ` after §5, minus the §3 reading.
  - The cost report may lag. If it does not cover the run yet, the record
    says `actual: pending` with the time of the read.
  - The same read-only command can be run again later, with no paid request,
    to complete the record.
- **The two are never merged into one figure.** A gap between them is
  reported as it is.

## 7. Sanitized evidence

The record holds only:
- the run URL, `expected_sha` and the workspace name;
- the cap as read and as attested, with the time of the read;
- the model ID and the number of §4 requests;
- the estimated cost and the actual cost (or `pending`);
- the final provider status and error `type`, plus a redacted message of at
  most 300 characters;
- the outputs of §5: the outcome, the suspension, the exit codes, and the
  execution and attempt counts before and after;
- the result: `cap-enforced-through-ilmar`, `inconclusive`, `cap-too-high`,
  `cap-unverified` or `refused-before-start`.

It never holds keys or their prefixes, headers, request bodies or
organization IDs. Before it is saved, the writer checks the record against
all three secrets' values and against patterns shaped like keys. It is
uploaded as an artifact and posted on #93.

## 8. Afterwards

- Delete the scratch model key, the Admin read key and the GitHub token.
- Review the record.
- If the result is `cap-enforced-through-ilmar`, the maintainer may tick
  Gate 1's cap-drill item on #93. Nothing else in Gate 1 changes.

## Draft workflow (for review only, not installed)

```yaml
name: Provider cap drill through Ilmar (one time, manual)
on:
  workflow_dispatch:
    inputs:
      expected_sha: {required: true, type: string}
      authorization: {description: "URL of the maintainer's #93 comment", required: true, type: string}
      attested_cap_cents: {required: true, type: number}
      model: {required: true, type: string}
      issue: {description: "trusted ilmar-disposable issue number", required: true, type: number}
permissions: {contents: read}
concurrency: {group: cap-drill, cancel-in-progress: false}
jobs:
  guard:      # §2, no secrets
    if: github.ref == 'refs/heads/main'
    runs-on: ubuntu-24.04
    permissions: {contents: read, issues: write, actions: read}
    steps:
      - uses: actions/checkout@<pinned SHA>
      - run: python3 scripts/cap_drill.py guard
      - run: python3 scripts/cap_drill.py mark
  prepare:    # as live.yml: hash-locked wheels, the digest-pinned agent image
    needs: guard
    runs-on: ubuntu-24.04
    steps: ["<as live.yml's prepare job>"]
  drill:
    needs: prepare
    runs-on: ubuntu-24.04
    environment: cap-drill
    timeout-minutes: 30
    steps:
      - uses: actions/checkout@<pinned SHA>
        with: {ref: "${{ inputs.expected_sha }}"}
      - name: Verify the cap (§3)
        env: {CAP_DRILL_ADMIN_READ: "${{ secrets.CAP_DRILL_ADMIN_READ }}"}
        run: python3 scripts/cap_drill.py verify-cap --max-cents 50 --state cap.json
      - name: Reach the cap (§4)
        env: {CAP_DRILL_MODEL_KEY: "${{ secrets.CAP_DRILL_MODEL_KEY }}"}
        run: python3 scripts/cap_drill.py exhaust --state cap.json
      - name: Ilmar's coordinator (§5)
        env:
          CAP_DRILL_MODEL_KEY: "${{ secrets.CAP_DRILL_MODEL_KEY }}"
          CAP_DRILL_GITHUB_TOKEN: "${{ secrets.CAP_DRILL_GITHUB_TOKEN }}"
        run: python3 scripts/cap_drill.py through-ilmar --state cap.json
      - name: Actual cost (§6)
        env: {CAP_DRILL_ADMIN_READ: "${{ secrets.CAP_DRILL_ADMIN_READ }}"}
        run: python3 scripts/cap_drill.py actual-cost --state cap.json --record cap-drill.json
      - uses: actions/upload-artifact@<pinned SHA>
        if: always()
        with: {name: cap-drill-record, path: cap-drill.json}
```

Each step runs only if the one before it succeeded, so a stop in §3 means
§4 and §5 never run.

## Decisions for the maintainer

1. Approve the design. The script, its offline tests (against a fake
   provider and a fake Admin API) and the workflow follow in one sensitive
   PR.
2. Create the Admin read key. It is **required** now: without a verified cap,
   the drill does not spend.
3. Choose the model.
