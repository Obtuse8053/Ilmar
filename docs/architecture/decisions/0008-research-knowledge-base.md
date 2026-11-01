# ADR-0008: In-repository research knowledge base and review process

- Status: Proposed
- Date: 2026-11-01
- Sources: [R2], [R10] · Findings: F-007, F-016

## Context and problem

Ilmar should improve from evidence about how other software factories work.
Without a home for that evidence, agents either ignore prior art or change the
architecture on unrecorded hunches. MANIFEST §9 lists founding references but
has no room for verification results, evidence quality or follow-up.

## Considered options

1. Extend MANIFEST §9 directly whenever something is learned.
2. Keep research in GitHub issues and discussions only.
3. A small in-repository knowledge base: a source registry, findings with
   evidence levels, ADRs and a roadmap, plus a periodic review process.

## Decision

Chosen option: 3, in `docs/research/` and `docs/roadmap.md`. The process is
described in `docs/research/README.md`.

- The manifest stays the founding specification and is not duplicated. The
  registry continues its IDs (R1–R8) and adds verification details and new
  sources (R9 onwards).
- Findings separate observation from implication and carry an evidence level
  (Established, Emerging, Experimental). Self-reported results from a single
  organisation are at most Emerging.
- Changes flow from finding to issue to approval to ADR or roadmap item to pull
  request. Agents do not change the architecture silently (AGENTS.md §3a).
- A monthly research review is designed but not automated. It may become an
  Ilmar task after M4.
- `tests/architecture/test_docs.py` checks that relative links resolve and that
  every `[R<n>]` cited exists in the registry.

Option 1 would churn a sensitive file. Option 2 leaves agents without
versioned, local context, which R2 identifies as essential.

## Consequences

- Good: decisions can be traced to evidence, and stale decisions have explicit
  triggers.
- Bad: upkeep cost. Mitigated by keeping entries short and dropping irrelevant
  items during triage.

## Revisit when

- Reviews produce nothing actionable for three months in a row. Then reduce the
  cadence.
- The knowledge base outgrows flat files. Then consider structured data.
