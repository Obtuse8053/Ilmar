# ADR-0035: Agent progress from stream events, and stall detection

- Status: Proposed
- Date: 2026-11-10
- Sources: [R15], [R21] · Findings: F-002, F-021
- Amends (by addition; no accepted text is edited):
  - **ADR-0017:** the invocation and the result parsing.
  - **ADR-0014:** a command's inactivity timeout and bounded output capture.
  - **ADR-0029 §1:** what "agent output" means for the stall limit.
- Decided by the maintainer on 2026-11-10: Option A, stream events with a
  900-second default. Amended in review the same day: every event is
  validated incrementally, including any a capture would drop.

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

### 4. Every event is validated as it arrives

Standard output of the agent command is not captured as text. It is fed,
chunk by chunk as the runtime reads it, to a **stream validator** owned by
the runner. The validator splits lines itself and checks **every** line,
whatever its position in the stream. No line escapes validation because it
falls in a region a capture would drop.

**Bounded state.** The validator holds only:

- the current incomplete line, at most `MAX_EVENT_BYTES` (1 MiB). Once a
  line passes that size, its further bytes are counted and discarded, never
  buffered, and the line is a violation;
- the result object's line, once seen (at most `MAX_EVENT_BYTES`);
- counters: lines, events and bytes;
- the first violation, as a kind and a line number. It never holds the
  violating line's content.

Memory per agent command is therefore at most about 2 × `MAX_EVENT_BYTES`,
however long the stream.

**Violations.** Each is checked on every complete line, and at the end of
the stream:

| Violation | When |
| --- | --- |
| `malformed-event` | A line that is not valid JSON, is not an object, or has a missing or undocumented `type`. Empty lines count. |
| `oversized-line` | A line longer than `MAX_EVENT_BYTES`, detected while it is being read |
| `duplicate-result` | A second `result` object |
| `data-after-result` | Any further line after the `result` object |
| `unterminated-line` | Bytes after the last newline at the end of the stream |
| `missing-result` | The stream ended without a `result` object |

**The first violation ends the run.** As soon as a violation is found, the
validator reports it. The runtime ends the command as it does on a timeout:
the process group is killed and the sandbox removed (ADR-0014). The outcome
is `FAILED` with the reason "malformed stream: <kind> at line <n>". This
holds even if a valid result would have followed. A malformed section in the
middle of the stream fails the run even when the final result is valid. The
failure is not retried: an agent that breaks its documented output format
needs a human (for example a version mismatch).

**Progress.** A complete line that passes validation is a progress event
(§2) and restarts the inactivity timer. A violation never counts as
progress.

**Standard error** is not validated. It keeps a bounded capture of its first
256 KiB and last 64 KiB, used only for the redacted one-line diagnostic of a
failed run, as today.

### 5. The outcome comes from the validator

- **Success** needs a stream that ended with no violation and exactly one
  result object, which was the last line. F-021 is unchanged: `COMPLETED`
  also needs exit status 0, `subtype == "success"` and `is_error == false`.
- **Fields.** Only the result object's documented fields are read. Its cost
  and usage figures are untrusted (ADR-0029 §2) and are recorded only as
  untrusted input for #18.
- **Ordering.** A violation, a stall, a timeout and a stop each end the run
  before any result is used. The first of them to occur decides the outcome.

### 6. Redaction and what is kept

The transcript passes through the coordinator but is never stored:

- **Kept from a run:**
  - the parsed outcome;
  - the redacted, truncated result text (4,000 characters, as today);
  - counts: events, bytes and dropped bytes.
- **Never written:** the raw stream is not written to the ledger, events,
  logs or evidence. Apart from the bounded validator state of §4, no
  standard-output bytes are kept at all, and that state is discarded when the
  command ends.
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

- **`ContainerRuntime.run`** gains `idle_timeout_seconds` and an optional
  `stdout_consumer`. The consumer receives each chunk of standard output as
  it is read (`feed`) and the end of the stream (`close`). Each call returns
  one of three verdicts: progress, nothing new, or a violation. When a
  consumer is given:
  - the runtime keeps no standard-output text;
  - a progress verdict restarts the idle timer;
  - a violation ends the command as a timeout does.
- **`CommandResult`** gains `stalled` and `rejected`. At most one of
  `timed_out`, `cancelled`, `stalled` and `rejected` is set.
- **`AgentRequest`** gains `stall_timeout_seconds`, and **`AgentOutcome`**
  gains `STALLED`.
- **The validator** is a pure, standard-library class in the Claude Code
  adapter. It is fed bytes and returns verdicts, so it is tested without a
  runtime.

## Consequences

- Good:
  - a hung agent is ended in minutes rather than at the hard timeout;
  - a long transcript no longer risks losing its result, and no line goes
    unchecked;
  - the outcome rules become stricter, and a malformed stream stops spending
    at once.
- Bad:
  - every byte of the transcript passes through the coordinator, which must
    stay bounded (about 2 MiB per command) and must never persist it;
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
  - standard-error output never resets the timer, and an invalid line ends
    the run instead of counting as progress.
- **Validation everywhere in the stream:**
  - a malformed event, an undocumented `type` or an empty line in the middle
    of a long stream, followed by a valid final result: the run fails with
    `malformed-event` at the right line number;
  - an oversized line in the middle (for example 50 MiB without a newline)
    followed by a valid result: `oversized-line`, while the validator never
    holds more than `MAX_EVENT_BYTES` of it;
  - a second result object: `duplicate-result`;
  - any line after the result: `data-after-result`;
  - a final line without a newline: `unterminated-line`;
  - no result at all: `missing-result`;
  - the first violation stops the command at once (no further spending), and
    a later valid result does not change the outcome.
- **Chunking:** any split of the same stream into chunks, down to one byte,
  across UTF-8 sequences and JSON tokens, gives the same verdicts and line
  numbers.
- **Bounded memory:** the validator's held bytes stay within its bound for
  streams of any length. The runtime keeps no standard-output text when a
  consumer is given.
- **Results:** a long valid stream completes with its result. F-021's
  mappings hold on stream output.
- **Nothing raw persisted:** after runs with each violation kind, no stream
  line appears in the ledger, events, logs, evidence or exception texts. Only
  the kind and the line number do.
- **Redaction:** a stream whose events contain the model key and the other
  environment values. None of them appears in the outcome, the summary, an
  exception, a log line, an event or the store.
- **Crashes:**
  - the coordinator killed during a run: the container stops by its runtime
    lifetime (real Podman, integration), and recovery starts a new attempt
    after backoff;
  - a crash while parsing changes nothing durable.
- **Contracts:** the `ContainerRuntime` contract covers the idle timeout,
  the standard-output consumer (progress, violation, end of stream) and the
  absence of retained standard output, for the fake and for Podman. The `AgentRunner`
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
