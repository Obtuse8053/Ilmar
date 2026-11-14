# SPDX-License-Identifier: Apache-2.0
"""ClaudeCodeRunner details beyond the AgentRunner contract (#8, ADR-0017)."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import pytest

from ilmar.adapters.claude_code import ClaudeCodeRunner, ClaudeCodeSettings
from ilmar.adapters.claude_code.runner import REDACTED
from ilmar.adapters.claude_code.stream import StreamValidator
from ilmar.domain.agent import AgentOutcome, AgentRequest
from ilmar.domain.errors import PolicyViolationError
from ilmar.domain.workspace import (
    CommandResult,
    ContainerHandle,
    ContainerSpec,
    NetworkPolicy,
    ResourceOwner,
)

from claude_code_support import (
    API_RETRY,
    ASSISTANT,
    ERROR,
    RATE_LIMIT,
    SUCCESS,
    SUCCESS_RESULT,
    allow_probe,
    result_json,
    stream,
)
from contracts.container_runtime import identity_of
from fakes import FakeContainerRuntime
from helpers import task

KEY = "sk-ant-test-key-0123456789"
OWNER = ResourceOwner(instance_id="t", execution_id="e", attempt=1, token="t")


class Scripted:
    """A ``claude`` stand-in that records every call and returns queued results."""

    def __init__(self, *results: CommandResult) -> None:
        self.results = list(results)
        self.calls: list[tuple[list[str], dict[str, str]]] = []

    def __call__(
        self, argv: Sequence[str], timeout: float, env: Mapping[str, str]
    ) -> CommandResult:
        self.calls.append((list(argv), dict(env)))
        return self.results.pop(0)


def setup(
    tmp_path: Path, *results: CommandResult
) -> tuple[FakeContainerRuntime, ContainerHandle, Scripted]:
    runtime = FakeContainerRuntime()
    script = Scripted(*results)
    runtime.register("claude", script)
    allow_probe(runtime)
    spec = ContainerSpec(
        image="img",
        host_workspace=tmp_path,
        name="agent",
        owner=OWNER,
        workspace_identity=identity_of(tmp_path),
    )
    return runtime, runtime.start(spec), script


def request(instructions: str = "Fix the bug.", title: str = "Bug") -> AgentRequest:
    return AgentRequest(
        task=task("42", title=title), instructions=instructions, timeout_seconds=600
    )


def ok(stdout: str = SUCCESS, code: int = 0, stderr: str = "") -> CommandResult:
    return CommandResult(code, stdout, stderr, 1.0)


def test_documented_non_interactive_invocation(tmp_path: Path) -> None:
    runtime, handle, script = setup(tmp_path, ok())
    runner = ClaudeCodeRunner(
        api_key=KEY, settings=ClaudeCodeSettings(model="sonnet", max_turns=50)
    )
    runner.run(runtime, handle, request())
    argv, _ = script.calls[0]
    assert argv[:2] == ["claude", "-p"]
    assert argv[3:] == [
        "--output-format", "stream-json", "--verbose", "--permission-mode", "bypassPermissions",
        "--no-session-persistence", "--strict-mcp-config",
        "--max-turns", "50", "--model", "sonnet",
    ]  # fmt: skip


@pytest.mark.parametrize("instructions", ["--dangerously-do-something", "-p evil", "\n--x"])
def test_the_prompt_can_never_be_read_as_an_option(tmp_path: Path, instructions: str) -> None:
    runtime, handle, script = setup(tmp_path, ok())
    ClaudeCodeRunner(api_key=KEY).run(runtime, handle, request(instructions, title="-t"))
    prompt = script.calls[0][0][2]
    assert prompt.startswith("Ilmar task ")
    assert instructions in prompt


def test_the_credential_travels_only_in_the_commands_environment(tmp_path: Path) -> None:
    runtime, handle, script = setup(tmp_path, ok())
    ClaudeCodeRunner(api_key=KEY).run(runtime, handle, request())
    argv, env = script.calls[0]
    assert env["ANTHROPIC_API_KEY"] == KEY
    assert not any(KEY in arg for arg in argv)
    assert env["CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"] == "1"
    assert env["DISABLE_AUTOUPDATER"] == "1"
    assert "ANTHROPIC_BASE_URL" not in env
    # Never in the container's own environment, so other commands do not see it.
    container = runtime._containers[handle.id]
    assert KEY not in container.spec.env.values()


def test_gateway_url_is_passed_when_configured(tmp_path: Path) -> None:
    runtime, handle, script = setup(tmp_path, ok())
    settings = ClaudeCodeSettings(base_url="https://llm-gateway.example.com")
    ClaudeCodeRunner(api_key=KEY, settings=settings).run(runtime, handle, request())
    assert script.calls[0][1]["ANTHROPIC_BASE_URL"] == "https://llm-gateway.example.com"


def gateway_setup(
    tmp_path: Path, *results: CommandResult
) -> tuple[FakeContainerRuntime, ContainerHandle, Scripted]:
    runtime = FakeContainerRuntime()
    script = Scripted(*results)
    runtime.register("claude", script)
    allow_probe(runtime)
    spec = ContainerSpec(image="img", host_workspace=tmp_path, name="agent", owner=OWNER,
                         workspace_identity=identity_of(tmp_path),
                         network=NetworkPolicy.GATEWAY, model_budget_micros=1_000_000)  # fmt: skip
    return runtime, runtime.start(spec), script


def test_a_gateway_sandbox_gets_the_token_and_never_the_real_key(tmp_path: Path) -> None:
    """ADR-0037 §1: the runtime's route replaces the key and sets the base URL."""
    runtime, handle, script = gateway_setup(tmp_path, ok())
    result = ClaudeCodeRunner(api_key=KEY).run(runtime, handle, request())
    assert result.outcome is AgentOutcome.COMPLETED
    route = runtime.model_access(handle)
    assert route is not None
    for _argv, env in script.calls:  # the probe and the run alike
        assert KEY not in env.values()
    _, env = script.calls[-1]
    assert env["ANTHROPIC_API_KEY"] == route.token
    assert env["ANTHROPIC_BASE_URL"] == route.base_url == "http://10.89.0.2:8080"


