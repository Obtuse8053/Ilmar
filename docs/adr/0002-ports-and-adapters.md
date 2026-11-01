# ADR-0002: Ports and adapters with an enforced dependency rule

- Status: Proposed
- Date: 2026-11-01

## Context

MANIFEST §2.1 and §4 require issue trackers, source-control platforms, coding
agents and container runtimes to be interchangeable, and forbid
provider-specific types from leaking into core logic. The first targets are
GitHub, Podman and Claude Code, with GitLab, Jira, Docker and Codex planned.
Rules that are only written down tend to erode, especially when much of the code
is written by agents.

## Decision

Structure the package in four layers:

| Layer | Contents | May import |
| --- | --- | --- |
| `ilmar.domain` | Frozen dataclasses, enums, errors | stdlib |
| `ilmar.ports` | `typing.Protocol` interfaces | stdlib, `domain` |
| `ilmar.core`, `ilmar.config` | State machine; later scheduler, coordinator, policies | stdlib, `domain`, `ports` |
| `ilmar.adapters` | Implementations of ports | anything |

The five ports follow MANIFEST §4: `TaskProvider`, `SourceControlProvider`,
`ContainerRuntime`, `AgentRunner` and `ExecutionStore`.

Notable interface choices:

- **Protocols, not base classes.** Adapters conform structurally and need not
  import anything from the core. Mypy checks conformance where tests assign an
  adapter to a port-typed variable.
- **Opaque identifiers.** `TaskRef.key`, `RepositoryRef.slug` and
  `ContainerHandle.id` are strings that only the owning adapter interprets. The
  core never parses `owner/repo#123`.
- **`AgentRunner.run` receives the `ContainerRuntime`.** Any agent adapter can
  then run on any runtime adapter. Claude Code on Docker needs no new code once
  both adapters exist.
- **Neutral vocabulary.** "Change request" covers pull requests and merge
  requests; "checks" covers CI statuses and pipelines.
- **The store is dumb, the state machine is smart.** Legal transitions live in
  the core (ADR-0004). The store guarantees atomicity, uniqueness and optimistic
  concurrency (ADR-0005).

Every adapter must pass a shared contract suite for its port in
`tests/contracts/`. Contract suites are mixin classes that a test module
subclasses, providing factory hooks. The suites also run against the in-process
fakes in `tests/fakes.py`, so tests that use fakes rely only on behaviour that
real adapters must also have.

The dependency rule is enforced by `tests/architecture/test_dependencies.py`,
which parses imports with `ast` and fails the build on a violation.

## Consequences

- Adding a provider means writing one adapter and passing one contract suite.
- Some duplication at the boundary: adapters convert provider data into domain
  types.
- M1 has contracts for `ExecutionStore`, `TaskProvider` and `ContainerRuntime`.
  Contracts for `SourceControlProvider` and `AgentRunner` will be written with
  their first real adapters in M2, when there is concrete behaviour to pin down.
  Writing them now would mostly test fakes against themselves.
- The contract suites live under `tests/` for now. If third-party adapter
  packages appear, they can move into an importable `ilmar.testing` package.

## Prior art

- [R1] OpenAI Symphony and [R4] Open SWE: the general idea of separating the
  tracker, workspace and agent concerns in an issue-driven agent orchestrator.
- [R5] OpenHands: the idea of pluggable sandbox runtimes.
- Ports and adapters (hexagonal architecture) is a long-established, widely
  published pattern.

Ideas only. No code, interface definitions or text from these projects were
consulted or copied for this change. The references were not accessed during
M1; they are cited from MANIFEST §9 (see `docs/references.md`).
