# ADR-0003: Python 3.11+, standard library first, no runtime dependencies in the core

- Status: Accepted (PR #1, 2026-11-01)
- Date: 2026-11-01
- Sources: [R6]

## Context and problem

MANIFEST §3 selects Python and §2.5 asks for minimal infrastructure. Every
dependency adds licence review (MANIFEST §7.4), supply-chain risk and upgrade
churn. M1 needs only dataclasses, SQLite, TOML and a CLI.

## Considered options

1. A typical application stack: pydantic, typer/click, SQLAlchemy.
2. Standard library only in the core, with optional extras per adapter.

Build backend options: setuptools, hatchling, uv_build.

## Decision

Chosen option: 2, with setuptools as the build backend.

- Python 3.11 or newer (for `tomllib`, `typing.Self` and modern typing). CI
  tests 3.11, 3.12 and 3.13.
- `project.dependencies == []`, enforced by a test. Adapters declare extras
  such as `ilmar[github]`.
- Dev tools: pytest, ruff, mypy (strict), all MIT licensed and not
  distributed.
- `scripts/license_inventory.py` flags incompatible dependency licences in CI.
  It is a heuristic and does not replace human review.
- Every Python file has an SPDX header (enforced).
- setuptools was chosen because it could be verified offline in the build
  environment. The choice has no runtime effect.

R6 shows that a small, dependency-light design can still be capable.

## Consequences

- Good: `pip install ilmar` pulls nothing else, and the core can be audited on
  its own.
- Bad: validation and CLI code are written by hand. No secret scanning or
  SHA-pinned Actions yet.

## Revisit when

- Hand-written validation or CLI code grows large enough that a library would
  remove more code than it adds.
- A supported Python version reaches end of life. Raise the floor.