def test_a_gateway_sandbox_refuses_a_second_base_url(tmp_path: Path) -> None:
    runtime, handle, script = gateway_setup(tmp_path, ok())
    runner = ClaudeCodeRunner(
        api_key=KEY, settings=ClaudeCodeSettings(base_url="https://other.example.com")
    )
    with pytest.raises(PolicyViolationError, match="ADR-0037"):
        runner.run(runtime, handle, request())
    assert script.calls == []


def test_the_gateway_token_is_redacted_from_summaries(tmp_path: Path) -> None:
    runtime, handle, _ = gateway_setup(tmp_path)
    route = runtime.model_access(handle)
    assert route is not None
    leaked = stream(result_json(subtype="success", is_error=False, text=f"tok {route.token}"))
    runtime.register("claude", Scripted(ok(leaked)))
    result = ClaudeCodeRunner(api_key=KEY).run(runtime, handle, request())
    assert route.token not in result.summary
    assert REDACTED in result.summary


def test_the_credential_is_redacted_from_summaries(tmp_path: Path) -> None:
    leaked = stream(result_json(subtype="success", is_error=False, text=f"my key is {KEY}"))
    runtime, handle, _ = setup(tmp_path, ok(leaked), ok("", 1, f"Error: bad key {KEY}"))
    runner = ClaudeCodeRunner(api_key=KEY)
    first = runner.run(runtime, handle, request())
    second = runner.run(runtime, handle, request())
    for result in (first, second):
        assert KEY not in result.summary
    assert REDACTED in first.summary
    assert REDACTED in second.summary


