# Ilmar

Ilmar is an open-source, provider-independent software factory. It turns
development tasks into verified code changes using existing autonomous coding
agents, and is being built to eventually develop and maintain itself.

> **Status:** pre-alpha, milestone M1 (Foundation). Nothing here runs tasks end
> to end yet.

## What exists today

- Provider-neutral domain model and port interfaces for task tracking, source
  control, container runtimes, coding agents and execution storage.
- The execution state machine, with bounded retries and optimistic concurrency.
- SQLite and in-memory execution stores that prevent duplicate active executions
  per task.
- TOML configuration with per-port adapter selection.
- Contract test suites that every adapter must pass.

The first targets are GitHub, Podman and Claude Code (milestone M2). The core
does not depend on any of them.

## Getting started

```sh
python -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
pytest
ilmar config validate ilmar.example.toml
```

## Documentation

- [`MANIFEST.md`](MANIFEST.md): mission, principles and roadmap.
- [`AGENTS.md`](AGENTS.md): development instructions for humans and agents.
- [`docs/architecture.md`](docs/architecture.md): architecture overview.
- [`docs/adr/`](docs/adr/): architecture decision records.
- [`docs/references.md`](docs/references.md): prior art and reference register.

## Contributing

External contributions are not accepted yet. A Contributor License Agreement
will be set up first (MANIFEST §7.6).

## License

Apache License 2.0. See [`LICENSE`](LICENSE), [`NOTICE`](NOTICE) and
[`THIRD_PARTY_NOTICES.md`](THIRD_PARTY_NOTICES.md).

Names of third-party products appear only to identify integrations. Ilmar is not
affiliated with or endorsed by their owners.
