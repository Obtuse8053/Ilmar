# ADR-0042: A person resolves a quarantined resource, and the row stays quarantined

- Status: Proposed
- Date: 2026-11-14
- Sources: ADR-0020, ADR-0012, ADR-0022 · Findings: none (a design decision)
- Amends (by addition; no accepted text is edited):
  - **ADR-0020:** how "a human resolves it" is recorded for a `QUARANTINED`
    resource, without changing the row.
  - **ADR-0024 §4:** a new extra event, `resource.resolved`, and the reason
    code `quarantine-resolved`.
  - **The `ContainerRuntime` port:** a read, `sandbox_parts`.
- Asked for in #59 (from the #15 audit, 2026-11-08). The maintainer made it an
  explicit Gate 1 dependency on 2026-11-14: a quarantine during the Gate 1
  trial must have a supported way out, not a hand-edited store.

## Context and problem

ADR-0020 makes `QUARANTINED` final for automation. Ilmar never reuses, mounts,
deletes or releases a quarantined resource, and "a human resolves it". But
there is no supported way to do that:

- an execution with a quarantined resource stays `BLOCKED` (exit 2) on every
  pass, and keeps its task's claim;
- in loop mode, exit 2 ends the loop (ADR-0030, as approved);
- the only way out today is editing the SQLite store by hand. That bypasses
  fencing, leaves no record, and could break the event log's agreement with
  the rows.

## Considered options

1. **An append-only resolution event; the row stays `QUARANTINED`.**
   **Chosen.** The event log is already append-only, fenced and verified
   against the rows (ADR-0024). The row keeps meaning "never touch this again".
2. *A new resolution table.* Rejected for now: a schema change for one record
   kind that the event log already holds safely. It also contends with the
   schema numbers in flight (#132).
3. *Let the row move from `QUARANTINED` to a new `RESOLVED` status.*
   Rejected: it breaks ADR-0020's rule that quarantined rows never change, and
   every reader of that status would need re-review.

## Decision

### 1. Two commands, for a person only

- `ilmar quarantine list --config …`: every quarantined resource, its
  execution, its kind and name, and whether it is already resolved.
  Read-only.
- `ilmar quarantine resolve --config … --execution E --resource KEY --as NAME
  --note TEXT`:
  - it takes the instance lock and writes one fenced commit, like every
    mutating command (ADR-0012);
  - the note is short, printable and refused if it looks like a credential.

They are never run by the coordinator, the loop or any scheduled path. The
store refuses a `resource.resolved` event whose actor is not a person, and an
architecture test checks that only the command builds one.

### 2. It verifies before it records

The command records nothing unless what it can check is true now:

- **A sandbox (`container`):** the runtime's read `sandbox_parts(name)` lists
  any remaining part: the container, its egress or gateway sidecar, its
  network. Nothing may remain. A remaining part is listed and the command
  refuses: the person removes it first. A part with another owner, or none,
  is reported and never touched. This command removes nothing.
- **A directory (a workspace, a checkout):** it is absent, or still has the
  recorded identity (device and inode).
- **Any other kind:** refused. There is no way to check it yet.

### 3. What a resolution records

An `execution.`-scoped extra event, `resource.resolved`:

- the execution, the reason `quarantine-resolved` and the person as actor;
- data: the resource key and what was observed (`absent` or
  `identity-matches`);
- the note, through the event log's credential refusal.

The resource row is unchanged and stays `QUARANTINED`. Automation still never
reuses, mounts, deletes or releases it.

### 4. What changes for automation

When every quarantined resource of an execution has a resolution, that
execution is no longer held by them: the next pass finishes it as usual. A
failed or cancelled execution's claim is released, and its workspace is kept.
Nothing else reads the resolution. A resolution for a resource that is not
quarantined is refused.

## Consequences

- A quarantine during the Gate 1 trial has a supported, recorded way out.
- The record of who resolved what, when, why and what was observed is in the
  event log, and `ilmar events` shows it.
- A person can still be wrong. The command checks only what the runtime and
  the filesystem can show now, and says so.

## Tests required

- Resolving a quarantined sandbox whose parts are gone unblocks the
  execution, and the next pass finishes it.
- A sandbox with any part still present is refused, and nothing is written.
- A foreign-owned part is reported and never touched.
- A directory: absent and same identity are accepted; another identity is
  refused.
- The command fails without the instance lock.
- Automation never resolves anything. The store refuses a non-human actor,
  and an architecture test checks who builds the event.
- A resource that is not quarantined, or an unknown one, is refused.

## Revisit when

- A resource kind appears that cannot be checked from the host.
- The schema is next changed for another reason; a resolution table could
  then replace the event.
