# ADR-0035: Agent progress from stream events, and stall detection

- Status: Proposed
- Date: 2026-11-10
- Sources: [R15], [R21] · Findings: F-002, F-021
- Amends (by addition; no accepted text is edited):
  - **ADR-0017:** the invocation and the result parsing.
  - **ADR-0014:** a command's inactivity timeout and bounded output capture.
  - **ADR-0029 §1:** what "agent output" means for the stall limit.
- Decided by the maintainer on 2026-11-10: Option A, stream events with a
  900-second default.

## Context and problem

ADR-0029 §1 and #13 require stall detection: an attempt whose agent shows no
progress for `stall_timeout_seconds` is ended and retried after backoff.
ADR-0017 runs Claude Code as `claude -p … --output-format json`. That prints
one result object at the very end and nothing before it, so a stall timer on
that output would end every healthy run longer than the timeout.

Today:

- The Podman adapter keeps the first 8 MiB of each stream and drops the rest
  (`OUTPUT_LIMIT_BYTES`). A long transcript would lose its last line, which is
  the result.
- With `json`, only the final result passes through the coordinator. A
  transcript would also carry tool inputs and outputs: file contents, command
  output, anything the agent printed.

## Considered options

1. **`--output-format stream-json --verbose`.** Claude Code writes one JSON
   object per line as the run progresses, and the last line is the result
   object ([R15]). **Chosen.**
2. *CPU progress of the container* (for example `podman stats`). Rejected:
   - a busy loop counts as progress, and a wait on the network does not;
   - it needs polling a second interface;
   - it says nothing about the agent itself.
3. *Changes to workspace files.* Rejected: reading, planning and long test
   runs change no files.
4. *Stream events plus `--include-partial-messages`* (token deltas). Rejected
   as the default: many times more lines for no gain at a timeout measured in
   minutes.

## Decision

### 1. Invocation

ADR-0017's command changes in one place only: `--output-format json` becomes
`--output-format stream-json --verbose`. In print mode, Claude Code refuses
`stream-json` without `--verbose` ([R15]). Everything else stays as decided:

- the prompt, the permission mode and the MCP settings;
- `--max-turns`;
- the per-command environment, the version pin and the network policy.

### 2. Progress is a validated event

A **progress event** is one complete line on the agent command's standard
output that is:

- at most `MAX_EVENT_BYTES` (1 MiB) long;
- valid JSON;
- a JSON object whose `type` is one of the documented top-level types:
  `system`, `assistant`, `user` or `result`.

Nothing else counts:

- standard error;
- non-JSON lines;
- JSON with an unknown or missing `type`;
- over-long lines;
- partial lines.

The check is structural only. The coordinator never acts on an event's
content.

### 3. The inactivity timeout

- **Measurement.** `[limits] stall_timeout_seconds`, default **900**,
  positive, finite and at most `agent_timeout_seconds`. It is measured in the
  coordinator with the monotonic clock from the command's start, and restarted
  by each progress event. It is never persisted (ADR-0028 §5).
- **On expiry**, the runtime ends the command exactly as on a timeout:
  - the process group is killed and the whole sandbox removed (ADR-0014);
  - the result reports `stalled`.
