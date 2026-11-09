# Cancelling an execution

ADR-0025 defines cancellation. This page describes what exists so far
(#16 part 1): asking for a cancellation and recording it. Stopping a running
agent (part 2) and the transition to `cancelled` (part 3) are not
implemented yet. Until then an accepted request is recorded, and nothing else
happens to the execution.

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
