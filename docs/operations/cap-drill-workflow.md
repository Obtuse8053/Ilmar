# The one-time provider cap drill through Ilmar

*Designed 2026-11-16; revised 2026-11-17: the scratch workspace
`ilmar-cap-test` keeps its own USD 1.00 monthly limit, inside an independent
USD 1.15 test allowance.* The workflow is `.github/workflows/cap-drill.yml`,
the script is `scripts/cap_drill.py`, and `tests/unit/test_cap_drill.py` tests
both offline. **Nothing runs until the maintainer dispatches it, after
authorizing it on #93.**

## Purpose

Gate 1 requires one observation that has never been made: a real Anthropic
workspace spend limit refusing Claude Code **inside Ilmar's own
coordinator**, and Ilmar then:

1. records the outcome `provider-refused`;
2. suspends with `provider-cap`;
3. reports it through `ilmar budget` with exit 78;
4. **never retries**: the next pass starts nothing.

**It replaces no Gate 1 requirement.** It is one recorded way to perform the
cap drill (runbook §2), reviewed like all Gate 1 evidence. The gate is still
recorded only by `ilmar gate record`.

## What the provider documents, and what it does not ([R33])

| Need | Documented means | Caveat |
| --- | --- | --- |
| Which workspace a key belongs to | Claude API responses carry an `anthropic-workspace-id` header ([R35]). The Admin API's key list gives each key's `workspace_id` and a `partial_key_hint` | The workspace's ID comes from its name only through the Admin API. |
| The workspace's spend limit | Admin API: `GET /v1/organizations/spend_limits?scope_type=workspace&workspace_id=…`, beta header `spend-limit-reads-2026-09-26`; amounts in cents | Listing Claude Console limits is an **early-access preview**. The Spend Limits guide calls the API Enterprise-only. Without access the read fails and the drill stops. |
| Month-to-date cost | Admin API cost report, grouped by workspace; amounts in cents | It can lag behind real spend. |
| A minimum workspace limit | Not documented | |
| How fast a limit takes effect, and whether a request may overshoot it | Not documented | So the drill keeps headroom (§3). |
| A read-only Admin key | Not offered to Console organizations: every Admin key has full access | So `scripts/cap_drill.py` calls only `GET` on four Admin paths (tested), and the key is deleted after the run. |

## 1. Isolation

- **The scratch workspace `ilmar-cap-test`**, never `ilmar-test`. It has a
  USD 1.00 monthly limit and showed USD 0.00 spent on 2026-11-17.
- **The disposable test environment of Gate 1**:
  - `ilmar-disposable`;
  - the Gate 1 configuration (`deploy/gate1/ilmar.gate1.toml`), changed only
    where the drill must differ (below);
  - Claude Code 2.1.318;
  - rootless Podman and the digest-pinned agent image, prepared as `live.yml`
    prepares it.
- **A protected GitHub environment `cap-drill`**, configured like `live`:
  - a required reviewer with self-review prevented;
  - no admin bypass;
  - deployment from `main` only.

  It holds three secrets, each given only to the steps that need it:

