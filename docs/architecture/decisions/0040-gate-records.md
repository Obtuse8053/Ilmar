# ADR-0040: Gate records bound to version-pinned evidence and an immutable target

- Status: Proposed
- Date: 2026-11-14
- Sources: none external · Findings: none (a design decision, not a research finding)
- Amends (by addition; no accepted text is edited):
  - **ADR-0030 §2:** what a gate record holds and how it is stored, written
    and revoked.
  - **ADR-0029 §8:** schema 9 is the `gate_record` table (as reserved there).
  - **ADR-0024 §4:** two new event types, `instance.gate_recorded` and
    `instance.gate_revoked`, and reason codes `gate-recorded` and
    `gate-revoked`.
- Asked for by the maintainer on 2026-11-14 (#91, part 2): durable
  record/revoke/show, version-bound evidence, explicit revocation, and the
  approved target bound to its immutable GitHub repository ID.

## Context and problem

ADR-0030 makes the loop depend on an unrevoked gate record, but leaves its
contents open. Two later decisions constrain them:

- ADR-0035's hard gate (maintainer, 2026-11-11): the evidence must be for one
  exact agent version, with a passed pinned-version live test and a reviewed
  `provenance: recorded` stream fixture for that version, and Gate 1 (#93)
  recorded. A synthetic fixture never counts.
- The maintainer's review of #128: a repository name and the
  `self_repository` flag are defence in depth, not identity. A rename must not
  lose the approval, and a fork must not inherit it.

## Considered options

1. **A table of records, each holding all its evidence, written only by
   `ilmar gate`, with a fenced commit and an event.** **Chosen.**
2. *Gate records as events only.* Rejected: the reserved schema 9 table gives
   typed rows, a one-active-per-mode index and triggers that make a record
   immutable, independent of the event log.
3. *A file the operator edits.* Rejected: it could not be fenced, and nothing
   would record who wrote or revoked it.

## Decision

### 1. What a record states

`GateEvidence`, every field checked, none free text:

- `mode`: `supervised` (the only mode with a gate so far);
- `repository_id`: the approved target's immutable GitHub repository ID;
  `repository`: its owner/name when recorded, for people;
- `agent_version`: an exact `x.y.z`;
- `evidence`: the merged pull request URL that holds the gate evidence;
- `live_test`: the passed pinned-version live test run (a GitHub Actions run
  URL);
- `fixture` and `fixture_sha256`: the recorded stream fixture's path under
  `tests/fixtures/` and its SHA-256;
- `gate1`: the Gate 1 record (an issue, pull request or comment URL).

`ilmar gate record` reads the fixture from the current checkout. It must say
`"provenance": "recorded"` and name the same `claude_code_version`, and its
digest is recorded. `--repository-id` must equal `[autonomy] repository_id`
when that is set. `supervised` now requires `[autonomy] repository_id`.

### 2. Storage

Schema 9 adds `gate_record`: `seq`, `mode`, the evidence as canonical JSON,
`recorded_at`, `recorded_by`, `revoked_at` and `revoked_by`. At most one
unrevoked record per mode (a partial unique index). Triggers refuse deleting
a record, changing anything but setting `revoked_*` once, and un-revoking.
The migration only adds the table.

### 3. Writing

`Commit.gate` carries `GateRecorded(evidence, at)` or
`GateRevoked(mode, at)`, in a commit of its own. The store refuses it unless
the commit's context actor is a person, refuses a second active record for a
mode, and refuses a revocation with nothing active. It derives
`instance.gate_recorded` (with the evidence) or `instance.gate_revoked`
(naming the revoked record's `seq`) in the same transaction. Only
`ilmar gate record|revoke` builds a gate change. An architecture test checks
this, and no coordinator code path can pass the person check anyway.
`ilmar gate show` lists every record, revoked ones included.

### 4. What the loop will check (part 3)

Before each pass, the loop requires all of these:

- the active `supervised` record;
- its `repository_id` equal to `[autonomy] repository_id`;
- the live repository's ID, read from the forge at start and before each
  pass, equal to both. A rename keeps the ID; a fork, a transfer to another
  repository or a re-created repository does not.
- its `agent_version` equal to the configured `expected_version`.

Any mismatch, or a revoked record, stops the loop at the next check
(ADR-0030 §4).

## Consequences

- No record can exist before a person writes one with real, version-matched
  evidence. A fork or a lookalike repository never inherits an approval.
- A changed agent version, a new fixture or a new target needs a revocation
  and a new record.
- The repository name and `self_repository` checks of #128 stay as defence in
  depth.
- The fixture digest proves which file was reviewed, not that the review
  happened: that is the evidence PR's role.

## Revisit when

- A mode above `supervised` gets a gate (Mode 2, ADR-0033).
- Ilmar supports a forge other than GitHub: `repository_id` becomes
  provider-scoped.
