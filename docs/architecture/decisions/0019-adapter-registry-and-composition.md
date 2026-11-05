# ADR-0019: Adapter registry, two-phase construction and the `Components` bundle

- Status: Accepted (maintainer approval on PR #46, 2026-11-05)
- Date: 2026-11-05
- Sources: [R1], [R11] · Findings: F-014
- Implements: #10; completes ADR-0007's "adapter-specific validation arrives
  with an adapter registry"

## Context and problem

ADR-0007 defines `ilmar.toml` with one `[adapters.<port>]` table per port, a
`kind` and `options`, but validates only structure. Before the coordinator
(#11) can run, something has to:
- turn each `(port, kind)` into a working adapter;
- validate every adapter's options strictly, also in `ilmar config validate`;
- read secrets from the environment without ever showing them;
- check the rules *between* ports (an agent that needs network on a runtime
  that cannot filter it);
- own the adapters' lifecycle, including after a partial failure;
- stop configuration from weakening the sandbox or the verification policy;
- do all this without the coordinator naming a concrete adapter, so a new
  adapter needs no coordinator change.

The layer rules (ADR-0002) forbid `core` and `config` from importing
adapters.

## Considered options

Where construction lives:
1. In `ilmar.config`. It may not import adapters (ADR-0002).
2. In the coordinator. It would name every concrete adapter.
3. **In `ilmar.adapters.registry`, a composition root in the adapter layer.**
   It hands the coordinator a provider-neutral `ilmar.core.components.Components`.

How kinds are found:
1. Imports of every adapter at the top of the registry. Every adapter would be
   loaded, and an optional extra that is not installed would break all
   configurations.
2. Python entry points, which allow plug-ins from other distributions. Nothing
   needs this yet.
3. **A table of `(port, kind) → "module:function"` references, imported only
   when that kind is configured.** `Registry.extended()` adds entries, which
   is how tests and future adapters plug in.

One phase or two:
1. Construct while parsing. `config validate` would then need secrets,
   executables and a writable state directory.
2. **Parse, then create.** Parsing is pure. Creation reads secrets and builds.

## Decision

- **Parsers and plans.** Each kind has a parser `(Options, BuildContext) →
  Plan`. `Options` is a strict reader: typed getters, and `finish()` rejects
  any key the parser did not read. A `Plan` holds:
  - `create(secrets)` and an optional `close(adapter)`;
  - the environment variable names it reads;
  - the facts the registry checks across ports.

  Port-specific facts:
  - `source_control` must give a `Target`, the repository and base branch;
  - `agent_runner` must give an `AgentSandbox`: image, network and the
    allowed hosts;
  - `container_runtime` lists the network policies it can enforce;
  - `task_provider` may give the repository its tasks belong to.
- **`plan(config)`** parses every port, then checks the rules between ports.
  It reads no secret, touches no file and starts nothing.
  `ilmar config validate` runs it. `--check-environment` additionally reports
  which named variables are set, by name only.
- **`build(config, environ)`** runs `plan`, then creates the adapters in port
  order. After creating each adapter, it checks that the object can serve
  as its port:
  - every member of the port's protocol is present;
  - properties are readable, and `name` is a non-empty string;
  - methods are callable;
  - every parameter the port passes is accepted under the same name and
    kind (keyword-only stays accepted by keyword);
  - no extra required parameter exists.

  The source-control adapter's `name` must equal its target's provider. It
  returns `Components`, which the caller owns:
  - `close()` releases every resource in reverse order, exactly once;
  - every closer is called even if one fails, and the failures are reported
    together;
  - a context manager closes on exit.

  If anything fails during `build`, everything already created is closed in
  reverse order before the error propagates. A failure during that cleanup is
  attached to the original error as a note and never replaces it.
- **Secrets.** Secrets are read only in `create`, through `Secrets.get`, from
  variables named by `*_env` options (ADR-0007). Errors:
  - the registry's own messages name sections, keys and variables, never
    configured values. Validation messages from adapters may quote a
    non-secret value; secrets cannot be in options;
  - *every* exception from `create`, of any type, becomes a `ConfigError`
    naming the original type. Every secret value read so far is replaced by
    `[redacted]`, including in cleanup notes;
  - errors are raised `from None`, so no chained traceback carries the
    original text.
- **The agent's credential stays separate (ADR-0006).** A plan marks secrets
  that are handed into a sandbox (`enters_sandbox`), which today is the model
  credential. Such a variable must not also be a task-tracker or
  source-control variable: `plan` checks this by name. `build` checks, before
  creating anything, that its *value* differs from every other secret's.
  Otherwise a configuration slip would put the GitHub token into the agent
  sandbox.

  `Components.__repr__` shows no adapter internals.
- **Fail-closed rules.**
  - An unknown kind, a missing module, a reference that is not a parser, a
    parser that returns no plan, an unknown or ill-typed option, a missing
    required option, or a missing secret or executable is a `ConfigError`.
  - `[verification]` is required. Without it, nothing could be verified or
    published.
  - The agent sandbox's network must be one the runtime can enforce. For
    Podman, `ALLOWLIST` requires `proxy_image`. Every runtime must support
    `NONE`, which verification uses.
  - The task provider and source control must name the same repository.
  - `[ilmar] instance_id` must be a safe identifier.
  - The execution store must not be under `workspaces/`, where agents and
    verification checkouts write.
  - The configuration file itself must not be under `workspaces/`. It must
    not be writable by group or others, and must be owned by the running
    user or root. The verification policy therefore always comes from
    operator-controlled configuration, never from a candidate or another
    local user (ADR-0018).
  - Host executables (`podman`, `git`) are a bare name or an absolute
    path. Bare names are looked up on *absolute* `PATH` entries only, so a
    `.` entry cannot pick up a program planted in a repository checkout.
    The result is resolved once, at `build`. It must not lie under
    `workspaces/`, including through a symlink.
- **Nothing that weakens the sandbox is configurable.**
  - The Podman options are the binary, `proxy_image`, `egress_network`,
    default `cpus`, `memory_mb` and `max_processes`, and `tmpfs_mb`.
    `allow_unrestricted_network`, `network`, `privileged`, `uid`, `gid`,
    `user`, `userns`, `init`, `keepalive`, `home`, capabilities,
    `security_opt`, `seccomp`, `volumes`, `mounts`, `devices` and
    `extra_hosts` are refused with a message that says they would weaken
    the sandbox.
  - The agent sandbox's allowed hosts come from the agent adapter:
    `api.anthropic.com`, or the host of `base_url` for a gateway.
    `allowed_hosts` and `network` options are refused.
  - URLs must be `https`, without credentials, query or fragment.
    Executables may not start with `-`.
- **Consistency with verification.** The source-control adapter refuses to
  publish changes to the policy's literal CI-configuration paths, the same
  ones verification rejects (ADR-0016, ADR-0018). Glob patterns cannot be
  expressed as paths there, and verification still catches them.
- **Configuration changes.**
  - `[ilmar]` gains `instance_id` (default `default`).
  - The agent image moves from the runtime's options to the agent runner's
    options, where it belongs.
  - The SQLite store path defaults to `<state_dir>/executions.sqlite3`.
    Relative paths resolve against the configuration file's directory.
- **Built-in kinds:**
  - `task_provider.github`;
  - `source_control.github`;
  - `container_runtime.podman`;
  - `agent_runner.claude-code`;
  - `execution_store.sqlite`.

  The in-memory store is not registered: a coordinator without durable
  state cannot recover (ADR-0012).

## Verification

- Unit tests (`tests/unit/test_registry.py`, `tests/unit/test_cli.py`):
  - every built-in kind is built from options, and the instance id, layout,
    limits and CI paths reach the adapters;
  - the agent sandbox reaches only the model endpoint, or a gateway's host;
  - the policy is exactly the configured object;
  - required, unknown and ill-typed options for every kind;
  - every sandbox-weakening key is refused;
  - configured values are never repeated in errors;
  - each rule between ports, and an instance id, store path or configuration
    file in an unsafe place;
  - an unknown kind, a missing module, a non-parser and a parser returning no
    plan;
  - a missing secret named without values;
  - adversarial cases:
    - errors of any type redacted;
    - a model-credential variable, or value, shared with a tracker or SCM
      credential;
    - a planted `podman` reached through a relative `PATH` entry;
    - relative executable paths;
    - an executable that resolves into a workspace through a symlink;
    - five broken interface shapes;
    - a source-control adapter that does not own its target;
    - a group-writable or foreign-owned configuration file.

    Mutation checks: each of these fixes, reverted on its own, fails at
    least one test;
  - lifecycle, with fake parsers: reverse cleanup after a failure part-way;
    a failing cleanup kept as a redacted note; a non-adapter refused and
    cleaned up; `close()` reaching every resource, once, even when one
    fails;
  - no adapter module imported until configured;
  - the CLI prints secret names, never values.
- Real Podman (CI, `tests/integration/test_registry_podman.py`):
  - components built from a TOML file run a sandbox that is non-root, has
    loopback only, a read-only root and the configured memory limit;
  - the agent sandbox from configuration goes through the egress proxy and
    leaves nothing behind;
  - the SQLite store persists across builds;
  - a wrong Podman binary fails closed without leaking the secret.

## Consequences

- Good:
  - The coordinator depends only on ports and `Components`. A new adapter
    is a parser and one registry entry.
  - `config validate` catches option mistakes without secrets or side
    effects.
  - Partial initialisation cannot leak resources, and errors cannot leak
    secrets that went through `Secrets`.
- Bad:
  - Redaction only covers secrets read through `Secrets`, in the exact form
    read. An adapter that reads a secret another way, or a message that
    contains a transformed secret (base64 in an HTTP header, for example),
    is not covered. The built-in adapters never put those into errors.
  - The port check covers presence, kind and parameters, not types or
    behaviour. The contract suites and mypy cover the built-in kinds.
  - Some settings are trusted, not checked:
    - the `podman` and `git` binaries and the proxy image (a permissive
      proxy image would defeat the allow-list);
    - the GitHub URLs, which receive the token;
    - `base_url`, whose host the agent sandbox may reach.

    They are the operator's, in a file only the operator can write.
  - Executables are resolved once, at `build`. Replacing the binary at that
    path afterwards is outside Ilmar's control.
  - Widening the agent sandbox (for example, a package registry the agent
    needs) is impossible without a code change. That is deliberate for now.
  - Strictness rejects configuration files written for newer versions
    (ADR-0007).

## Revisit when

- Adapters ship as separate distributions. Then use entry points.
- An agent legitimately needs more hosts than its model endpoint. Then add
  an explicit, reviewed opt-in, not a free-form list.
- Tasks and changes must live in different repositories.
- Several coordinators share one host or state directory.
- A command that changes anything (`ilmar run`, #11) is added. It must take
  an explicit configuration path, not default to `./ilmar.toml` in the
  working directory, which may be a repository checkout.
