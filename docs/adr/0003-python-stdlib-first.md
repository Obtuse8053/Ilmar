# ADR-0003: Python 3.11+, standard library first, no runtime dependencies in the core

- Status: Proposed
- Date: 2026-11-01

## Context

MANIFEST §3 selects Python and §2.5 asks for minimal infrastructure. Every
dependency adds licence review work (MANIFEST §7.4), supply-chain risk and
upgrade churn. The M1 core needs only data classes, an SQLite driver, TOML
parsing and a CLI, all of which the standard library provides.

## Decision

- Require Python 3.11 or newer, for `tomllib`, `typing.Self` and modern typing
  syntax. CI tests 3.11, 3.12 and 3.13.
- The `ilmar` distribution has **no runtime dependencies**. A test enforces
  `project.dependencies == []`. An adapter that needs a library declares it as
  an optional extra, for example `ilmar[github]`, and is reviewed when added.
- Development tooling, all permissively licensed and not distributed:
  - **pytest** for tests;
  - **ruff** for linting and formatting;
  - **mypy** in strict mode for type checking;
  - **setuptools** as the build backend.
- `scripts/license_inventory.py` (standard library only) lists the licences of
  installed distributions and fails on licences incompatible with distributing
  Ilmar under Apache-2.0. CI runs it.
- Every Python file carries an `SPDX-License-Identifier: Apache-2.0` header,
  enforced by a test.

## Consequences

- `pip install ilmar` pulls nothing else, and the core can be audited on its own.
- Some conveniences (pydantic validation, rich CLIs) are written by hand, kept
  small, and tested.
- The licence check is heuristic. It reads declared metadata and cannot detect
  mislabelled packages. It supplements human review and does not replace it.
- Not yet set up: secret scanning (see the PR notes) and SHA-pinning of GitHub
  Actions.

## Prior art

- [R6] mini-SWE-agent: simplicity as a design benchmark (MANIFEST §9). Ideas
  only; not accessed during M1.
