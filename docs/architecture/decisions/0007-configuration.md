# ADR-0007: TOML configuration with per-port adapters and no inline secrets

- Status: Accepted (PR #1, 2026-11-01)
- Date: 2026-11-01
- Sources: [R1], [R11] · Findings: F-014

## Context and problem

MANIFEST §4 requires each adapter to be configurable independently.
Configuration gets committed, pasted into issues and read by agents, so it must
never contain secrets.

## Considered options

1. YAML front matter in a Markdown workflow file, as R1's `WORKFLOW.md` does.
   This needs a YAML parser dependency.
2. Lenient TOML that ignores unknown keys for forward compatibility (R1's
   approach to unknown keys).
3. Strict TOML: unknown keys, missing ports and wrong types are errors.
4. Environment variables only.

For secrets: `$VAR` interpolation in values (R1) or `*_env` keys that name a
variable.

## Decision

Chosen option: 3, with `*_env` keys.

- One file, `ilmar.toml`, parsed with the standard-library `tomllib`. Sections:
  `[ilmar]`, `[limits]`, and `[adapters.<port>]` with `kind` and `options`.
- Strictness catches typos, which matters more now than forward compatibility
  (F-014).
- Option keys that look like secrets (`token`, `password`, `api_key`, …) are
  rejected unless they end in `_env`. This applies to nested tables too.
  `*_env` keeps a secret reference visibly different from a value, whereas a
  `$VAR` string can be mistaken for a literal.
- `ilmar config validate` checks structure. Adapter-specific validation arrives
  with an adapter registry in M2.

## Consequences

- Good: configuration is safe to share, and typos fail loudly.
- Bad: older Ilmar versions will reject newer files. The secret check is a
  naming heuristic.

## Revisit when

- Configuration files must work across Ilmar versions. Then add
  `config_version` and reconsider tolerating unknown keys.
