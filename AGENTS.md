# AGENTS.md — How to work on Ilmar

This file is the operating manual for anyone changing this repository, whether
you are a human or a coding agent (including Ilmar itself). `MANIFEST.md` is the
founding specification and takes precedence over this file. If the two
disagree, follow the manifest and open an issue about the conflict.

Read `MANIFEST.md` before your first change. Read `docs/architecture/README.md`
and the ADRs in `docs/architecture/decisions/` before changing anything under
`src/ilmar/`.

## 0. Start here

Every session, whether it is new or resumed:

1. Read `docs/STATUS.md` (where we are) and `docs/workflow.md` (how work is
   picked, claimed, handed off and finished).
2. Check open PRs and issues labelled `status:in-progress` on GitHub before
   starting anything new.
3. Work only on `approved` issues, one issue per branch and PR.

You should not need any earlier conversation. If something you need is not in
the repository or the issues, add it there.

## 1. Repository map

| Path | Purpose |
| --- | --- |
| `src/ilmar/domain/` | Provider-neutral value types and errors. Standard library only. |
| `src/ilmar/ports/` | Provider interfaces (`typing.Protocol`). Depend only on `domain`. |
| `src/ilmar/core/` | Orchestration logic: state machine, later scheduler and coordinator. Depends on `domain` and `ports`. |
| `src/ilmar/config.py` | TOML configuration loading and validation. |
| `src/ilmar/adapters/` | Concrete providers (SQLite, in-memory, later GitHub, Podman, Claude Code). |
| `src/ilmar/cli.py` | Command-line entry point. |
| `tests/contracts/` | Shared contract suites every adapter of a port must pass. |
| `tests/unit/` | Fast, deterministic unit tests. |
| `tests/architecture/` | Tests that enforce the dependency rules and repository policy. |
| `docs/architecture/` | Architecture overview and decision records (`decisions/`). |
| `docs/research/` | Source registry, findings and the research review process. |
| `docs/STATUS.md` | Current milestone, planned order, recommended next task, change log. Issue state lives on GitHub. |
| `docs/workflow.md` | Labels, milestones, session protocol, milestone close-out. |
| `docs/roadmap.md` | Milestone status and approved improvements, linked to issues. |
| `scripts/` | Small standard-library helper scripts used by CI. |

## 2. Commands

```sh
python -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"

ruff check .                 # lint
ruff format --check .        # formatting
mypy                         # strict type checking
pytest                       # all tests
python scripts/license_inventory.py   # dependency license check
```

All five must pass before you submit a change. CI runs the same commands.

## 3. Architecture rules

Ilmar uses ports and adapters (ADR-0002). The dependency direction is fixed:

```
adapters ──► ports ──► domain
   │           ▲         ▲
   └──► core ──┴─────────┘
```

- `domain` imports only the standard library and `ilmar.domain`.
- `ports` imports only the standard library, `ilmar.domain` and `ilmar.ports`.
- `core` and `config` never import `ilmar.adapters` or any third-party package.
- Provider-specific types (GitHub issue numbers, Podman container JSON, Claude
  Code output formats) stay inside their adapter. Convert them to domain types at
  the adapter boundary.
- `tests/architecture/` enforces these rules. Do not weaken those tests to make a
  change pass; change the design instead, or propose an ADR.

Additional rules:

- The core has zero runtime dependencies (ADR-0003). An adapter that needs a
  library declares it as an optional extra in `pyproject.toml`.
- Every adapter must pass the contract suite for its port in
  `tests/contracts/`. Provider-specific behaviour goes in additional
  integration tests, not in the contract.
- An agent declaring success is never sufficient evidence. Acceptance requires
  deterministic checks run by Ilmar's own verification. What is published must
  be exactly the verified candidate revision (ADR-0010).
- Host-side git never runs in an agent-written repository. Agent output is
  frozen as plain file content into a coordinator-owned repository
  (ADR-0010).
- Source-control and task-tracker credentials never enter the agent sandbox.
  Pushing, opening change requests and talking to task trackers happen in the
  coordinator process (ADR-0006). The coding agent's own model credential is
  the single, documented exception.
- Secrets never appear in configuration files. Configuration names the
  environment variable that holds a secret (`token_env = "GITHUB_TOKEN"`).

## 3a. Research and architectural change

The process and formats are in `docs/research/README.md`. The rules:

1. **Consult first.** Before proposing a significant architectural change, read
   the relevant ADRs and `docs/research/findings.md`. Cite what you relied on.
