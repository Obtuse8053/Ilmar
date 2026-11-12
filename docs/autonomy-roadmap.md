# Autonomy roadmap (M3 and M4)

Approved by the maintainer on 2026-11-10 (revision 2, with three
amendments). Revision 3, the accelerated path to Mode 2, was conditionally
approved on 2026-11-12 with seven binding amendments, which are reflected
below. This file is the durable copy of that plan. Decisions it
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
| 2. Budget-limited autonomy | M4 | Unattended processing and pull requests on one controlled repository, one execution at a time, approved tasks only; supervisor restarts | Sidecar gateway: worst-case cost reservation per request within a finite per-attempt dollar budget (ADR-0037); ledger daily and monthly bounds; the provider cap, independently | #96 |
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
- **Reservations are accounting, not cost limits.** In Mode 1 each execution
  is charged a fixed, conservative reservation before it starts, and the
  charge is never reduced. A reservation decides only whether new work may
  start. Actual cost is limited by the provider's workspace cap (and in
  Mode 2 by the sidecar gateway's worst-case reservations, ADR-0037).
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

## Mode 2 gateway: in the egress sidecar (ADR-0037)

LiteLLM (#94, ADR-0032) is deferred. Mode 2's gateway is Ilmar's own, in the
per-sandbox egress sidecar.

- **Credential:**
  - the real model key lives only in the sidecar;
  - the agent gets a throwaway per-attempt token;
  - the agent has no network route to the provider.
- **Hard per-attempt dollar bound:**
  - every request reserves its worst-case cost before it is forwarded, from
    the operator's attested price table;
  - only allowed models, usage types and request fields pass;
  - request size, `max_tokens` and in-flight requests are limited;
  - an attempt can never spend more than its ledger reservation.
- **Daily and monthly bounds:** the ledger's allowances therefore become
  proven daily and monthly dollar bounds.
- **Independent backstop:** the provider's workspace cap, tested at Gate 1.
- **Prerequisite:** the pinned Claude Code must work through the sidecar's
  plain-HTTP endpoint. A test records this before any gateway code is
  merged. If it fails, the fallback is the same sidecar over HTTPS with a
  per-sandbox certificate.

## Restarts and upgrades

An operator-installed systemd user unit owns process restarts (#95, ADR-0033,
Mode 2). It has bounded restarts, and exit codes 3 and 78 are never restarted.
Ilmar also keeps its own durable start log, written before anything else and
before any ownership claim; a restart loop suspends. Restarts
and version upgrades are separate paths with separate counters. Ilmar never
restarts itself. Upgrades exist only in Mode 3 (#97): an immutable release,
built from a human-merged commit whose CI was green on that SHA, is promoted
by a human and rolled back once, automatically, if its health gate fails.
Releases with schema migrations never roll back automatically.

## Milestones and critical path (revision 3)

**Scope of the first Mode 2:**
- one controlled repository;
- one execution at a time;
- approved tasks only.

**Outside it:**
- self-hosting, automatic upgrades and release promotion (Mode 3, #97, #98);
- more than one concurrent execution;
- more than one repository.

**Open pull requests.** #112 (decisions) and #113 (#18 part 1, the ledger and
suspension) each need their normal review.

| PR | Content | Issue | Depends on |
| --- | --- | --- | --- |
| C0 | Gateway compatibility test (pinned Claude Code, plain HTTP) | #111 | — |
| P0 | This ADR bundle: ADR-0037, ADR-0033, roadmap revision 3 | #111, #95 | — |
| P1 | Kill-switch file; a suspension interrupts the running agent; clock-anomaly suspension | #18 | #113 |
| P2 | Candidate secret scan | #110 | — |
| P3 | Protected safety controls, blocked release, CI-secrets guard | #92 | P2 (same publication hook) |
| P4 | Gateway sidecar: real key only in the sidecar, throwaway token, no direct route | #111 | P0; C0's recorded run |
| P5 | Gateway worst-case cost reservations, model, field and size limits, settlement | #111 | P4 |
| P6 | systemd unit, durable start log, restart-loop suspension | #95 | P0, #113 |
| P7 | Loop mode, `[autonomy]` mode, gate records, fail-closed start, one execution, exact version, the two-loop test | #91 | P1 |
| P8 | Gate evidence | #93, #96 | the gate session |

**Streams that run in parallel:**
- **A (coordinator and store):** #113 → P1 → P7.
- **B (publication):** P2 → P3.
- **C (gateway):** C0 recorded → P4 → P5.
- **D (supervisor):** P6.

**Schema order:** #113 is schema 8, and P7's gate records are 9. P6 needs no
schema.

**Maintainer-run checks, as early as possible:**
- the pinned-version live test with its recorded fixture;
- the pinned sealed-settings test;
- the gateway compatibility test (C0);
- the provider-cap drill.

**One gate session, two independent records:**
1. **Gate 1, the supervised segment (#93).** It has its full criteria and is
   recorded with `ilmar gate record` before the unattended segment begins.
2. **Gate 2, the unattended segment (#96).** Full criteria:
   - **Gateway (P4 and P5) merged.** A real-Podman test shows:
     - the model key appears nowhere in the sandbox (environment, files,
       `/proc`);
     - there is no route from the sandbox to the provider;
     - the gateway accepts only the throwaway token.
   - **Compatibility and pinned tests re-run through the real sidecar** on the
     pinned version.
   - **Per-attempt bound:**
     - unit and property tests show no request sequence exceeds the attempt
       budget;
     - a real budget refusal is observed end to end;
     - the price table is attested.
   - **Supervisor (P6) tests pass:** starts faster than the minimum interval,
     the hourly and daily limits, a backwards clock, a corrupt log, exits 3
     and 78 never restarted, and a supervisor ignoring its limits.
   - **An unattended soak** with budget exhaustion, a kill switch, a provider
     outage and a crash restart each observed.
   - **Blocked executions** neither halt the loop nor cause restarts, and are
     reported to the operator.
   - **Evidence committed (P8),** then `ilmar gate record` for Mode 2.

**Deferred:**
- #94 and ADR-0032 (LiteLLM);
- #111's TLS-terminating design;
- usage reported by the agent itself, replaced by the gateway's settlement;
- a separate restart-ledger table;
- #59 (quarantine resolution). This is deferred only while blocked executions
  cannot halt the loop or cause a restart cycle, and the operator is clearly
  notified (checked in P7 and at Gate 2).

#90 needs its acceptance audit, which is an issue comment only. #87, #14 and
#51 are off this path.

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
- **#111 is not required for Gate 1.** This holds only if Gate 1 runs on a
  controlled test repository that holds no credentials, with trusted tasks
  and a capped workspace. Since revision 3, #111 is delivered as the sidecar
  gateway (ADR-0037).
- **#111 is mandatory before** Mode 2, any untrusted repository, and
  self-hosting.

## Mandatory test areas

Every gate requires its tests to pass in CI, with the existing contract,
architecture, crash-recovery and real-Podman suites unchanged:

- concurrency;
- budget exhaustion;
- provider outages (the model API, GitHub and, from Mode 2, the sidecar
  gateway);
- partial usage reporting;
- crashes at every new step;
- clock anomalies;
- the credential path;
- protected paths;
- restart loops and supervisor failure (Mode 2);
- containment of the gateway (Mode 2);
- upgrades and rollbacks (Mode 3).

The issues list the individual cases.
