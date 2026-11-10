# SPDX-License-Identifier: Apache-2.0
"""Stall detection and stream validation through the coordinator (ADR-0035).

* A stalled attempt is transient: it ends, waits for its backoff (#13) and is
  retried, with reason code ``agent-stalled``.
* A rejected stream fails the run without a retry.
* No line of the stream reaches the ledger, the events or the logs, for
  every kind of violation.
* A crash in the middle of a stream changes nothing durable beyond what
  recovery already handles: the attempt is abandoned and retried.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from datetime import timedelta
from pathlib import Path

import pytest

from coordinator_support import Setup, make_setup
from ilmar.adapters.claude_code.runner import ClaudeCodeRunner
from ilmar.adapters.sqlite import SQLiteExecutionStore
from ilmar.core.state_machine import RetryBackoff
from ilmar.core.worker import Outcome
from ilmar.domain.agent import AgentOutcome
from ilmar.domain.execution import ExecutionState
from ilmar.domain.workspace import CommandResult

from claude_code_support import ASSISTANT, INIT, SUCCESS_RESULT, stream

S = ExecutionState
BACKOFF = RetryBackoff(300.0, 1200.0)
FIXED = {"src/app.py": "print('fixed')\n"}
MARKER = "PRIVATE-STREAM-CONTENT-7f3a"
KEY = "sk-ant-coordinator-test-0123456789"


def test_a_stalled_attempt_waits_for_its_backoff_and_is_retried(tmp_path: Path) -> None:
    setup = make_setup(tmp_path)
    setup.agent.plan = [(AgentOutcome.STALLED, {}), (AgentOutcome.COMPLETED, FIXED)]
    setup.coordinator(retry_backoff=BACKOFF).run_once()
    execution = setup.store.get("e1")
    assert execution is not None
    assert (execution.state, execution.attempt) == (S.RETRY_WAIT, 1)
    assert execution.last_error == "the agent showed no progress"
    assert execution.retry_not_before == execution.updated_at + timedelta(seconds=300)
    events = setup.store.events()
    assert any(event.reason == "agent-stalled" for event in events)

    setup.clock.now = execution.retry_not_before
    report = setup.coordinator(retry_backoff=BACKOFF).run_once()
    assert [r.outcome for r in report.results] == [Outcome.PUBLISHED]
    done = setup.store.get("e1")
    assert done is not None
    assert (done.state, done.attempt) == (S.SUCCEEDED, 2)


def with_claude(setup: Setup, stdout: str) -> None:
    """Run the real Claude Code runner against a scripted ``claude`` program."""

    def claude(argv: Sequence[str], timeout: float, env: Mapping[str, str]) -> CommandResult:
        return CommandResult(0, stdout, "", 1.0)

    setup.runtime.register("claude", claude)
    setup.components.agent_runner = ClaudeCodeRunner(api_key=KEY)


BAD_STREAMS = {
    "malformed-event": stream(SUCCESS_RESULT, before=(INIT, f"not json {MARKER}")),
    "unknown-type": stream(SUCCESS_RESULT, before=(INIT, f'{{"type": "{MARKER}"}}')),
    "duplicate-result": stream(SUCCESS_RESULT, after=(SUCCESS_RESULT.replace("Fixed", MARKER),)),
    "data-after-result": stream(SUCCESS_RESULT, after=(ASSISTANT.replace("Looking.", MARKER),)),
    "unterminated-line": stream(SUCCESS_RESULT) + f'{{"type": "assistant", "m": "{MARKER}"}}',
    "missing-result": f'{{"type": "assistant", "m": "{MARKER}"}}\n',
    "oversized-line": f'{{"type": "assistant", "m": "{MARKER}{"x" * (2 * 1024 * 1024)}"}}\n',
}


@pytest.mark.parametrize("kind", sorted(BAD_STREAMS))
def test_a_rejected_stream_fails_without_retry_and_nothing_of_it_is_persisted(
    tmp_path: Path, kind: str, caplog: pytest.LogCaptureFixture
) -> None:
    db = tmp_path / "executions.sqlite3"
    setup = make_setup(tmp_path, store=SQLiteExecutionStore(db))
    with_claude(setup, BAD_STREAMS[kind])
    with caplog.at_level(logging.DEBUG):
        report = setup.coordinator(retry_backoff=BACKOFF).run_once()
    execution = setup.store.get("e1")
    assert execution is not None
    assert (execution.state, execution.attempt) == (S.FAILED, 1)  # never retried
    assert execution.last_error is not None
    assert "malformed stream" in execution.last_error
    assert MARKER not in execution.last_error
    assert all(MARKER not in r.detail for r in report.results)
    assert MARKER not in caplog.text
    assert KEY not in caplog.text
    setup.store.close()  # type: ignore[attr-defined]
    stored = b"".join(path.read_bytes() for path in tmp_path.rglob("executions.sqlite3*"))
    assert MARKER.encode() not in stored
    assert KEY.encode() not in stored


def test_a_valid_stream_completes_and_keeps_no_stream_content(tmp_path: Path) -> None:
    db = tmp_path / "executions.sqlite3"
    setup = make_setup(tmp_path, store=SQLiteExecutionStore(db))
    with_claude(setup, stream(SUCCESS_RESULT, before=(INIT, ASSISTANT.replace("Looking.", MARKER))))
    setup.coordinator(retry_backoff=BACKOFF).run_once()
    execution = setup.store.get("e1")
    assert execution is not None
    # The scripted agent changed nothing, so verification fails; the point is
    # that the run completed and no event line was kept anywhere.
    assert execution.state in {S.FAILED, S.RETRY_WAIT}
    setup.store.close()  # type: ignore[attr-defined]
    stored = b"".join(path.read_bytes() for path in tmp_path.rglob("executions.sqlite3*"))
    assert MARKER.encode() not in stored


def test_a_crash_in_the_middle_of_a_stream_is_recovered_as_an_interrupted_attempt(
    tmp_path: Path,
) -> None:
    """The coordinator dies while the agent streams: the stream is lost with it,
    nothing of it was stored, and recovery abandons the attempt and retries it
    after backoff."""
    setup = make_setup(tmp_path)

    class _CoordinatorDied(BaseException):
        pass

    def claude(argv: Sequence[str], timeout: float, env: Mapping[str, str]) -> CommandResult:
        raise _CoordinatorDied  # as if the process ended during the run

    setup.runtime.register("claude", claude)
    setup.components.agent_runner = ClaudeCodeRunner(api_key=KEY)
    with pytest.raises(_CoordinatorDied):
        setup.coordinator(retry_backoff=BACKOFF).run_once()
    running = setup.store.get("e1")
    assert running is not None
    assert running.state is S.RUNNING_AGENT

    # A new coordinator process: recovery, then the retry waits for its backoff.
    setup.agent.plan = [(AgentOutcome.COMPLETED, FIXED)]
    setup.components.agent_runner = setup.agent
    setup.coordinator(retry_backoff=BACKOFF).run_once()
    recovered = setup.store.get("e1")
    assert recovered is not None
    assert recovered.state is S.RETRY_WAIT
    assert recovered.last_error == "interrupted while the agent ran"
    assert recovered.retry_not_before is not None
    setup.clock.now = recovered.retry_not_before
    setup.coordinator(retry_backoff=BACKOFF).run_once()
    done = setup.store.get("e1")
    assert done is not None
    assert (done.state, done.attempt) == (S.SUCCEEDED, 2)
