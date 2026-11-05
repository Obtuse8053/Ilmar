# ADR-0018: Verification stage, policy and append-only evidence

- Status: Accepted (maintainer approval on PR #45, 2026-11-05)
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
  3. Otherwise the commands run **per job** (see "Isolation boundary"
     below). For each job, the source-control adapter (ADR-0016)
     materialises the candidate revision into a fresh `verify-<run>/<job>`
     directory, and the job gets its own sandbox:
     - network `NONE`;
     - an empty environment, so no credential;
     - the policy's image and limits;
     - the name `…-verify-<run>-<job>`, owned per ADR-0012.

     The job's commands then run in order in that sandbox, and the sandbox
     is removed. A checkout directory is never reused.
  4. Each command's pass or fail is **only** its harness-measured exit
     status and timeout. Output is reduced to a `sha256` digest. Any report
     the candidate writes is never read. A timeout fails that check, and
     the job's remaining checks are recorded as "not run", because the
     runtime removed the job's sandbox (ADR-0014). Other jobs still run.
     Candidate text shown in a check's detail (the last stderr line) has
     control characters replaced, so it cannot carry terminal or link
     escape sequences into logs or change requests.
  4a. **Failure handling is fail-closed in both directions.**
     - If the candidate's code ends its own sandbox (for example by killing
       every process), the command fails with "the sandbox stopped while
       the command ran" and the job's remaining commands are "not run".
       The record is still written, so sabotage is a rejection with
       evidence, not missing evidence.
     - Any other runtime error while the sandbox is still running, and
       every `ContainmentError`, propagates: no verdict and no record. The
       coordinator treats that as an infrastructure failure.
     - A run directory that already exists is refused before anything is
       checked out or started, even if it is empty.
     - Each output stream of a sandboxed command is bounded (8 MiB kept,
       the rest drained and counted) by the Podman adapter, so a command
       printing without end cannot exhaust the coordinator's memory.
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
- **Isolation boundary: the job.** This replaces the first draft's claim
  that every check ran fresh; it never did (PR #45 review).
  - A command names a `job` in the policy (default `default`). Jobs are
    lowercase identifiers and run in order of first appearance.
  - **Between jobs:** separate checkout, separate container, separate
    `/tmp` and home tmpfs, separate process namespace. Nothing one job
    writes or leaves running is visible to another.
  - **Within a job:** commands share the checkout, `/tmp` and any
    processes left running, as steps of one CI job do. A later command can
    see, and can be misled by, what an earlier command of the same job
    did. Commands that must not influence each other belong in different
    jobs.
  - The rules that matter for security do not depend on the job: every
    job has network `NONE`, an empty environment and a read-only root, and
    the policy is read from `ilmar.toml`, never from the candidate.
  - Cost: each job pays one checkout and one container start and stop
    (seconds, dominated by the image being present locally). Commands
    within a job pay nothing extra. A container per command was rejected:
    it multiplies those costs by the number of commands and breaks
    legitimate steps that build on each other (for example, a build
    followed by its tests). Dependencies are expected in the image, so no
    job installs anything (ADR-0011 §7).
  - Evidence records each check's `job`.
- **Default path classes** count tool configuration at any depth (ruff, for
  example, reads a `ruff.toml` in any directory), plus `sitecustomize.py`,
  `usercustomize.py` and `.pth` files. Agent instruction files at any depth,
  `.claude/`, `.mcp.json`, `.vscode/` and `.devcontainer/` are protected,
  because they make later agent or editor sessions run commands.
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
  - the sandbox settings and a fresh checkout per run and job;
  - job isolation: one sandbox per job, one start for all of a job's
    commands, a tampered checkout or stray process in one job invisible
    to another, shared state within a job, a timeout ending only its job;
  - adversarial cases: an existing run directory refused before anything
    runs; a candidate killing its sandbox rejected with evidence; an
    infrastructure error or `ContainmentError` producing no record; control
    sequences removed from candidate text; bounded output, and a leftover
    process holding the output pipe treated as a timeout;
  - default path classes for nested configuration and agent hook files;
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
  publishes. An unfixed one is rejected whatever the agent claims. A
  two-job run shows that a file written to the checkout, a file in `/tmp`
  and a background process left by the first job are visible to the
  first job's later commands and to nothing in the second job. A
  candidate that kills every process in its sandbox is rejected with
  evidence, and the next job still runs.

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
  - Inline suppressions (`# noqa`, `# type: ignore`, skip markers in source
    files outside the test paths) are ordinary source changes and are not
    flagged. Holdout checks are the strengthening here too.
  - The path checks trust `CandidateRevision.changed_paths` as produced by
    `freeze_candidate`. The coordinator (#11) and restart recovery
    (#12/#15) must pass that object through, or recompute the diff from
    the two revisions. They must never rebuild it from stored revisions
    with an empty path list.
  - Commands within one job can influence each other. The policy author
    decides the grouping; the default puts everything in one job, which is
    the cheapest and the weakest setting.
  - Evidence grows with every attempt. Pruning is a later decision, made
    explicitly.

## Revisit when

- Holdout scenarios or a second, hidden check set become available.
- The approval policy (#20) consumes evidence records and needs more
  fields.
- Verification needs network access for some projects. That needs an
  explicit, policy-level opt-in (ADR-0011 §7).