- **The outcome** is the new `AgentOutcome.STALLED`, with reason code
  `agent-stalled`. It is transient: the attempt ends, backoff applies (#13),
  it counts as an attempt and keeps its reservation (ADR-0029).
- **Long silent tools are expected.** One tool call, such as a full test
  suite, can legitimately produce no events for its whole duration. The
  default of 900 seconds is therefore deliberately long, and operators with
  longer single steps raise it.
- **The hard limit stays separate.** Whatever the stall timer does, the
  hard lifetime bounds the attempt:
  - `agent_timeout_seconds` in the coordinator;
  - Podman's `--timeout`, enforced by conmon (ADR-0029 §1).

### 4. Bounded capture that keeps the result

The runtime port gains a capture mode for long transcripts:

- **What is kept.** For the agent command, each stream keeps its first
  `HEAD_BYTES` (256 KiB) and its last `TAIL_BYTES` (8 MiB) in a ring buffer.
  A marker states how many bytes were dropped in between. The total held
  per stream stays bounded, as before.
- **Reading.** Lines are read and checked as they arrive, so the progress
  timer never depends on what is kept.
- **The result line must be wholly inside the tail.** If it is not, or is
  longer than `MAX_EVENT_BYTES`, the outcome is `FAILED` with reason "result
  too large". It is never parsed from a fragment.
- **Other commands** (verification, git, probes) keep today's head-only
  capture.

### 5. Strict parsing of the outcome

- **Last line.** The last non-empty line of standard output must be the
  result object (`{"type": "result", …}`). Anything after it, or no such
  line, is `FAILED` ("malformed stream").
- **Unknown lines.** Any kept line that is not a valid event is `FAILED`
  ("malformed stream"). Claude Code writes only events to standard output.
- **More than one result object** among the kept lines is `FAILED`.
- **F-021 is unchanged.** `COMPLETED` still needs all of: exit status 0,
  `subtype == "success"` and `is_error == false`, on that one result object.
- **Fields.** Only the result object's documented fields are read. Its cost
  and usage figures are untrusted (ADR-0029 §2) and are recorded only as
  untrusted input for #18.

### 6. Redaction and what is kept

The transcript passes through the coordinator but is never stored:

- **Kept from a run:**
  - the parsed outcome;
  - the redacted, truncated result text (4,000 characters, as today);
  - counts: events, bytes and dropped bytes.
- **Never written:** the raw stream is not written to the ledger, events,
  logs or evidence, and its buffers are discarded after parsing.
- **Redaction:** the model key is removed from every derived string, as today
  (ADR-0017), and so are the values of all other variables in the agent
  command's environment.
- **Error texts:** exceptions and summaries quote no line from the stream,
  only the line number and the reason it was rejected.

### 7. Crashes

If the coordinator dies during a run:

- the stream and the timer are lost with it;
- the container stops by its runtime-side lifetime (ADR-0029 §1);
- recovery treats the attempt as interrupted, as today (ADR-0021, ADR-0022);
- nothing from a partial stream is persisted, so nothing needs replaying.

### 8. Port changes

These are shared by every adapter and tested in the contract suites:

- `ContainerRuntime.run` gains `idle_timeout_seconds` and a `progress` line
  predicate. It also gains the head-and-tail capture mode.
- `CommandResult` gains `stalled`. At most one of `timed_out`, `cancelled`
  and `stalled` is set.
- `AgentRequest` gains `stall_timeout_seconds`, and `AgentOutcome` gains
  `STALLED`.

## Consequences

- Good:
  - a hung agent is ended in minutes rather than at the hard timeout;
  - a long transcript no longer risks losing its result;
  - the outcome rules become stricter.
- Bad:
  - the coordinator handles the full transcript in memory, bounded but larger
    than before, and must never persist it;
  - a single tool step longer than the stall timeout is ended and retried, so
    operators must size the timeout to their slowest step;
  - the format of the documented `stream-json` events becomes a dependency.
    The live test and the version pin guard it.

## Tests required

- **Silence:**
  - no events for the timeout ends the run as `STALLED`, removes the sandbox,
    and the retry waits for its backoff;
  - steady events never stall a run;
  - a silent stretch just under the timeout does not stall it;
  - standard-error output and invalid or unknown lines never reset the timer.
- **Truncation:** a stream far beyond the limits keeps its result and
  completes. A result line outside the tail, or over `MAX_EVENT_BYTES`, fails
  with "result too large". The dropped-bytes marker is correct.
- **Malformed streams:** non-JSON lines, unknown types, trailing data after
  the result, two result objects, a missing result and a partial last line
  each give `FAILED`. F-021's mappings hold on stream output.
- **Redaction:** a stream whose events contain the model key and the other
  environment values. None of them appears in the outcome, the summary, an
  exception, a log line, an event or the store.
- **Crashes:**
  - the coordinator killed during a run: the container stops by its runtime
    lifetime (real Podman, integration), and recovery starts a new attempt
    after backoff;
  - a crash while parsing changes nothing durable.
- **Contracts:** the `ContainerRuntime` contract covers idle timeout,
  progress lines and capture, for the fake and Podman. The `AgentRunner`
  contract covers `STALLED`.
- **Live:** the opt-in live test runs real Claude Code with `stream-json`. It
  checks that the documented event types arrive and that the last line is the
  result object.

## Revisit when

- Claude Code documents a heartbeat or progress event for long tool calls.
  That could allow a shorter default.
- Its stream format changes in a way the live test reports.
- A second agent runner is added. Its progress signal then needs its own
  definition under this port.
