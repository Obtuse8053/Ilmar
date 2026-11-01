# Third-party notices

This file records third-party material incorporated into the Ilmar repository,
as required by MANIFEST §7.2. Conceptual inspiration that did not involve copying
code or text is recorded in `docs/research/sources.md` instead.

## Incorporated material

| Material | Source | Revision | License | Location in repo | Modifications | Approved by |
| --- | --- | --- | --- | --- | --- | --- |
| Apache License, Version 2.0 (license text) | Apache Software Foundation, https://www.apache.org/licenses/LICENSE-2.0 | Version 2.0, January 2004 | Reproduced verbatim, as the license permits for its own text | `LICENSE` | None | Pending maintainer review |

No third-party source code is incorporated.

## Development-only tools

These tools are used to develop and test Ilmar. They are not distributed with
Ilmar and are not runtime dependencies. Licenses as declared in each package's
published metadata at the time of adding them:

| Tool | Purpose | License |
| --- | --- | --- |
| pytest | Test runner | MIT |
| ruff | Linter and formatter | MIT |
| mypy | Static type checker | MIT |
| setuptools | Build backend | MIT |

## Runtime dependencies

None. See ADR-0003.
