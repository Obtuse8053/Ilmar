## Summary

<!-- What changes and why. Link the task or issue. -->

## Acceptance criteria

<!-- The measurable criteria this change meets, and how tests demonstrate them. -->

## Architecture

<!-- Decisions made, ADRs added or updated, deviations from MANIFEST.md. -->

## Sensitive areas

<!-- Does this touch auth, sandbox policy, approval or verification policy, CI or
release automation, AGENTS.md, MANIFEST.md, licence files, or architecture
tests? If so, say which. These need independent verification and maintainer
approval. -->

## Provenance (MANIFEST §7)

- [ ] All code, tests and docs are original to this change, or listed below.
- [ ] External sources used materially: <!-- none, or list with URL/revision -->
- [ ] Third-party material incorporated: <!-- none, or recorded in THIRD_PARTY_NOTICES.md -->
- [ ] New dependencies and their licences: <!-- none, or list -->
- [ ] Uncertain provenance flagged for review: <!-- none, or describe -->

## Checks

- [ ] `ruff check .` and `ruff format --check .`
- [ ] `mypy`
- [ ] `pytest`
- [ ] `python scripts/license_inventory.py`
