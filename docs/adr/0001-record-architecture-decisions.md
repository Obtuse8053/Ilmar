# ADR-0001: Record architecture decisions

- Status: Proposed
- Date: 2026-11-01

## Context

MANIFEST §2.7 and §8 require architecture and engineering decisions to be
documented and reproducible. Ilmar will be developed largely by coding agents,
including eventually by itself. An agent starting cold needs to discover *why*
the code looks the way it does without relying on chat history or the memory of
whoever made the decision.

## Decision

Keep short, numbered architecture decision records in `docs/adr/`, one decision
per file, written in Markdown using the template in `docs/adr/README.md`. Every
change that alters the architecture, a port contract, the persistence format, a
security boundary or the dependency policy includes a new or updated ADR.

Each ADR has a "Prior art" section that cites the reference IDs from MANIFEST §9
and states whether only ideas were used. This is how the IP policy's
requirement to document architectural inspiration (MANIFEST §7.1) is met.

## Consequences

- Reviewers and agents can check a change against the recorded decisions.
- Decisions are cheap to revisit: write a superseding ADR.
- There is a small documentation cost on every structural change.

## Prior art

The ADR practice is a widely used engineering convention. The format here is
written for this project; no template text was copied.
