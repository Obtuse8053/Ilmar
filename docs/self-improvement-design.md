# Self-improvement design (proposal, for the maintainer's decision)

*Proposed 2026-11-14. Not a decision.* ADR-0031 (amendment of 2026-11-12, §4)
requires a reviewed ADR, before Mode 3, that decides how Ilmar can propose
improvements to itself while safety-critical changes stay under human control.
This document is the input to that ADR. Nothing here changes behaviour, and
each part lands only through its own ADR or issue.

## Where we are

- **Ilmar cannot push any change to its own code.** In Modes 1 and 2, the
  whole of `src/ilmar/` is a protected safety control in Ilmar's own
  repository (ADR-0031 interim policy). A candidate that touches it is frozen
  with its evidence for the maintainer.
- **Ilmar's own repository is never a Mode 1 target** (ADR-0030 §5,
  `ILMAR_REPOSITORY_ID`), and trials are refused there (ADR-0041).
- **Humans merge every pull request and promote every release.** No setting
  changes this (roadmap; ADR-0031 §6).
- **What a candidate writes never configures the factory.** The verification
  policy, the protected list, budgets, limits, gate records and supervisor
  files come only from the running release and the operator's configuration
  (ADR-0031 §5).

## The proposal in one paragraph

Split Ilmar's code into a small **safety kernel**, which only a human may
change, and **ordinary code**, for which Ilmar may open pull requests that a
human reviews and merges. Make the split enforceable by import closure: the
kernel may import only the kernel. Ilmar always runs an **immutable, human-
promoted release**, never its own working tree, and every new release must
pass a health gate, with one automatic rollback. Self-hosting starts on a
**disposable copy** of Ilmar's repository, then moves to the real one.

## 1. How Ilmar works on its own backlog

