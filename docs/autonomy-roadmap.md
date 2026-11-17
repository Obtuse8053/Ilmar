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
| 2. Budget-limited autonomy | M4 | Unattended processing and pull requests; supervisor restarts | Ilmar's own model gateway sidecar per attempt (ADR-0037, ADR-0039), plus the provider cap | #96 |
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

## Mode 2 gateway: Ilmar's own sidecar (ADR-0037, ADR-0039)

*Updated 2026-11-14.* The original plan of an operator-run LiteLLM proxy,
adopted only if it passed an evaluation (#94), is **deferred**. ADR-0037
chose a per-sandbox gateway sidecar written in Ilmar, standard library only.
It is simpler for one repository with one execution at a time, and it adds
no external service (no Postgres or Redis failure modes, no licence
questions). ADR-0032 stays reserved for a shared gateway.

What the sidecar does:

- **Only route.** The sandbox reaches the model only through it, on an
  internal network.
- **Throwaway token.** The sandbox holds only a per-attempt token. The real
  key reaches only the sidecar, through a file-free hand-over.
- **Admission.** Each request is checked against strict shapes and a beta
  allow-list, and an estimated reservation is taken before it is forwarded.
- **Halts.** A complete, trusted usage report is charged in full. Anything
  else (an untrusted report, a non-200 status, an unreachable provider, or
  an error in the gateway itself) charges the whole reservation and halts
  the attempt.
- **Settlement.** Settlement into the ledger, crash recovery and the
  human spending review are ADR-0039.

**What it does not give:** a proven dollar bound. Only the output term of a
reservation is a hard bound. The input term is an estimate, and an overrun
is charged and halts, but it is not prevented (ADR-0037 §4). **The Anthropic
workspace cap stays the independent hard backstop**, in every mode, and the
Gate 1 cap drill tests it.

**Status:** built and tested (compatibility with the pinned Claude Code
through the real sidecar; unit and real-Podman tests). It is **not reachable
from any configuration** until its acceptance gates pass (#111, Gate 2).

**Revisit** (ADR-0037): more than one concurrent execution or repository, or
a provable input bound from the provider.

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
| M4 Self-hosting, Mode 2 part | Gate 2 (#96) | about 6, plus gate evidence | #111 gateway sidecar (ADR-0037, built) → configuration wiring → Gate 2 evidence; #94 LiteLLM deferred |
| M4 Self-hosting, Mode 3 part | Gate 3 (#98) | about 6, plus gate evidence | #97 releases |

In parallel within M3: #110 (candidate secret scan, a Gate 1 prerequisite),
#90 (limits and credential path), #92 (protected
safety controls) and #59 (quarantine recovery). #87 (stray labels) is off the
critical path. #14 and #51 are outside the autonomy path.

Gate 1 runs on a dedicated test repository. MANIFEST §10 places work on
Ilmar's own repository in M4, so pointing the loop at this repository waits
for the maintainer's decision after Gate 1.

### Credential containment by gate (maintainer decisions, 2026-11-11)

ADR-0036 sets out which routes to a model are enforced today. These decisions
follow from it:

- **Candidate secret scan (#110): mandatory before Gate 1.** Before
  publication, Ilmar scans the frozen candidate for the values of configured
  secrets and refuses to publish a match, without exposing the values. It
  detects the values and the specific encodings listed in #110. It does not
  detect arbitrary transformations of a secret.
- **Pinned-version compatibility test: mandatory before Gate 1.**
  `tests/integration/test_claude_code_pinned.py` passes on the pinned image,
  together with the pinned-version live test and its reviewed, recorded
  stream fixture.
- **Credential-injecting egress proxy (#111): not required for Gate 1.**
  This holds only if Gate 1 runs on a controlled test repository that holds
  no credentials, with trusted tasks and a capped workspace.
- **#111 is mandatory before** Mode 2, any untrusted repository, and
  self-hosting.

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
