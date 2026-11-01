# AGENTS.md — How to work on Ilmar

This file is the operating manual for anyone changing this repository, whether
you are a human or a coding agent (including Ilmar itself). `MANIFEST.md` is the
founding specification and takes precedence over this file. If the two
disagree, follow the manifest and open an issue about the conflict.

Read `MANIFEST.md` before your first change. Read `docs/architecture.md` and the
ADRs in `docs/adr/` before changing anything under `src/ilmar/`.

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
| `docs/adr/` | Architecture decision records. |
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
  deterministic checks (tests, linters, CI status).
- Source-control and task-tracker credentials never enter the agent sandbox.
  Pushing, opening change requests and talking to task trackers happen in the
  coordinator process (ADR-0006). The coding agent's own model credential is
  the single, documented exception.
- Secrets never appear in configuration files. Configuration names the
  environment variable that holds a secret (`token_env = "GITHUB_TOKEN"`).

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

1. Work on a branch named `<milestone-or-area>/<short-topic>`, for example
   `m2/github-task-provider`.
2. Define acceptance criteria before coding (MANIFEST §8).
3. Implement the smallest useful increment.
4. Write or update an ADR in `docs/adr/` for any decision that changes the
   architecture, a port contract, persistence format, security boundary or
   dependency policy.
5. Open a pull request using the template. Fill in the provenance section.
6. **Never merge your own pull request.** A maintainer approves and merges.

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
  `docs/references.md`, citing the manifest's reference IDs (`[R1]`…).
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
