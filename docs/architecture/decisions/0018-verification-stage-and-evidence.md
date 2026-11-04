# ADR-0018: Verification stage, policy and append-only evidence

- Status: Proposed
- Date: 2026-11-04
- Sources: [R3], [R7], [R9] · Findings: F-004, F-005, F-022
- Implements: ADR-0010 §3–7 (#9); schema v2 of ADR-0005's store

## Context and problem

ADR-0010 decides *that* acceptance rests on Ilmar's own checks of one frozen
candidate revision, with a trust boundary between Ilmar-controlled,
independent and candidate-executed checks, and an outcome table (§5). #9 has
to make that concrete:
- where the policy lives;
- how changed paths are classified;
- how checks run and what counts as their result;
- what the evidence record holds, and how it is kept;
- how the state machine ties executions to candidate revisions.

## Considered options

Where the policy lives:
1. In the repository being changed, for example a policy file on the base
   branch. The agent can edit it in the same change, and a later attempt
   would read the edited one.
2. **In Ilmar's configuration (`ilmar.toml`).** The candidate has no way to
   influence it.

Path classes:
- Full replacement in configuration: a slip could drop `.github/`.
- **Built-in defaults plus `extra_*` patterns that can only extend them.**

Evidence storage:
- In the execution row: it is overwritten on retry.
- **An append-only table**, with database triggers and a digest per record.

## Decision

- **Policy (`[verification]` in `ilmar.toml`).**
  - The image, the commands (name, `argv`, timeout, `required`) and
    resource limits.
  - Three path classes with built-in defaults:
    - *protected:* `AGENTS.md`, `CLAUDE.md`, `MANIFEST.md`, licence and
      notice files, `ilmar.toml`, `.ilmar/`;
    - *check-influencing:* tests, `conftest.py`, tool and build
      configuration, dependency manifests and lock files, `scripts/`;
    - *CI configuration:* `.github/`, GitLab, CircleCI, Azure, Jenkins and
      Buildkite files.
  - Configuration can add patterns with `extra_*` but never remove a default.
  - The policy's canonical `sha256` digest is recorded in every evidence
    record and shown by `ilmar config validate`.
  - Without `[verification]`, nothing can be verified or published.
- **Path patterns.** Repository-relative and case-sensitive. `dir/` means
  "below `dir`", `**` crosses directories, `*` and `?` stay within one.
- **The `Verifier` (`ilmar.core.verification`).**
  1. *Independent checks* run on the candidate's `changed_paths` as data:
     `ci-config-paths`, which fails when CI configuration changed, and
     `protected-paths` and `check-influencing-paths`, which flag.
  2. If CI configuration changed, nothing of the candidate is executed.
     The verdict is `REJECTED_CI_CONFIG` (`ci-config-modified`), and a
     human takes over.
  3. Otherwise the candidate revision is materialised into a fresh
     `verify-<run>` directory by the source-control adapter (ADR-0016).
     Each run gets a fresh sandbox:
     - network `NONE`;
     - an empty environment, so no credential;
     - the policy's image and limits;
     - the name `…-verify-<run>`, owned per ADR-0012.

     A checkout directory is never reused.
  4. Each command's pass or fail is **only** its harness-measured exit
     status and timeout. Output is reduced to a `sha256` digest. Any report
     the candidate writes is never read. A timeout fails that check, and
     the remaining checks are recorded as "not run", because the runtime
     removed the sandbox (ADR-0014).
  5. The verdict follows ADR-0010 §5:

     | Diff | Required checks | Verdict | `independence` |
     | --- | --- | --- | --- |
     | Touches CI configuration | (not run) | `REJECTED_CI_CONFIG` | `ci-config-modified` |
     | Anything else | A required check fails | `REJECTED_CHECKS_FAILED` | `unchanged-checks` or `modified-checks` |
     | No check-influencing or protected paths | All pass | `PUBLISH` | `unchanged-checks` |
     | Check-influencing or protected paths | All pass | `PUBLISH_FOR_REVIEW` | `modified-checks` or `unchanged-checks` |

     Optional checks are recorded and never block. Protected paths always
     add `requires_human_review`.
  6. The agent's own report is stored as `agent_report`, labelled as
     reported, and never changes the verdict. The record has no field for
     change-request CI results, which are never evidence.
  7. The record is appended to the store **before** it is returned.
- **Evidence records** hold:
  - execution, attempt and time;
  - base and candidate revisions;
  - the policy digest;
  - per check: class, command, exit status, timeout, duration, output
    digest and whether it is required;
  - the touched protected, check-influencing and CI paths;
  - `independence`, the verdict, `requires_human_review` and its reasons;
  - the agent report.

  JSON is versioned (`format: 1`).
- **Store schema v2 (the first migration).**
  - `executions` gains `base_revision` and `candidate_revision`.
  - A new `evidence` table stores each record with its digest.
  - Triggers make `evidence` *and* `transitions` append-only.
  - Records read back are checked against their digest
    (`EvidenceIntegrityError`).
  - Version 1 databases migrate in place in one transaction, and a failure
    leaves them untouched.
  - The in-memory store keeps serialised copies.
- **State machine.**
  - `base_revision` is set once, on entering RUNNING_AGENT.
  - `candidate_revision` is set once, on entering VERIFYING.
  - VERIFYING and PUBLISHING need both.
  - A new attempt clears them.
- **Review label.** `ChangeRequestSpec.labels` are added to the change
  request and never removed, because a human may have set others. A
  `PUBLISH_FOR_REVIEW` verdict is published with `ilmar:needs-human-review`
  by the coordinator (#11).

## Verification

- Unit tests:
  - one test per outcome-table row;
  - protected paths in passing and failing runs;
  - the exit status as the only verdict (a candidate printing "PASSED" and
    exiting 1 fails);
  - the agent's claim ignored both ways;
  - the sandbox settings and a fresh checkout per run;
  - timeouts, then "not run";
  - optional checks;
  - evidence appended before return;
  - input validation;
  - the path matcher and policy digest;
  - evidence JSON round trips;
  - state-machine revision rules;
  - store contract additions;
  - the v1 to v2 migration, a failed migration, append-only triggers and
    tamper detection;
  - configuration parsing.
- Mutation checks:

  | Mutation | Failing tests |
  | --- | --- |
  | Ignore required failures | 5 |
  | Execute the candidate on CI changes | 1 |
  | Grant network | 12 |
  | Pass on timeout only | 5 |

- End to end on real Podman (CI): a real git candidate (ADR-0016) is
  verified in a real sandbox whose checks prove no network, no credential
  (with one set in the coordinator) and a read-only root. A fixed candidate
  publishes. An unfixed one is rejected whatever the agent claims.

## Consequences

- Good:
  - The agent cannot change what it is judged by without being flagged,
    and cannot change CI at all.
  - The evidence is durable, tamper-evident and explains every verdict.
  - Verdicts are deterministic given the policy, the candidate and the
    exit statuses.
- Bad:
  - Candidate-executed checks still run code the candidate controls. They
    catch honest mistakes, not a deliberately subverted candidate. Holdout
    checks the agent cannot see are the planned strengthening (F-005,
    [R7]).
  - The verification image must carry every tool the checks need, and no
    network is available at verification time (ADR-0011 §7).
  - Evidence grows with every attempt. Pruning is a later decision, made
    explicitly.

## Revisit when

- Holdout scenarios or a second, hidden check set become available.
- The approval policy (#20) consumes evidence records and needs more
  fields.
- Verification needs network access for some projects. That needs an
  explicit, policy-level opt-in (ADR-0011 §7).
