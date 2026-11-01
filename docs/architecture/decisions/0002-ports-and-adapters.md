# ADR-0002: Ports and adapters with an enforced dependency rule

- Status: Proposed
- Date: 2026-11-01
- Sources: [R1], [R2], [R4], [R5], [R6] · Findings: F-008, F-010, F-011

## Context and problem

MANIFEST §2.1 and §4 require interchangeable trackers, SCM platforms, agents and
runtimes, with no provider types in core logic. First targets: GitHub, Podman,
Claude Code. Planned: GitLab, Jira, Docker, Codex. Rules that are only written
down tend to erode, especially when much of the code is written by agents.

## Considered options

1. Integrate GitHub, Podman and Claude Code directly in the orchestrator, as
   R1's reference implementation does with its tracker and agent.
2. A plugin framework now (entry points, plugin manager).
3. `typing.Protocol` ports in a provider-free core, with shared contract test
   suites and mechanically enforced layering.

## Decision

Chosen option: 3. Option 1 contradicts the manifest. Option 2 adds machinery
before there is a second adapter.

| Layer | Contents | May import |
| --- | --- | --- |
| `ilmar.domain` | Frozen dataclasses, enums, errors | stdlib |
| `ilmar.ports` | `TaskProvider`, `SourceControlProvider`, `ContainerRuntime`, `AgentRunner`, `ExecutionStore` | stdlib, `domain` |
| `ilmar.core`, `ilmar.config` | State machine; later coordinator and policies | stdlib, `domain`, `ports` |
| `ilmar.adapters` | Implementations | anything |

- Identifiers (`TaskRef.key`, `RepositoryRef.slug`, `ContainerHandle.id`) are
  opaque to the core.
- `AgentRunner.run` receives the `ContainerRuntime`, so any agent runs on any
  runtime. R6 shows a stateless, exec-based agent fits this shape (F-010).
- Neutral vocabulary: "change request" covers pull and merge requests, and
  "checks" covers CI.
- Each port has a contract suite in `tests/contracts/` that adapters and the
  test fakes must pass. Suites exist for `ExecutionStore`, `TaskProvider` and
  `ContainerRuntime`. `SourceControlProvider` and `AgentRunner` suites come with
  their first real adapters in M2.
- `tests/architecture/test_dependencies.py` enforces the layering, as R2 does
  with structural tests (F-008).

## Consequences

- Good: a new provider means one adapter plus one contract suite. Pluggable
  sandboxes are common practice ([R4], [R5], F-011).
- Bad: conversion code at every adapter boundary. Protocols can drift from the
  contract unless contract tests cover them.

## Revisit when

- Third-party adapter packages appear. Then move the contract suites into an
  importable `ilmar.testing` package and add entry-point discovery.
- A port needs provider-specific escape hatches more than once. That signals
  the abstraction is wrong.