@pytest.mark.parametrize(
    ("stdout", "code", "outcome"),
    [
        (SUCCESS, 0, AgentOutcome.COMPLETED),
        (stream(SUCCESS_RESULT, before=()), 0, AgentOutcome.COMPLETED),
        (stream(SUCCESS_RESULT, after=(RATE_LIMIT,)), 0, AgentOutcome.COMPLETED),
        (stream(SUCCESS_RESULT, before=(RATE_LIMIT, ASSISTANT, API_RETRY)), 0,
         AgentOutcome.COMPLETED),
        # Anything that is not a valid event fails the run, wherever it is.
        ("progress line\n" + SUCCESS, 0, AgentOutcome.FAILED),
        ('{\n  "type": "result", "subtype": "success",\n  "is_error": false, "result": "ok"\n}\n',
         0, AgentOutcome.FAILED),
        (SUCCESS.rstrip("\n"), 0, AgentOutcome.FAILED),  # unterminated last line
        (stream(SUCCESS_RESULT, after=(SUCCESS_RESULT,)), 0, AgentOutcome.FAILED),
        (stream(SUCCESS_RESULT, after=(ASSISTANT,)), 0, AgentOutcome.FAILED),
        (stream(SUCCESS_RESULT, after=(API_RETRY,)), 0, AgentOutcome.FAILED),
        (stream(result_json(subtype="error_max_turns", is_error=True, text="ran out")), 0,
         AgentOutcome.FAILED),
        (stream(result_json(subtype="success", is_error=True, text="odd")), 0,
         AgentOutcome.FAILED),
        (stream(result_json(subtype="other", is_error=False, text="?")), 0, AgentOutcome.FAILED),
        (stream('{"type": "result", "subtype": "success", "result": "no is_error"}'), 0,
         AgentOutcome.FAILED),
        (stream(ASSISTANT, before=()), 0, AgentOutcome.FAILED),  # no result
        ("[1, 2]\n", 0, AgentOutcome.FAILED),
        (ERROR, 1, AgentOutcome.FAILED),
    ],
)  # fmt: skip
def test_outcome_mapping(tmp_path: Path, stdout: str, code: int, outcome: AgentOutcome) -> None:
    runtime, handle, _ = setup(tmp_path, ok(stdout, code))
    result = ClaudeCodeRunner(api_key=KEY).run(runtime, handle, request())
    assert result.outcome is outcome
    rejected = result.summary.startswith("malformed stream")
    # A rejected stream ends the command (killed: -9), as a timeout does.
    assert result.exit_code == (-9 if rejected else code)


def test_failure_summaries_say_why(tmp_path: Path) -> None:
    max_turns = stream(
        result_json(subtype="error_max_turns", is_error=True, text="ran out of turns")
    )
    runtime, handle, _ = setup(
        tmp_path, ok(max_turns), ok(SUCCESS, 3), ok("plain text", 0, "Error: auth failed\n")
    )
    runner = ClaudeCodeRunner(api_key=KEY)
    assert runner.run(runtime, handle, request()).summary.startswith("error_max_turns: ran out")
    assert (
        "exit status 3 despite a success result" in runner.run(runtime, handle, request()).summary
    )
    summary = runner.run(runtime, handle, request()).summary
    assert summary == "malformed stream: unterminated-line at line 1 (Error: auth failed)"
    assert "plain text" not in summary  # never a line of the stream


def test_long_summaries_are_truncated(tmp_path: Path) -> None:
    long = stream(result_json(subtype="success", is_error=False, text="x" * 10000))
    runtime, handle, _ = setup(tmp_path, ok(long))
    assert len(ClaudeCodeRunner(api_key=KEY).run(runtime, handle, request()).summary) == 4000


# -- version pinning (ADR-0011 §6) ---------------------------------------------------------


def test_expected_version_is_checked_before_running(tmp_path: Path) -> None:
    runtime, handle, script = setup(tmp_path, ok("2.1.44 (Claude Code)\n"), ok())
    settings = ClaudeCodeSettings(expected_version="2.1.44")
    result = ClaudeCodeRunner(api_key=KEY, settings=settings).run(runtime, handle, request())
    assert result.outcome is AgentOutcome.COMPLETED
    version_argv, version_env = script.calls[0]
    assert version_argv == ["claude", "--version"]
    assert "ANTHROPIC_API_KEY" not in version_env


@pytest.mark.parametrize("reported", ["2.1.45 (Claude Code)\n", "", "2.1.4\n"])
def test_a_different_version_never_runs(tmp_path: Path, reported: str) -> None:
    runtime, handle, script = setup(tmp_path, ok(reported))
    settings = ClaudeCodeSettings(expected_version="2.1.44")
    result = ClaudeCodeRunner(api_key=KEY, settings=settings).run(runtime, handle, request())
    assert result.outcome is AgentOutcome.FAILED
    assert "expected 2.1.44" in result.summary
    assert len(script.calls) == 1  # the agent itself never started


