# Reference register

This register implements the reference-management requirements in MANIFEST §9.
The canonical list of references, with URLs and licence notes, is MANIFEST §9;
the IDs below refer to it.

Two kinds of use are tracked separately:

- **Conceptual inspiration:** ideas or patterns that informed a design. Recorded
  here and in the ADR concerned. No code or text is reused.
- **Incorporated material:** code, text or assets copied into the repository.
  Recorded in `THIRD_PARTY_NOTICES.md` with exact source, revision and licence,
  after maintainer approval (MANIFEST §7.2).

## Access log

Record each time a reference is consulted for a consequential decision.

| Date | Ref | What was consulted (URL, revision) | Used for | Kind of use |
| --- | --- | --- | --- | --- |
| — | — | — | — | — |

No reference was accessed during the M1 foundation change. The ADRs cite
references only through the summaries in MANIFEST §9, for general concepts
(separating tracker, workspace and agent; sandboxed execution; reconciliation
and recovery; durable progress artefacts; simplicity). When M2 work consults a
reference directly, record the URL, revision or access date and purpose here
before relying on it.

## Usage by ADR

| ADR | References | Kind of use |
| --- | --- | --- |
| 0002 Ports and adapters | R1, R4, R5 | Conceptual |
| 0003 Standard library first | R6 | Conceptual |
| 0004 Execution state machine | R1, R3 | Conceptual |
| 0006 Credentials outside sandbox | R1, R4, R5 | Conceptual |

## Incorporated material

Only the Apache License 2.0 text ([R8]), in `LICENSE`. See
`THIRD_PARTY_NOTICES.md`.
