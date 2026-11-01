# Architecture overview

This document gives a high-level picture of Ilmar. The decisions behind it are
recorded in `docs/adr/`; the founding requirements are in `MANIFEST.md`.

## Shape

```
             ┌──────────────────────── coordinator process (trusted) ───────────────────────┐
             │                                                                              │
 tracker ◄───┤ TaskProvider ◄──┐                                                            │
             │                 │      ┌─────────────── ilmar.core ───────────────┐         │
 SCM ◄───────┤ SourceControl ◄─┼──────┤ state machine · (M2+) scheduler,         │         │
             │                 │      │ coordinator, verification, recovery      │         │
             │ ExecutionStore ◄┤      └──────────────────────────────────────────┘         │
             │   (SQLite)      │                                                           │
             │ ContainerRuntime◄──── AgentRunner                                           │
             └───────┬──────────────────────────────────────────────────────────────────────┘
                     │ start / run / stop
              ┌──────▼──────────── sandbox (untrusted) ─┐
              │ /workspace (host dir) + coding agent    │
              │ no tracker or SCM credentials           │
              └─────────────────────────────────────────┘
```

- **Domain** (`ilmar.domain`): value types such as `Task`, `Execution`,
  `ChangeRequest` and `ContainerSpec`, plus errors. Standard library only.
- **Ports** (`ilmar.ports`): five `Protocol` interfaces. See ADR-0002.
- **Core** (`ilmar.core`): orchestration logic. In M1, only the execution state
  machine (ADR-0004).
- **Adapters** (`ilmar.adapters`): in M1, the SQLite and in-memory execution
  stores (ADR-0005). GitHub, Podman and Claude Code arrive in M2.
- **Config** (`ilmar.config`): TOML loading and validation (ADR-0007).

## Execution lifecycle

See ADR-0004 for the state diagram. In short, an execution is QUEUED when a task
is discovered, CLAIMED once the tracker reserves it, and moves through
PROVISIONING, RUNNING_AGENT, VERIFYING and PUBLISHING to SUCCEEDED. Retries go
through RETRY_WAIT and are bounded. Any non-terminal state can end in FAILED or
CANCELLED. Every change is versioned and logged.

## Trust boundary

The sandbox gets the working copy and nothing else. The coordinator alone talks
to the tracker and source-control platform, and only publishes after
independent verification. See ADR-0006.

## Milestone status

| Milestone | Scope | Status |
| --- | --- | --- |
| M1 Foundation | Ports, SQLite store, state machine, config, contract tests | This change |
| M2 End-to-end | GitHub issue → Podman → Claude Code → tests → PR | Next |
| M3 Reliability | Recovery, duplicate prevention, retries, cancellation, logs, limits | Planned |
| M4 Self-hosting | Ilmar changes its own repository | Planned |
| M5 Portability | Docker plus a second tracker/SCM provider | Planned |
| M6 Public stability | Stable APIs, releases, security review | Planned |

## Not yet designed

Structured event logging, the scheduler and coordinator loop, verification and
approval policies, planning and review stages, network allow-listing for
sandboxes, and the adapter registry. Each will get an ADR when it is built.