2. **Record sources honestly.** Add new sources to `docs/research/sources.md`
   with licence and access level. Label each finding Established, Emerging or
   Experimental. Never present one organisation's self-reported result as
   established.
3. **Compare alternatives.** A proposal lists the options considered. If
   evidence meets an ADR's "Revisit when" condition, say that the ADR should be
   reconsidered. Do not quietly work around it.
4. **Decide in ADRs.** Record decisions in `docs/architecture/decisions/`
   (template in its README), linked to source IDs (`[R<n>]`) and findings
   (`F-<nnn>`).
5. **Respect rights.** Paraphrase sources. Never copy third-party code or text
   without the §7 process. Check the licence of the specific material, not just
   the repository.
6. **Propose; don't impose.** Architectural improvements start as a GitHub issue
   (Architecture proposal template) and land through a reviewed pull request.
   Never change architecture silently, inside an unrelated change, or by
   editing an ADR's decision after it is accepted.

## 4. Coding standards

- Python 3.11+, fully type-annotated, passing `mypy --strict`.
- Prefer frozen dataclasses and enums for domain data. No mutable global state.
- Inject time, randomness and I/O. Core functions take `now` instead of calling
  the clock.
- Raise errors from `ilmar.domain.errors`. Do not catch broad exceptions except
  at process boundaries, and never silence them.
- Keep modules small and single-purpose. Avoid frameworks and speculative
  abstractions: build what the current milestone needs.
- Every Python file starts with `# SPDX-License-Identifier: Apache-2.0`.

## 5. Testing rules

- Tests are deterministic and offline. No network and no real containers in the
  default `pytest` run. Integration tests that need real services will be
  marked and opt-in (introduced in M2).
- New behaviour comes with tests. Bug fixes come with a regression test.
- Contract suites test behaviour visible through the port, not implementation
  details.
- Use the fakes in `tests/fakes.py` rather than mocking internals.

## 6. Change workflow

1. Work on a branch named `m<N>/<issue>-<topic>` (or `chore/<issue>-<topic>`),
   for example `m2/6-github-tasks`.
2. Define acceptance criteria before coding (MANIFEST §8).
3. Implement the smallest useful increment.
4. Write or update an ADR in `docs/architecture/decisions/` for any decision
   that changes the architecture, a port contract, persistence format, security
   boundary or dependency policy.
5. Update `docs/STATUS.md` only if the plan, the milestone or the recommended
   next task changed. Open a pull request using the template, with
   `Closes #<issue>`, and fill in the provenance section.
6. **Never merge on your own initiative.** Merge only when the maintainer
   explicitly asks for that specific PR, and only once CI is green on the exact
   commit being merged (`docs/workflow.md`, "Finish").

### Sensitive areas

Changes touching any of the following need independent verification and
explicit approval from an authorized human maintainer (MANIFEST §6):

- authentication and credential handling;
- sandbox and container isolation policy (`ContainerRuntime` and its adapters);
- approval and verification policies;
- release automation and CI workflows (`.github/`);
- this file, `MANIFEST.md`, `LICENSE`, `NOTICE` and `THIRD_PARTY_NOTICES.md`;
- the architecture tests in `tests/architecture/`.

Say so in the pull request description when a change touches these.

## 7. Intellectual property (mandatory)

Summary of MANIFEST §7. The manifest text is authoritative.

- Write original code, tests and documentation. Published ideas and patterns may
  inform the design; implementation code and prose from other projects must not
  be copied just because they are public.
- Record architectural inspiration in the relevant ADR and in
  `docs/research/sources.md`, citing source IDs (`[R1]`…).
- Before importing any third-party code or asset: identify exact source, file
  and revision; verify the license of that specific material; confirm Apache-2.0
  compatibility; preserve notices; record it in `THIRD_PARTY_NOTICES.md`; get
  maintainer approval. **If licensing is uncertain, do not import it.**
- Coding agents: do not reproduce substantial third-party code or documentation
  from memory. Name any external source you used materially. Flag uncertain
  provenance in the pull request for human review.
- Never remove copyright or license notices.
- New dependencies require a license check (`scripts/license_inventory.py`) and
  a line in the pull request explaining why the dependency is needed.
- Use third-party names only to identify integrations factually. Do not imply
  endorsement or affiliation.
- External contributions are not accepted until the Contributor License
  Agreement described in MANIFEST §7.6 exists.

## 8. Definition of done

- Acceptance criteria met and demonstrated by tests.
- Lint, format, type checks, tests and the license check pass.
- Architecture rules intact.
- ADRs and docs updated where decisions changed.
- Pull request describes the change, deviations from the manifest, risks and
  provenance.
