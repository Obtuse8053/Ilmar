# Ilmar

Ilmar is an open-source, provider-independent software factory. It turns
development tasks into verified code changes using existing autonomous coding
agents, and is being built to eventually develop and maintain itself.

> **Status:** pre-alpha. Nothing here runs tasks end to end yet. For current
> progress, see [`docs/STATUS.md`](docs/STATUS.md).

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
- [`docs/architecture/`](docs/architecture/): architecture overview and
  [decision records](docs/architecture/decisions/).
- [`docs/research/`](docs/research/): source registry, findings and the
  research review process.
- [`docs/STATUS.md`](docs/STATUS.md): where development stands and what is next.
- [`docs/workflow.md`](docs/workflow.md): how work is planned, claimed and reviewed.
- [`docs/roadmap.md`](docs/roadmap.md): milestone status and approved improvements.

## Contributing

External contributions are not accepted yet. A Contributor License Agreement
will be set up first (MANIFEST §7.6).

## License

Apache License 2.0. See [`LICENSE`](LICENSE), [`NOTICE`](NOTICE) and
[`THIRD_PARTY_NOTICES.md`](THIRD_PARTY_NOTICES.md).

Names of third-party products appear only to identify integrations. Ilmar is not
affiliated with or endorsed by their owners.