| Secret | What it is | Steps |
| --- | --- | --- |
| `CAP_DRILL_KEY` | the `ilmar-cap-test` key | Preflight (one free token count; compared locally with the Admin API's hints, never sent there); Reach the cap; Ilmar's coordinator |
| `CAP_DRILL_ADMIN_READ` | an Admin API key, used only for reads | Preflight; Actual cost |
| `CAP_DRILL_GITHUB_TOKEN` | a fine-grained token for `ilmar-disposable` only, valid for a short time | Ilmar's coordinator |

## 2. One-time authorization

The `guard` job, which has no secrets, refuses unless all of these hold:

- **Started by hand:** the run was dispatched manually on `main`, at
  `expected_sha`, and `main` still points there.
- **The environment:** `cap-drill` and `main`'s rules are as described in §1.
- **The authorization:** a comment on #93, by the maintainer (`OWNER`), never
  edited, at most seven days old, containing the line

      cap-drill: authorize <expected_sha> workspace=ilmar-cap-test allowance=1.15

- **Once:** no `cap-drill: started` marker exists on #93, and no earlier run
  of the workflow started its Mark step or a paid step.

**The authorization is used only after the free preflight passes.** In the
`drill` job, the preflight (§3) runs first and makes no paid request. Only
if it passes does the Mark step check again that no marker exists, post
`cap-drill: started <run URL>` on #93, and confirm that it is the only
marker. The paid steps refuse to run without this run's marker.

So a run that stops in the preflight, for example because the organization
has no spend-limit read access, leaves the authorization unused. It can be
dispatched again once the cause is fixed.

- **No retries.** There is no `continue-on-error` and no retry in the code.
- **Once the marker is posted,** a failed or cancelled run has used up the
  authorization.

## 3. Preflight: free, before the authorization is used

With Admin API `GET` requests and one free token count, the preflight
establishes:

1. **The workspace:** exactly one active workspace named `ilmar-cap-test`.
2. **The key's binding, two ways:**
   - a free token-count call with `CAP_DRILL_KEY`, sent without an
     `anthropic-workspace-id` request header, must return that workspace's ID
     in its `anthropic-workspace-id` response header. A missing header, or
     another workspace (as an unscoped key would report), fails closed;
   - exactly one active key in the organization has a hint that fits
     `CAP_DRILL_KEY`, and the Admin API lists it in `ilmar-cap-test`.
3. **Spend-limit read access and the limit:** an enabled monthly USD limit on
   that workspace, read from the Spend Limits API. Without access, the run
   stops here with `cap-unverified` and the authorization stays unused.
   **Only enabled, monthly, USD workspace limits are accepted**, and the
   lowest of them counts:
   - a limit of another period (daily, weekly) is never taken for the
     monthly cap, however low;
   - a monthly limit that is disabled, or whose `is_enabled` is not exactly
     `true`, is not accepted;
   - a monthly limit that is not a whole number of USD cents stops the run.

   With no accepted limit, the run stops. The accepted limit must be **at
   most USD 1.00** (it is USD 1.00) and must equal the attested cap (a
   dispatch input).
4. **The month-to-date cost** from the cost report.
5. **Headroom inside the USD 1.15 allowance.** The limit can only be reached
   by a request that crosses it, and how far a request may overshoot is not
   documented. Therefore:

   ```
   limit + the largest single request + Ilmar's reservation  ≤  allowance
   USD 1.00 + 5¢ + 10¢ = USD 1.15  ≤  USD 1.15
   ```

   A lower allowance, or a limit above USD 1.00, stops with `no-headroom` or
   `cap-too-high`.

**Any failure stops the run with no paid request:** `cap-unverified`,
`cap-too-high` or `no-headroom`. The stop is recorded with the missing
requirement. An attestation alone never suffices.

## 4. Reaching the cap

With `CAP_DRILL_KEY`:

- **The model** is on the script's reviewed allow-list, at Anthropic's
  published prices ([R36]). Today that is only Claude Haiku 4.5
  (`claude-haiku-4-5`), at USD 1 / USD 5 per million input / output tokens,
  one rate at every prompt length. About 25 requests reach a USD 1.00 limit.
  Claude Code uses the same model inside Ilmar.
- **The prompt.** A fixed prompt is sized with the free token-count endpoint.
  Its worst case, the counted input plus 8 output tokens at those prices,
  × 1.25, is at most 5¢.
- **One request at a time:** `max_tokens` 8, never retried, at most 60
  requests.
- **Probes past the limit.** Once the estimated month-to-date spend has
  reached the limit, only a tiny fixed probe is sent, whose worst case is a
  fraction of a cent. The provider refuses the probe, not a 5¢ request. So
  provider spend stays within the limit plus one sized request (at most
  USD 1.05). The request cap (60) bounds the probes if the provider keeps
  accepting them.
- **The ceiling.** Before each request:

  *month-to-date cost read in §3 + estimated spend so far + the next
  request's worst case + Ilmar's 10¢ reservation ≤ USD 1.15*

  Otherwise the request is never sent, and the run ends `inconclusive`.
- **Spending and accounting are kept apart.** In the ceiling and in the
  record:
  - **provider spending** is the month-to-date cost and each sent request's
    estimated cost;
  - **accounting reservations** are the next request's worst case before it
    is sent, and the 10¢ held for Ilmar's attempt (also charged in Ilmar's
    own ledger as `reserve_per_execution_usd`).

  A reservation is never reported as provider spend.
- **The first non-success ends this step.**
  - A refusal that Ilmar's own classifier calls a spend limit is the expected
    end.
  - Anything else (a rate limit, an overload, an authentication error or a
    timeout) is `inconclusive`, and Ilmar does not run.

