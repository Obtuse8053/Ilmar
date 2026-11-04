# SPDX-License-Identifier: Apache-2.0
"""ClaudeCodeRunner details beyond the AgentRunner contract (#8, ADR-0017)."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path

import pytest

from ilmar.adapters.claude_code import ClaudeCodeRunner, ClaudeCodeSettings
from ilmar.adapters.claude_code.runner import REDACTED, parse_result
from ilmar.domain.agent import AgentOutcome, AgentRequest
from ilmar.domain.workspace import CommandResult, ContainerHandle, ContainerSpec, ResourceOwner

from claude_code_support import ERROR, SUCCESS, result_json
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
        "--output-format", "json", "--permission-mode", "bypassPermissions",
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


def test_the_credential_is_redacted_from_summaries(tmp_path: Path) -> None:
    leaked = result_json(subtype="success", is_error=False, text=f"my key is {KEY}")
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
        ("progress line\n" + SUCCESS, 0, AgentOutcome.COMPLETED),
        (
            '{\n  "type": "result", "subtype": "success",\n  "is_error": false, "result": "ok"\n}',
            0,
            AgentOutcome.COMPLETED,
        ),
        (
            result_json(subtype="error_max_turns", is_error=True, text="ran out"),
            0,
            AgentOutcome.FAILED,
        ),
        (result_json(subtype="success", is_error=True, text="odd"), 0, AgentOutcome.FAILED),
        (result_json(subtype="other", is_error=False, text="?"), 0, AgentOutcome.FAILED),
        (
            '{"type": "result", "subtype": "success", "result": "no is_error"}',
            0,
            AgentOutcome.FAILED,
        ),
        ('{"type": "assistant", "is_error": false, "subtype": "success"}', 0, AgentOutcome.FAILED),
        ("[1, 2]", 0, AgentOutcome.FAILED),
        (ERROR, 1, AgentOutcome.FAILED),
    ],
)
def test_outcome_mapping(tmp_path: Path, stdout: str, code: int, outcome: AgentOutcome) -> None:
    runtime, handle, _ = setup(tmp_path, ok(stdout, code))
    result = ClaudeCodeRunner(api_key=KEY).run(runtime, handle, request())
    assert result.outcome is outcome
    assert result.exit_code == code


def test_failure_summaries_say_why(tmp_path: Path) -> None:
    max_turns = result_json(subtype="error_max_turns", is_error=True, text="ran out of turns")
    runtime, handle, _ = setup(
        tmp_path, ok(max_turns), ok(SUCCESS, 3), ok("plain text", 0, "Error: auth failed\n")
    )
    runner = ClaudeCodeRunner(api_key=KEY)
    assert runner.run(runtime, handle, request()).summary.startswith("error_max_turns: ran out")
    assert (
        "exit status 3 despite a success result" in runner.run(runtime, handle, request()).summary
    )
    summary = runner.run(runtime, handle, request()).summary
    assert summary == "no structured result (exit 0): Error: auth failed"


def test_long_summaries_are_truncated(tmp_path: Path) -> None:
    long = result_json(subtype="success", is_error=False, text="x" * 10000)
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


def test_parse_result_ignores_non_result_objects() -> None:
    assert parse_result('{"type": "system"}\n{"type": "assistant"}') is None
    parsed = parse_result('noise\n{"type": "result", "subtype": "success"}\n')
    assert parsed is not None
    assert parsed["subtype"] == "success"
