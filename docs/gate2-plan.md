# Gate 2 plan: from Gate 1 to unattended operation (proposal)

*Proposed 2026-11-14, for the maintainer's approval.* Once approved, the
order moves into `STATUS.md` and the issues. Work that needs no live
credential comes first, so it can proceed while Gate 1 waits on manual steps.

## Reconciliation: issues and ADRs against what has shipped

These are recommendations only. Closing an issue or accepting an ADR is the
maintainer's decision.

| Item | State on `main` | Recommendation |
| --- | --- | --- |
| #89 ADRs 0028–0031 | All four are Accepted | Close |
| #91 loop mode | Shipped (#129, #131); audit posted | Close (audit on #91) |
| #92 protected safety controls | Shipped; audit posted | Close (audit on #92) |
| #18 reservation ledger, suspension, kill switch | Budget, suspension and kill switch are on `main` | Audit against its criteria, then close |
| #59 quarantine resolution | #135 (ADR-0042) and #136 open | Close when both merge |
| #111 "credential-injecting egress proxy" | Superseded by ADR-0037's sidecar, which is built and not yet wired | Retitle: "Model gateway sidecar: wiring and Gate 2 evidence" |
| #94 LiteLLM | Deferred by ADR-0037 | Mark deferred; revisit on ADR-0037's condition |
| #95 supervisor | ADR-0033 built; #140, #141 (backoff) and #142 (credentials) open | Close when those merge |
| ADR-0033, 0036–0040 | Implemented and merged, still `Proposed` | Accept each, or record why it stays Proposed |
| #102, #119 | Load-sensitive cancellation tests | Keep; not Gate 2 blockers |

## What #96 requires, and where each part stands

1. **Gateway integrated; model key absent from the sandbox (real Podman).**
   - Built: the sidecar, admission, settlement (ADR-0039) and the real-Podman
     route and key checks.
   - Missing: wiring into configuration, and a Mode 2 mode that may use it.
2. **Supervisor tests, including restart loops and supervisor failure.**
   - #140, #141 and #142 are open.
3. **Unattended soak run.** This needs live credentials and Gate 1 first.

## The shortest path, in order

### A. No live credentials needed

1. **Merge the open safety fixes:** #138 (the gateway fails closed on its
   own errors), then #140 → #141 and #142.
2. **Gateway concurrency decision** (from the #111 audit): set
   `max_in_flight = 1` for Mode 2, or state the bound as `max_in_flight`
   requests, then run the credential-free compatibility workflow with that
   value. The workflow first needs #137.
3. **Gateway hardening** (from the audit; small and independent):
   - cap response buffering, for a clean halt;
   - add a socket timeout on request bodies;
   - allow-list response headers;
   - put the sidecar on a dedicated egress network.
4. **ADR: the Mode 2 mode.**
   - `autonomous` becomes parseable and needs its own gate record, which
     needs a recorded Gate 1.
   - The gateway credential route is mandatory in it, and the environment
     model key is refused.
   - `[supervisor] enabled` becomes allowed (ADR-0043) only there.
   - The unattended loop has no operator on call, so every suspension is
     final until a person resumes.
5. **Wire the gateway** behind that mode:
   - `GatewaySettings` come from configuration, and the price table is
     attested by the operator;
   - the key is read through `[secrets]`;
   - tests show that every other mode refuses it.
6. **Provider-outage handling:** provider errors, timeouts and a dead
   sidecar each end in the documented suspension or retry, tested with
   fakes.
7. **A soak rehearsal without credentials:** a long scripted run against
   the fake provider, with injected faults (crashes, supervisor restarts,
   kill switch, outages, quarantine). It runs nightly in CI, so the real
   soak finds nothing new.

### B. Live credentials (the maintainer)

8. **Gate 1** (#93, runbook): record it.
9. **The gateway against the real API** (`gateway-live.yml`, #126): a
   spend-capped key, through the sidecar.
10. **The soak run** on the disposable repository: a fixed duration, the
    USD 10 workspace cap and Ilmar's own budget, unattended. Observe at least
    one supervisor restart, the kill switch and a provider outage. Commit
    the evidence, then `ilmar gate record` for Mode 2.

### C. After Gate 2: the path to self-improvement

11. **The safety-kernel ADR** (`docs/self-improvement-design.md`).
12. **#97, releases:** immutable build, human promotion, health gate and
    rollback.
13. **#28 and #29** (maintainer): branch protection, secret scanning and
    pinned Actions; and a least-privilege identity for Ilmar.
14. **#19:** self-hosting on a disposable copy of Ilmar. Then **#98**,
    Gate 3.

## Progress (2026-11-16)

| Step | PR / issue | State |
| --- | --- | --- |
| A1 safety fixes | #138 (gateway fail-closed and adversarial tests), #140 → #141 (restarts), #142 (credentials) | open, sensitive |
| A2 gateway concurrency | #138 audit; `max_in_flight` defaults to 1 in #146 | decision pending |
| A5 gateway wiring | #145 (ADR-0044) → #146 | open, sensitive; manual mode only |
| A7 soak rehearsal | #148 (fake-provider soak) | open, test only |
| Per-pass reads decode the whole event log | #147 | **found by the soak; Gate 2 blocker** |
| A4 the Mode 2 mode | — | next ADR |
| A3 gateway hardening (buffer cap, body timeout, header allow-list, network) | #111 | to do |
| A6 provider-outage handling | — | to do; the soak covers refusals and rate limits |

## Gate 2 acceptance criteria (proposed for #96)

Each needs evidence committed in an evidence PR. Ilmar checks only the
record's form (ADR-0040).

1. **Gate 1 recorded.**
2. **The gateway route,** on real Podman:
   - the model key is absent from the sandbox's processes, environment,
     files and logs;
   - every request is admitted against a reservation;
   - an uncertain charge stops all forwarding and leaves a `spend-uncertain`
     suspension that only a person's review lifts.
3. **Spending:**
   - charges never exceed `[budget]`'s daily and monthly allowances;
   - the provider's workspace cap is observed refusing (the Gate 1 cap
     drill, repeated through the gateway).
4. **Restarts:**
   - a crash is restarted under the ADR-0043 backoff;
   - a restart loop suspends (`restart-loop`);
   - exits 3 and 78 are never restarted;
   - systemd's actual `RestartUSecNext` is recorded on the operator's host.
5. **Credentials:** from systemd encrypted credentials; never in a unit
   file, an argument, a log or the repository; fails closed when missing.
6. **Soak:**
   - **Fake provider:** the harness passes a long run, at least 200
     lifetimes over several seeds, with pass time flat (#147 fixed).
   - **Live:** the disposable repository runs unattended for a fixed
     duration under the USD 10 workspace cap. At least one supervisor
     restart, one kill switch and one provider outage are observed.
     Nothing is merged by Ilmar.
7. **Human control:** every PR is left for a person. Every suspension's
   resume is a person's command, with the event in the log.

## Recommended next PRs for approval, in order

1. #144 (Gate 1 command sheet; docs).
2. #138, then #140 → #141, and #142 (sensitive).
3. #145 → #146 (sensitive).
4. #148 (test only).
5. Then #147 and the Mode 2 ADR.