## 5. Ilmar's own coordinator

Ilmar runs only after a direct spend-limit refusal. Its configuration is the
Gate 1 file with exactly these changes, each checked:

- **Paths:** a fresh state directory, store and kill-switch file;
- **Instance:** `instance_id = "cap-drill"`;
- **Repository:** `ilmar-disposable`'s ID;
- **Credentials:** the two secret variable names;
- **The agent:** `max_turns = 1` and the drill's model;
- **The budget:** the limit as read, and the 10¢ reserve.

`ilmar config validate --check-environment` must pass. Then:

1. Label the maintainer's open, unlabelled issue in `ilmar-disposable`
   `ilmar:ready`.
2. Run **`ilmar run --once`**. Ilmar's own event log must show:
   - one execution;
   - one attempt;
   - the execution ended `provider-refused`;
   - one `provider-cap` suspension.
3. Run **`ilmar budget`**: it must exit 78 and name `provider-cap`.
4. Run **`ilmar run --once`** again. It must exit 78, with:
   - no new execution, attempt or suspension in the log;
   - no new Ilmar container.
5. Afterwards:
   - no new change request exists on `ilmar-disposable`;
   - the labels are removed, always;
   - the issue is closed with a link to the run.

The Admin key never reaches Ilmar's environment.

## 6. Costs: estimated and actual, reported apart

- **Estimated:** each successful request's `usage` at the allow-listed prices.
- **Actual:** the cost report's month-to-date cost after the run, minus the
  preflight's reading. If the report has not caught up, the actual cost reads
  `pending` with the time of the read. The figure can be completed later by
  reading the cost report again, with no paid request.

The two are never merged into one figure.

**Spend verification.** The provider may enforce limits with delayed usage
accounting, and no client-side estimate can guarantee the final bill. The
record states:

- `within-allowance`: the cost report is **reconciled against every sent
  request**, and the month-to-date provider spend stays within USD 1.15.
  Reconciled means its rise since the preflight is at least 99 % of the
  estimated cost of all successful requests (1 % for rounding). Spend above
  the USD 1.00 limit by at most one request is expected, and is shown as
  `above_provider_limit`;
- `overshoot`: the cost report already shows more than USD 1.15. This is
  final, because a fuller report can only show more;
- `pending`: anything short of reconciled, so a partly updated report is
  never read as the total. That covers no rise, a fall, or a rise below 99 %
  of the estimate. The record then gives the reported rise and its
  reconciled share, and the actual cost reads `pending`.

Only `within-allowance` can make the result `cap-enforced-through-ilmar`.
Otherwise the result is `inconclusive`, never a successful verification at
or below USD 1.15. A later read-only cost read can settle a `pending` record
for review.

## 7. Sanitized record

`cap-drill-record.json` is uploaded as an artifact and posted on #93. It
holds:

- the run, `expected_sha` and the workspace name;
- the allowance, the limit as read (which equals the attested cap), the
  month-to-date cost before the run and the time of the read;
- the model, its allow-listed prices and the number of requests;
- **provider spending:** estimated and actual, kept apart;
- **accounting reservations:** the drill's reserve for Ilmar, and Ilmar's
  ledger reservations, labelled as not provider spend;
- the provider's final status, error `type` and redacted message (at most
  300 characters);
- the outputs and checks of §5;
- the stop reason, if any;
- the result:
  - `cap-enforced-through-ilmar`;
  - `ilmar-check-failed` (the cap refused, but Ilmar did not behave as
    required);
  - `inconclusive` (including any run whose spend is `pending` or
    `overshoot`);
  - `cap-too-high`;
  - `no-headroom`;
  - `cap-unverified`.

It never holds a key, a key's hint, a header, a request body or an
organization or workspace ID.

**Redaction.** Each step redacts the secrets it holds, and anything shaped
like a credential, from everything it keeps. A step only ever sees responses
to requests made with its own secrets. The record is checked again before it
is written and before it is posted.

## 8. Afterwards

- Delete `CAP_DRILL_ADMIN_READ` and `CAP_DRILL_GITHUB_TOKEN`. Delete
  `CAP_DRILL_KEY` too, or archive the key.
- Review the record.
- If the result is `cap-enforced-through-ilmar`, the maintainer may tick
  Gate 1's cap-drill item on #93. Nothing else in Gate 1 changes.
