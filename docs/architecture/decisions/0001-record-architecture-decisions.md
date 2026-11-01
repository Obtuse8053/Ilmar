# ADR-0001: Record architecture decisions

- Status: Proposed
- Date: 2026-11-01
- Sources: [R2], [R10] · Findings: F-007

## Context and problem

MANIFEST §2.7 and §8 require documented, reproducible decisions. Ilmar is built
largely by agents, which start every session cold and need to find *why* the
code is the way it is without chat history.

## Considered options

1. Rely on pull request descriptions and commit messages.
2. A wiki outside the repository.
3. Classic Nygard-style ADRs (context, decision, consequences).
4. Lightweight MADR-inspired ADRs in the repository, with considered options,
   sources and revisit conditions.

## Decision

Chosen option: 4, stored in `docs/architecture/decisions/`, using the template
in this directory's README. Any change to architecture, a port contract, the
persistence format, a security boundary or the dependency policy includes a new
or updated ADR.

Option 4 was chosen because recording alternatives and revisit triggers lets a
later agent tell when a decision is stale (MANIFEST §8 step 9). Keeping the
records in the repository makes them versioned with the code, which R2 reports
as essential for agents. Options 1 and 2 scatter or detach the reasoning.

## Consequences

- Good: decisions can be reviewed, linked to evidence and revisited cheaply.
- Bad: a small documentation cost on structural changes.

## Revisit when

- ADRs are routinely skipped or go stale. Then consider a docs check in CI.

MADR's own template text was not copied. The format here is our own,
lightweight variant.