# -- configuration --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"permission_mode": "default"}, "permission_mode"),
        ({"permission_mode": "plan"}, "permission_mode"),
        ({"model": "--evil"}, "model"),
        ({"max_turns": 0}, "max_turns"),
        ({"expected_version": "latest"}, "1.2.3"),
        ({"base_url": "http://plain.example.com"}, "https"),
        ({"executable": "-x"}, "executable"),
    ],
)
def test_settings_are_validated(kwargs: dict[str, object], message: str) -> None:
    with pytest.raises(ValueError, match=message):
        ClaudeCodeSettings(**kwargs)  # type: ignore[arg-type]


@pytest.mark.parametrize("key", ["", "two\nlines"])
def test_a_usable_credential_is_required(key: str) -> None:
    with pytest.raises(ValueError, match="credential"):
        ClaudeCodeRunner(api_key=key)


# -- stream-json (ADR-0035) ----------------------------------------------------------------


class Spy(FakeContainerRuntime):
    """Records what the runner asks of the runtime for each command."""

    def __init__(self) -> None:
        super().__init__()
        self.asked: list[dict[str, object]] = []

    def run(self, handle: ContainerHandle, argv: Sequence[str], **kwargs: Any) -> CommandResult:
        self.asked.append(dict(kwargs))
        return super().run(handle, argv, **kwargs)


def spy_setup(tmp_path: Path, *results: CommandResult) -> tuple[Spy, ContainerHandle]:
    runtime = Spy()
    runtime.register("claude", Scripted(*results))
    allow_probe(runtime)
    spec = ContainerSpec(
        image="img", host_workspace=tmp_path, name="agent", owner=OWNER,
        workspace_identity=identity_of(tmp_path),
    )  # fmt: skip
    return runtime, runtime.start(spec)


def test_the_stall_timeout_and_a_validator_go_to_the_runtime(tmp_path: Path) -> None:
    runtime, handle = spy_setup(tmp_path, ok())
    stalling = AgentRequest(
        task=task("42"), instructions="x", timeout_seconds=600, stall_timeout_seconds=90
    )
    result = ClaudeCodeRunner(api_key=KEY).run(runtime, handle, stalling)
    assert result.outcome is AgentOutcome.COMPLETED
    probe, asked = runtime.asked  # the credential-path probe, then Claude Code
    assert probe.get("stdout_consumer") is None
    assert asked["idle_timeout_seconds"] == 90
    assert isinstance(asked["stdout_consumer"], StreamValidator)


def test_a_stall_is_reported_as_stalled(tmp_path: Path) -> None:
    runtime, handle, _ = setup(tmp_path, CommandResult(-9, "", "", 90.0, stalled=True))
    stalling = AgentRequest(
        task=task("42"), instructions="x", timeout_seconds=600, stall_timeout_seconds=90
    )
    result = ClaudeCodeRunner(api_key=KEY).run(runtime, handle, stalling)
    assert result.outcome is AgentOutcome.STALLED
    assert result.summary == "Claude Code showed no progress for 90 seconds"


def test_every_environment_value_is_redacted(tmp_path: Path) -> None:
    gateway = "https://llm-gateway.example.com/v1"
    leaked = stream(
        result_json(subtype="error", is_error=True, text=f"key {KEY} via {gateway} and 1")
    )
    runtime, handle, _ = setup(tmp_path, ok(leaked), ok("", 1, f"Error: {gateway} refused"))
    runner = ClaudeCodeRunner(api_key=KEY, settings=ClaudeCodeSettings(base_url=gateway))
    first = runner.run(runtime, handle, request())
    second = runner.run(runtime, handle, request())
    for result in (first, second):
        assert KEY not in result.summary
        assert gateway not in result.summary
    assert first.summary == f"error: key {REDACTED} via {REDACTED} and 1"  # short flags stay
    assert (
        second.summary == f"malformed stream: missing-result at line 0 (Error: {REDACTED} refused)"
    )


def test_a_rejected_stream_never_quotes_its_lines(tmp_path: Path) -> None:
    secret_line = f'{{"type": "unknown", "leak": "{KEY}", "data": "private"}}'
    runtime, handle, _ = setup(tmp_path, ok(stream(SUCCESS_RESULT, before=(secret_line,))))
    result = ClaudeCodeRunner(api_key=KEY).run(runtime, handle, request())
    assert result.outcome is AgentOutcome.FAILED
    assert result.summary == "malformed stream: malformed-event at line 1"
    assert "private" not in result.summary
