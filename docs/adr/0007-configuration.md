# ADR-0007: TOML configuration with per-port adapters and no inline secrets

- Status: Proposed
- Date: 2026-11-01

## Context

MANIFEST §4 requires adapters to be independently configurable. Configuration
files get committed, pasted into issues and read by agents, so they must never
contain secrets.

## Decision

- One TOML file (`ilmar.toml`), parsed with the standard-library `tomllib`. See
  `ilmar.example.toml`.
- Sections: `[ilmar]` (general settings), `[limits]` (attempts, concurrency,
  agent timeout) and `[adapters.<port>]` for each of the five ports, each with
  a `kind` and an adapter-specific `options` table.
- Validation is strict about structure: unknown keys, missing ports and wrong
  types are errors. A typo should fail loudly rather than be silently ignored.
- Option keys that look like secrets (`token`, `password`, `api_key`,
  `secret`, ...) are rejected unless they end in `_env`, which means the value is
  the *name* of an environment variable. This applies to nested tables too.
- Relative paths in `[ilmar]` resolve against the configuration file's directory.
- `ilmar config validate` checks a file. It does not check that an adapter
  `kind` is installed. An adapter registry, which will validate `options`
  against each adapter, arrives with the first real adapters in M2.

## Consequences

- Configuration can be committed and shared safely.
- The secret check is a name heuristic. It catches the common mistake, not
  every possible one.
- Adapter options are untyped mappings until each adapter validates its own.

## Prior art

None specific.
