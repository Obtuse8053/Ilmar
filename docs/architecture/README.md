# Architecture overview

This document gives a high-level picture of Ilmar. The decisions behind it are
recorded in [`decisions/`](decisions/), the evidence behind those decisions is in
[`../research/`](../research/), and the founding requirements are in
`MANIFEST.md`.

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
- **Core** (`ilmar.core`): orchestration logic: the execution state machine
  (ADR-0004), the verification stage (ADR-0018), and `Components`, the
  provider-neutral bundle of configured ports the coordinator runs with
  (ADR-0019), ownership and the write-ahead journal (`ownership`,
  `journal`, ADR-0020), and the coordinator: `coordinator`, `worker`, and
  `effects`, the only core code that changes the outside world (ADR-0021).
  Every worker step is decided from durable state, so recovering after a
  crash and running normally are the same code (ADR-0022).
- **Adapters** (`ilmar.adapters`): SQLite and in-memory execution stores
  (ADR-0005), GitHub tasks and source control (ADR-0013, ADR-0016), Podman
  (ADR-0014, ADR-0015) and Claude Code (ADR-0017). `ilmar.adapters.registry`
  is the composition root: it maps `(port, kind)` to lazily imported parsers
  and builds `Components` from `ilmar.toml` (ADR-0019).
- **Config** (`ilmar.config`): TOML loading and structural validation
  (ADR-0007). Adapter options are validated by the registry.

## Execution lifecycle

See ADR-0004 for the state diagram. In short, an execution is QUEUED when a task
is discovered, CLAIMED once the tracker reserves it, and moves through
PROVISIONING, RUNNING_AGENT, VERIFYING and PUBLISHING to SUCCEEDED. Retries go
through RETRY_WAIT and are bounded. Any non-terminal state can end in FAILED or
CANCELLED. Every change is versioned and logged.

## Trust boundary

The sandbox gets the working copy and nothing else. The coordinator alone talks
to the tracker and source-control platform, and only publishes after
independent verification. See ADR-0006. The sandbox profile (privileges,
mounts, egress allow-list) is ADR-0011. ADR-0010 binds verification and
publication to one immutable candidate revision. ADR-0012 keeps every external
side effect recoverable.

## Host directory layout

Computed purely by `ilmar.core.layout.WorkspaceLayout`. No task text ever
reaches a path.

```
<state_dir>/
├── workspaces/                     may be mounted into sandboxes
│   └── <execution-id>/<attempt>/
│       ├── agent/                  agent workspace (agent sandbox)
│       └── verify-<run>/           fresh checkout per verification run
└── coordinator/                    never mounted
    ├── instance.lock               exclusive flock held for the coordinator's life (ADR-0020)
    └── repos/<provider>/<hash>/    coordinator-owned git repository
```

- `ilmar.core.naming` derives names from identifiers:
  - the branch, `ilmar/<slug>-<hash>`;
  - the container, `ilmar-<instance>-<execution>-<attempt>`.

  Every slug uses only `[A-Za-z0-9._-]` and ends in a hash of the full task
  reference, so hostile or look-alike task keys cannot traverse directories or
  collide.
- `WorkspaceLayout.check_mountable` is a lexical pre-check only: it refuses
  anything outside `workspaces/` and anything under `coordinator/`. The gate
  before every mount is `ilmar.core.mounts.authorize_mount`, which also walks
  the real filesystem from a canonical root with `O_NOFOLLOW` and requires the
  recorded directory identity.
- `ilmar.core.safe_fs` creates, verifies and deletes these directories one
  component at a time without following symlinks. Deletion is bound to the
  verified directory's open descriptor rather than its name (ADR-0012
  clarification). The resource ledger (#11) decides which directories an
  execution may delete.

## Milestones

Milestone scope and status are tracked in [`../roadmap.md`](../roadmap.md).

## Event log

ADR-0024. Every write to the execution store appends its events to an
append-only `events` table **in the same transaction**: the mandatory events
of a `Commit` (one per changed row, derived in `ilmar.domain.events` and
paired with its row by `ilmar.ports.event_log` before `COMMIT`),
`evidence.recorded` and `ownership.acquired`. Coverage starts at the first
`claim_instance`, which runs only under the instance lock: it migrates the
database and writes the baseline of what already exists, in one transaction.
A read-only open (`SQLiteExecutionStore.open_read_only`) never migrates and
never writes. `ilmar.domain.event_history` checks invariant I11: the events
rebuild the stored rows.

The core supplies what rows do not hold (`ilmar.core.event_context`): a
random `pass` id per coordinator pass, each effect's duration measured with the
monotonic clock, the error class and stable reason code of every failure
(`Commit.reasons` carries a transition's or quarantine's code with the change),
and what each reconciliation observed. No supported path writes an
`unclassified`, `invalid`, `redacted` or `truncated` event; the test suite
checks this, and I11, after every coordinator pass and every crash point.

## Not yet designed

A long-running scheduler loop, approval policies,
and planning and review stages. Each will get an ADR when it is
built.
Open research findings that bear on these are listed in
[`../research/findings.md`](../research/findings.md), and their scheduling is in
[`../roadmap.md`](../roadmap.md).
