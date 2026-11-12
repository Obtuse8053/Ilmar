# ADR-0038: Scan the candidate and outgoing text for the deployment's own secrets

- Status: Proposed
- Date: 2026-11-12
- Sources: none external · Findings: none (a design decision, not a research finding)
- Amends (by addition; no accepted text is edited):
  - **ADR-0010 §5:** a new verdict, `rejected-candidate-secret`, and a new
    independent check, `candidate-secrets`.
  - **ADR-0018:** a scan checkout inside the verification run directory.
  - **ADR-0024 §4:** a new reason code, `candidate-secret`.
- Asked for by the maintainer on 2026-11-11 (#110): a mandatory Gate 1
  prerequisite. The scan is deterministic, never exposes a secret's value and
  must not be described as detecting arbitrary transformations.

## Context and problem

Until the sidecar gateway (ADR-0037, #111) keeps the model credential out of
the sandbox, the agent can read that credential. Nothing stopped it from
writing the value into a file, a path name or its own summary. Ilmar would
then freeze, verify and publish the file, and copy the summary into the
change request and the tracker note. Logs and adapter errors are redacted;
published content was not checked.

## Considered options

1. **Scan the frozen candidate inside verification, and every outgoing text
   before it leaves.** **Chosen.**
2. *Scan the agent's workspace before freezing.* Rejected: ADR-0010 requires
   that what is checked is exactly what is published. The workspace is the
   agent's to change; the frozen revision is not.
3. *Scan only at publication.* Rejected: the candidate's code would already
   have run in verification sandboxes for nothing, and a finding would be
   discovered only after the evidence said "publish".
4. *A general-purpose secret detector (entropy, provider key patterns).*
   Rejected for now: it would flag the candidate's legitimate test fixtures
   and needs tuning and review that Gate 1 does not. This decision looks only
   for the values this deployment actually holds.

## Decision

### 1. What is looked for

A `SecretScanner` (core, standard library only) is built at startup from the
value of every environment variable an adapter plan reads as a secret: the
source-control token, the model credential and any other `*_env` secret. A
value shorter than 8 bytes is refused as a configuration error, because it
would match by chance.

Each value is looked for byte for byte in these encodings:

- raw;
- base64, standard and URL-safe, at each of the three byte alignments, with
  or without padding;
- hexadecimal, lower and upper case;
- percent-encoding (`quote` and `quote_plus`);
- JSON string escaping.

**Not detected:** any other transformation. A value split, reversed,
re-wrapped (MIME line breaks), mixed-case hex, compressed, encrypted or
re-encoded twice passes. This is a guard against the plain and common ways
of copying a value, not a data-loss-prevention system. Keeping the value out
of the sandbox (ADR-0037) is the real control.

### 2. The candidate

At the start of each verification run, before any check runs, the frozen
candidate revision is checked out with `checkout_revision` into
`<run directory>/.secret-scan`. That name can never be a job name, and the
run directory is already a tracked, cleaned-up resource. For every changed
path, the scanner checks:

- the path's name;
- a symlink's target, never following it;
- a regular file's bytes, read in 1 MiB chunks with an overlap, so a value
  across a chunk boundary is still found.

Deleted paths, directories (submodules) and paths under a symlinked directory
have no content to read.

Unchanged files are not scanned. A value the base branch already contains was
not added by the agent.

A finding adds a failed independent check, `candidate-secrets`, and gives the
verdict `rejected-candidate-secret`. This wins over every other row of the
ADR-0010 §5 table, `rejected-ci-config` included, and nothing of the
candidate runs.

The execution fails without a retry, with reason code `candidate-secret`.
The check's detail in the evidence names the variable, the encoding and the
path, never the value; a finding in a path name does not repeat the path. The
failure reason, which becomes the tracker note, names neither paths nor
values.

When no secret is configured, nothing changes: there is no scan checkout and
no check.

### 3. Outgoing text

- **The change request.** Before the first push, the branch name, the title
  and the description are scanned. The description includes the agent's
  summary. A finding fails the execution with `candidate-secret`; nothing is
  pushed and no change request is opened.
- **Tracker notes.** Every note is scanned before it is posted. A note that
  contains a secret, such as a failure reason quoting the agent's summary, is
  replaced by a fixed text that names only the variables.

### 4. Persistence

`rejected-candidate-secret` is a new value of the evidence verdict.
`EVIDENCE_FORMAT` stays 2: the field is unchanged, and records written before
this change still read. An older Ilmar cannot read a record with the new
value; downgrading is not supported.

## Consequences

- Gate 1's "no configured secret is published in a candidate" requirement
  has a deterministic, tested check, with its limits stated.
- Every verification run with secrets configured makes one more checkout, and
  reads the changed files once.
- A candidate that legitimately needs a value equal to a deployment secret
  cannot be published. That is intended.
- **Rotation.** The scanner holds the values read at startup. If a credential
  is rotated between an agent run and its verification, the scan looks for
  the new value, not the old one the agent saw. Revoking the old value on
  rotation is the operator's control.
- **Local records keep the value.** The agent's summary is kept, unredacted,
  in the coordinator's own store: the agent-run operation and the evidence's
  agent report. The failure reason of an `agent-failed` or `provider-refused`
  execution, recorded locally, also keeps it. These records are on the
  operator's machine, which already holds the secret, and nothing in them is
  published. Redacting them is a separate change, if it is wanted.

## Tests required

- Each encoding, every base64 alignment, the chunk boundaries, the minimum
  length, and that no representation of a scanner or finding holds a value
  (`tests/unit/test_secret_scan.py`).
- Path names, symlink targets (not followed), paths under a symlinked
  directory, and deleted paths.
- Verifier: the verdict, that nothing runs, that the scan wins over the CI
  rejection, and that only changed paths are scanned
  (`tests/unit/test_verifier.py`).
- Coordinator end to end: a candidate with an encoded secret, a summary with a
  secret, and a withheld tracker note. In each, nothing is pushed or opened,
  and no event, transition or note holds the value
  (`tests/unit/test_secret_scan_coordinator.py`).
- Registry: every configured secret is scanned for, and a too-short secret is
  refused without its value (`tests/unit/test_registry.py`).

## Revisit when

- ADR-0037's gateway keeps every model credential out of the sandbox. The
  scan then still guards the source-control token and stays as defence in
  depth, but it is no longer the primary control.
- A candidate needs a broader detector (provider key patterns, entropy) for
  secrets the deployment does not itself hold. Mode 2 with untrusted
  repositories may.
- A secret is ever found in a published change despite this check. Record
  the encoding and extend the list.