1. **Tasks.** Only issues a human labels `ilmar:ready` (#20). The issue
   template asks for acceptance criteria. Ilmar never creates or labels its
   own tasks.
2. **Where it works.** First on a disposable copy of Ilmar's repository with
   its own immutable ID (#19). Only after Gate 3 does it work on
   `Obtuse8053/Ilmar`, a separate gate record bound to that ID.
3. **What it may change.** Ordinary code, docs and tests of ordinary code.
   A candidate that touches the kernel or any other ADR-0031 path is frozen
   for the maintainer (today's mechanism, unchanged).
4. **How it publishes.** One branch and one pull request per task, opened by
   the coordinator (ADR-0006), with the evidence bundle and provenance. At
   most one open self-PR at a time (**decided**, 2026-11-16), so review
   never queues up. A second task waits until that PR is merged or closed.
5. **Who merges.** The maintainer only:
   - Ilmar's GitHub identity is a **dedicated GitHub App** (**decided**,
     2026-11-16), installed only on the repositories it works on. It has
     contents, pull-request and issue access, but no merge, administration,
     workflow or bypass rights, and short-lived installation tokens;
   - branch protection (#28) requires the maintainer's review and green CI
     on the exact head SHA;
   - a PR Ilmar opened cannot be approved by Ilmar's identity.
6. **CI never gives candidate code secrets** (ADR-0031 §4). Pull-request
   workflows are read-only; credentialed workflows run on `main` only,
   started by hand.

## 2. The safety kernel

**Proposed contents.** These are the ADR-0031 §1 items that are code today,
mapped to modules:

- credentials, the secret scan and the registry's secret handling;
- sandbox and container policy (`adapters/podman/`), the gateway and its
  admission;
- fencing, ownership, the journal, recovery and quarantine;
- budgets, limits, suspension, the kill switch, time and restarts (the start
  log);
- configuration validation, the verification policy, gate records and trial
  authorization;
- the protected-list module itself;
- the loop's stop rules and the CLI commands that record human decisions
  (`gate`, `resume`, `blocked`, `quarantine`).

**Initially also in the kernel** (**decided**, 2026-11-16): every
credential operation, every policy (verification, protection, budget,
admission) and **every write-capable forge or tracker operation**:
- pushing;
- opening or updating a change request;
- labelling, commenting on, claiming or closing an issue;
- creating a branch.

The tracker and forge adapters are therefore kernel in full, until a later ADR
splits their read-only parts out.

Everything else (other CLI output, docs, ordinary tests) is ordinary code.

**Enforcement, all in `tests/architecture/` (itself protected):**

- **Import closure.** A kernel module may import only the standard library,
  `ilmar.domain` types the kernel lists, and other kernel modules. Otherwise
  a change to ordinary code could change kernel behaviour indirectly.
- **Exact listing.** Every module is either kernel or ordinary. A new module
  is kernel until the maintainer classifies it.
- **Kernel tests are kernel.** The tests of kernel modules, contract suites,
  every `conftest.py`, `tests/integration/` and the live harness are
  protected, so Ilmar cannot weaken the tests that hold the kernel.
- **The list can only grow without an ADR.** Narrowing it needs an ADR and a
  matching test change, as now.

**Alternative considered:** keep all of `src/ilmar/` protected and let Ilmar
change only docs and tests. That is simpler, but it gives Ilmar almost
nothing to improve, and test-only changes still need review.

## 3. Immutable releases, health checks and rollback (#97, ADR-0034 to come)

- **What a release is built from:** a human-merged commit whose CI was green
  on that exact SHA.
- **Immutability:** the release is unpacked once into a versioned directory,
  read-only, and recorded by digest. Ilmar runs only from a promoted
  release, never from a checkout it can write.
- **Promotion** (`ilmar release promote <version>`) is a human command, and
  no mode lets Ilmar call it. It switches `current` atomically.
- **Health gate:** after a switch, the new release must complete
  `health_check_passes` passes:
  - the loop's preflight;
  - an event-log verification;
  - a dry pass with no task.
- **Failure:** one automatic rollback to the previous release, then
  suspension. A release with a schema migration needs a store backup before
  promotion and never rolls back automatically.
- **Separate counters:** upgrades never share the restart counter
  (ADR-0033, ADR-0043).

## 4. What must change before self-hosting is permissible

Each item is its own issue or ADR. None needs live credentials except where
marked.

1. **Gate 1 recorded** (#93; credentials) and **Gate 2 recorded** (#96;
   credentials). Self-hosting keeps every Mode 1 and Mode 2 limit.
2. **ADR: the safety kernel**, replacing ADR-0031's interim whole-`src/ilmar/`
   entry, with the import-closure and listing tests. It lands before any
   narrowing.
3. **Releases (#97):** immutable build, human promotion, health gate, one
   rollback, migration rule. Ilmar must run from a release, never a working
   tree.
4. **Repository protection on `main`** (#28, maintainer): required review by
   the maintainer, required CI, no bypass for Ilmar's identity, and secret
   scanning with push protection. Actions pinned to SHAs (#29).
5. **A separate, least-privilege identity for Ilmar**, with no merge or admin
   rights, recorded in the gate record.
6. **The Mode 3 mode and gate record**, bound to the disposable copy's ID
   first, then to Ilmar's own ID. ADR-0030 §5's exclusion of Ilmar's own
   repository is lifted only by that record, never by a setting.
7. **Gate 3** (#98): upgrade and rollback drills, and a self-hosting run on
   the disposable copy (#19).

## 5. Protections that do not change

- Humans merge and promote, in every mode.
- The provider's spend cap stays the hard backstop.
- Candidate code never runs with secrets.
- Gate records come only from `ilmar gate record`, by a person.
- Autonomy is off until each gate passes.

## 6. Minimum controls before Ilmar may work on its own repository

All must hold at once. Each maps to a test or a recorded check.

| Control | Enforced by |
| --- | --- |
| Tasks only from a person's `ilmar:ready` label | the task provider's claim rules (ADR-0026) |
| Ilmar opens PRs and never merges | Mode 1 and 2 rules; branch protection (#28); an identity without merge rights |
| Safety-kernel paths frozen for review | the protected-control check against the running release's list (ADR-0031) |
| Kernel import closure | `tests/architecture/` (new, with the kernel ADR) |
| Isolated execution | the sandbox (ADR-0014, ADR-0036): no network but the model route, no forge credential |
| Spending | `[budget]`, the gateway (ADR-0037, ADR-0044) and the workspace cap |
| Restarts | the start log, backoff (ADR-0043), never after 3 or 78 |
| Runs only a promoted release | #97: an immutable install; promotion by a person |
| Health-gated rollback | #97: one automatic rollback, then suspension |
| A separate gate record for the repository's own ID | ADR-0040's form, a new mode (Mode 3) |

## Decisions (maintainer, 2026-11-16)

1. At most **one open self-improvement PR** at a time, initially.
2. Ilmar's identity is a **dedicated GitHub App**.
3. The safety kernel initially protects **all credential, policy and
   write-capable forge operations** (§2).
