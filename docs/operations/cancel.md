# Cancelling an execution

ADR-0025 defines cancellation. You ask with `ilmar cancel`. The coordinator
applies the request at its next safe point and stops the execution. That is
in the running pass, or at the next `ilmar run --once`.

## `ilmar cancel`

```sh
ilmar cancel x18b2c4d6e8f0 --config ilmar.toml --as alice
```

- **What it does:** it writes one request file into the coordinator's
  private inbox, `<state_dir>/coordinator/requests/cancel/<execution>.json`.
  It takes no instance lock and never writes the execution store, so it works
  while a coordinator is running.
- **`--as`:** a short lower-case label recorded as the human actor. It is
  not a login and is never a credential.
- **Who applies it:** the coordinator's worker for that execution applies
  the request. It records `execution.cancel_requested` (reason
  `cancel-requested`) with one decision:
  - `accepted`;
  - `too-late`: a publish was already intended;
  - `already-terminal`;
  - `duplicate`: a cancellation was already accepted.
  Read the record with `ilmar events --execution ID --type execution.cancel_requested`.
- **First request wins:** a second `ilmar cancel` while a request is
  pending publishes nothing and reports the pending one. It never
  overwrites it.

## What happens to the execution

The coordinator checks for a request before every step. It also checks
before a freeze, before a verification run and before the first push.
While the agent runs, a watcher checks every 2 seconds.

| When the request is applied | What happens |
| --- | --- |
| Before or right after the claim | Nothing more starts. A claim it holds is released. |
| While provisioning | No sandbox is started. The attempt's workspace is kept. |
| Before the agent runs | A started sandbox is removed. The agent never runs. |
| While the agent runs | The agent is stopped the way a timeout stops it: its sandbox, egress proxy and network are removed and confirmed gone. Its output is never frozen. The workspace is kept for inspection. |
| After the agent finished | Its output is never frozen. |
| While verifying | A verification run in progress finishes, within its own timeout. No further run starts. The evidence is kept. |
| Before the first push | Nothing is pushed. The candidate and the evidence are kept. |
| After the first push started | **Too late:** the request is recorded as `too-late`, and publishing completes exactly once. Close the change request to release the task. |
| After the execution finished | Recorded as `already-terminal` by the next pass, under the instance lock and the execution's lease. The file is removed. Nothing else changes. |

**How it ends:** the execution ends `cancelled`, with the reason code
`cancel-requested`. If the task was claimed, Ilmar posts one note on it and
releases the claim.

**When cleanup fails:** if an error interrupts the cleanup, the coordinator
runs the whole cleanup again. After three failed tries in one pass, it
reports the execution blocked and leaves it as it is. The next pass
continues. A cancellation never becomes a failure or a new attempt.

**When a sandbox cannot be confirmed removed:** the sandbox is quarantined.
The execution is reported blocked, and the claim is kept for a person.

**Starting the task again:** a task cancelled this way is never started again
automatically. Resetting it is #12.

**What `ilmar run --once` reports:** a cancelled execution is reported as
`cancelled [cancel-requested]` and exits 0. If an inbox entry was moved
aside, the line says so.

## Exit codes

| Code | Meaning |
| --- | --- |
| 0 | A request was published, or one is already pending or accepted. |
| 3 | Any of: a configuration or usage problem; an unknown or already finished execution; an unsafe inbox; an invalid entry in the inbox. |

## The inbox

The inbox must be private. Each level is owned by the coordinator's user
and has mode `0700`. Every request file has mode `0600`. Nobody else may
write the coordinator root.

- **What is refused:** the coordinator refuses any entry that is not a
  small, regular, private file holding exactly one canonical request for
  that execution. That includes symlinks, FIFOs, directories, extra hard
  links, malformed or non-canonical JSON, and a wrong execution or actor.
- **What happens to a refused entry:** it is moved to `rejected/`, reported,
  and never acted on.
- **A concurrent change:** if an entry changes between the coordinator's
  check and its cleanup, the entry is preserved in `rejected/`, never
  deleted. If it held a valid request, the report asks you to resubmit it.
- **Hard links:** the inbox's filesystem must support hard links. Without
  them, `ilmar cancel` refuses rather than risk overwriting a request.
