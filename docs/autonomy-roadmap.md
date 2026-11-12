# Autonomy roadmap (M3 and M4)

Approved by the maintainer on 2026-11-10 (revision 2, with three
amendments). This file is the durable copy of that plan. Decisions it
describes become binding only through the ADRs it names. Issue state lives on
GitHub; the order of work lives in `STATUS.md`.

## Goal

Ilmar runs continuously on its approved backlog, improves its own code
through pull requests, and restarts or updates safely. It does this without
uncontrolled spending, restart loops or any weakening of its safety controls.
Autonomy grows in three gated modes, and each mode is off until its gate
passes.

| Mode | Milestone | What runs | Hard spending limit | Gate |
| --- | --- | --- | --- | --- |
| 0. Manual (today) | — | `ilmar run --once`, started by a human | Provider workspace cap | — |
| 1. Supervised autonomy | M3 | `ilmar run --loop` on approved tasks, operator on call, restarts manual | Provider workspace cap | #93 |
| 2. Budget-limited autonomy | M4 | Unattended processing and pull requests; supervisor restarts | LiteLLM gateway per execution and per day, plus the provider cap | #96 |
| 3. Controlled self-hosting | M4 | Ilmar works on itself; humans promote releases; health-gated rollback | As Mode 2 | #98 |

A mode needs all three of: `[autonomy] mode`, a gate record written by the
human-run `ilmar gate record`, and the `--loop` flag. A higher mode keeps
every limit of the modes below it. Lowering the mode, the kill switch or an
exhausted budget always ends in a suspended state.

## Rules that hold in every mode

- **Humans merge every pull request and promote every release.** No
  configuration setting can change this. Automated promotion is out of scope.
- **Ilmar never pushes a change to a safety control** (#92, ADR-0031). A
  candidate that touches a protected path is frozen with its evidence and left
  for the maintainer. The protected list and the verification policy come from
  the running release and the operator's configuration, never from a
  candidate.
  - **Interim, for Modes 1 and 2:** in Ilmar's own repository the whole of
    `src/ilmar/` is protected (ADR-0031 amendment, 2026-11-12).
  - **Before Mode 3:** a reviewed design must decide how Ilmar can propose
    self-improvements while safety-critical changes stay human-controlled.
- **Reservations are accounting, not cost limits.** In Mode 1 each execution
  is charged a fixed, conservative reservation before it starts, and the
  charge is never reduced. A reservation decides only whether new work may
  start. Actual cost is limited by the provider's workspace cap (and in
  Mode 2 by the gateway).
- **Independent per-execution limits** bound every attempt, including across
  crashes: wall-clock kill, stall kill, turn limit, attempt limit with durable
  backoff, and one execution at a time in Mode 1.
- **One credential path.** The model credential is one API key scoped to a
  dedicated, non-default Anthropic workspace with a monthly spend limit.
  Before each container start, Ilmar refuses if the sandbox could reach a
  model any other way. Cap enforcement is demonstrated at Gate 1.
- **Time.** A persisted time may only delay the start of new work (ADR-0028,
  narrowly amending ADR-0022 §Time). Recovery, ownership, fencing, quarantine
  and budget refunds stay clock-free. A clock anomaly only delays or
  suspends.
- **Fail closed.** If spending cannot be bounded, the ledger cannot be
  written, the cap is not attested, the kill-switch path is unset or the
  clock is anomalous, Ilmar does not start new work.
- Existing fencing, ownership, crash-recovery, containment (ADR-0010,
  ADR-0011, ADR-0015) and credential (ADR-0006) guarantees are unchanged.

## Mode 2 gateway: LiteLLM, if it passes evaluation

An operator-run LiteLLM proxy becomes the trusted meter and per-execution cap
only if an evaluation of a pinned version passes every point in #94:

- pre-request reservation, or a proven worst-case overshoot of one request;
- fail-closed budget enforcement;
- behaviour when Postgres or Redis is down, slow, read-only or flushed;
- durable spend across restarts;
- admin-key isolation;
- Claude Code compatibility;
- the security record;
- MIT rather than Enterprise licensing.

The Anthropic workspace cap stays as an independent backstop. If the
evaluation fails, Ilmar stays in Mode 1 and a separate proposal follows.

## Restarts and upgrades

An operator-installed supervisor (a systemd user unit or a Podman Quadlet)
owns process restarts and the external kill switch (#95, Mode 2). Restarts
and version upgrades are separate paths with separate counters. Ilmar never
restarts itself. Upgrades exist only in Mode 3 (#97): an immutable release,
built from a human-merged commit whose CI was green on that SHA, is promoted
by a human and rolled back once, automatically, if its health gate fails.
Releases with schema migrations never roll back automatically.

## Milestones and critical path

| Milestone | Ends at | Pull requests (estimate) | Critical path |
| --- | --- | --- | --- |
| M3 Reliability | Gate 1 (#93) | about 8, plus gate evidence | #89 ADRs → #13 → #18 → #91 loop mode |
| M4 Self-hosting, Mode 2 part | Gate 2 (#96) | about 5, plus gate evidence | #94 evaluation → #94 integration |
| M4 Self-hosting, Mode 3 part | Gate 3 (#98) | about 6, plus gate evidence | #97 releases |

In parallel within M3: #90 (limits and credential path), #92 (protected
safety controls) and #59 (quarantine recovery). #87 (stray labels) is off the
critical path. #14 and #51 are outside the autonomy path.

Gate 1 runs on a dedicated test repository. MANIFEST §10 places work on
Ilmar's own repository in M4, so pointing the loop at this repository waits
for the maintainer's decision after Gate 1.

## Mandatory test areas

Every gate requires its tests to pass in CI, with the existing contract,
architecture, crash-recovery and real-Podman suites unchanged:

- concurrency;
- budget exhaustion;
- provider outages (the model API, GitHub and, from Mode 2, the gateway and
  its database);
- partial usage reporting;
- crashes at every new step;
- clock anomalies;
- the credential path;
- protected paths;
- restart loops and supervisor failure (Mode 2);
- containment of the gateway (Mode 2);
- upgrades and rollbacks (Mode 3).

The issues list the individual cases.
