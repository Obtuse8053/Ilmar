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

## Recommended next PRs for approval, in order

#138, #140, #141, #142, #137 (unblocks the pinned evidence), then items A2,
A4 and A5.
