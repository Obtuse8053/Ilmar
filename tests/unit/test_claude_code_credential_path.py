# SPDX-License-Identifier: Apache-2.0
"""One model-credential path, checked before Claude Code starts (ADR-0029 §5, #90).

The probe runs in the sandbox with exactly the environment Claude Code gets.
Every other route refuses the run before ``claude`` is invoked, naming what
was found but never a value. A probe that cannot run never lets Claude Code
start. The last tests run the real probe script in a real shell.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from collections.abc import Mapping, Sequence
from pathlib import Path

import pytest

from ilmar.adapters.claude_code import ClaudeCodeRunner, ClaudeCodeSettings
from ilmar.adapters.claude_code.credential_route import (
    PROBE_SCRIPT,
    forbidden_variables,
    parse_probe,
    refusal,
)
from ilmar.domain.agent import AgentRequest
from ilmar.domain.errors import PolicyViolationError, ProviderError
from ilmar.domain.workspace import CommandResult, ContainerSpec, ResourceOwner

from claude_code_support import SUCCESS, ProbeStandIn, allow_probe
from contracts.container_runtime import identity_of
from fakes import FakeContainerRuntime
from helpers import task

KEY = "sk-ant-test-key-0123456789"
OWNER = ResourceOwner(instance_id="t", execution_id="e", attempt=1, token="t")
GATEWAY = "https://llm-gateway.example.com/v1"
REQUEST = AgentRequest(task=task("1"), instructions="x", timeout_seconds=60)


class Claude:
    def __init__(self) -> None:
        self.calls = 0
        self.env: dict[str, str] = {}

    def __call__(
        self, argv: Sequence[str], timeout: float, env: Mapping[str, str]
    ) -> CommandResult:
        self.calls += 1
        self.env = dict(env)
        return CommandResult(0, SUCCESS, "", 1.0)


def run(tmp_path: Path, probe: ProbeStandIn, *, base_url: str | None = None) -> Claude:
    runtime = FakeContainerRuntime()
    claude = Claude()
    runtime.register("claude", claude)
    allow_probe(runtime, probe)
    handle = runtime.start(
        ContainerSpec(image="img", host_workspace=tmp_path, name="agent", owner=OWNER,
                      workspace_identity=identity_of(tmp_path))
    )  # fmt: skip
    runner = ClaudeCodeRunner(api_key=KEY, settings=ClaudeCodeSettings(base_url=base_url))
    runner.run(runtime, handle, REQUEST)
    return claude


FORBIDDEN = [
    "ANTHROPIC_AUTH_TOKEN",
    "ANTHROPIC_BASE_URL",  # no gateway configured
    "ANTHROPIC_VERTEX_PROJECT_ID",
    "CLAUDE_CODE_OAUTH_TOKEN",
    "CLAUDE_CODE_USE_BEDROCK",
    "CLAUDE_CODE_USE_VERTEX",
    "CLAUDE_CODE_USE_FOUNDRY",
    "AWS_BEARER_TOKEN_BEDROCK",
    "AWS_ACCESS_KEY_ID",
    "AWS_PROFILE",
    "GOOGLE_APPLICATION_CREDENTIALS",
    "CLOUD_ML_REGION",
    "VERTEX_REGION_CLAUDE_3_5_SONNET",
]


@pytest.mark.parametrize("name", FORBIDDEN)
def test_each_forbidden_variable_refuses_before_claude_starts(tmp_path: Path, name: str) -> None:
    claude = Claude()
    with pytest.raises(PolicyViolationError, match=name) as refused:
        claude = run(tmp_path, ProbeStandIn(image_env=[name]))
    assert claude.calls == 0
    assert KEY not in str(refused.value)


def test_a_configured_gateway_url_is_the_one_allowed_base_url(tmp_path: Path) -> None:
    probe = ProbeStandIn()
    claude = run(tmp_path, probe, base_url=GATEWAY)
    assert claude.calls == 1
    assert claude.env["ANTHROPIC_BASE_URL"] == GATEWAY


@pytest.mark.parametrize(
    "path",
    [
        "/home/ilmar/.claude/.credentials.json",
        "/home/ilmar/.claude.json",
        "/home/ilmar/.claude/settings.json",
        "/home/ilmar/.aws",
        "/home/ilmar/.config/gcloud",
        "/etc/claude-code/managed-settings.json",
    ],
)
def test_each_credential_file_refuses(tmp_path: Path, path: str) -> None:
    with pytest.raises(PolicyViolationError, match="files"):
        run(tmp_path, ProbeStandIn(files=[path]))


@pytest.mark.parametrize("path", [".claude/settings.json", ".claude/settings.local.json"])
def test_project_settings_with_a_credential_setting_refuse(tmp_path: Path, path: str) -> None:
    with pytest.raises(PolicyViolationError, match="project settings"):
        run(tmp_path, ProbeStandIn(settings=[path]))


@pytest.mark.parametrize(
    "probe", [ProbeStandIn(exit_code=127), ProbeStandIn(finish=False)], ids=["fails", "cut-short"]
)
def test_a_probe_that_does_not_complete_never_lets_claude_start(
    tmp_path: Path, probe: ProbeStandIn
) -> None:
    claude = Claude()
    with pytest.raises(ProviderError, match="probe did not complete"):
        claude = run(tmp_path, probe)
    assert claude.calls == 0


def test_the_probe_sees_exactly_the_environment_claude_gets(tmp_path: Path) -> None:
    probe = ProbeStandIn()
    claude = run(tmp_path, probe)
    assert claude.calls == 1
    assert probe.seen == [claude.env]


def test_the_runner_environment_passes_its_own_rule() -> None:
    env = ClaudeCodeRunner(api_key=KEY)._environment()
    assert forbidden_variables(env, gateway_configured=False) == []
    gateway_env = ClaudeCodeRunner(
        api_key=KEY, settings=ClaudeCodeSettings(base_url=GATEWAY)
    )._environment()
    assert forbidden_variables(gateway_env, gateway_configured=True) == []


def test_parse_and_refusal() -> None:
    assert parse_probe("ENV A\n") is None  # no end marker
    report = parse_probe("ENV ANTHROPIC_API_KEY\nENV PATH\nILMAR-PROBE-END\n")
    assert report is not None
    assert refusal(report, gateway_configured=False) is None


# -- the real probe script in a real shell --------------------------------------------------


def real_probe(workdir: Path, home: Path, env: dict[str, str]) -> str:
    sh = shutil.which("sh")
    assert sh is not None
    done = subprocess.run(  # noqa: S603 - fixed argv: a shell and the probe script
        [sh, "-c", PROBE_SCRIPT], cwd=workdir, capture_output=True, text=True, check=True,
        env={"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(home), **env},
    )  # fmt: skip
    return done.stdout


def test_the_real_probe_reports_a_clean_sandbox_as_clean(tmp_path: Path) -> None:
    home, work = tmp_path / "home", tmp_path / "work"
    home.mkdir()
    (work / ".claude").mkdir(parents=True)
    (work / ".claude" / "settings.json").write_text('{"permissions": {"allow": ["Bash"]}}')
    report = parse_probe(real_probe(work, home, {"ANTHROPIC_API_KEY": "x"}))
    assert report is not None
    assert refusal(report, gateway_configured=False) is None
    assert "ANTHROPIC_API_KEY" in report.variables


def test_the_real_probe_finds_every_kind_of_route(tmp_path: Path) -> None:
    home, work = tmp_path / "home", tmp_path / "work"
    (home / ".claude").mkdir(parents=True)
    (home / ".claude" / ".credentials.json").write_text("{}")
    (work / ".claude").mkdir(parents=True)
    (work / ".claude" / "settings.local.json").write_text('{"apiKeyHelper": "/bin/key"}')
    output = real_probe(work, home, {"ANTHROPIC_API_KEY": "x", "ANTHROPIC_AUTH_TOKEN": "secret"})
    assert "secret" not in output  # names only, never values
    report = parse_probe(output)
    assert report is not None
    reason = refusal(report, gateway_configured=False)
    assert reason is not None
    assert "ANTHROPIC_AUTH_TOKEN" in reason
    assert ".credentials.json" in reason
    assert ".claude/settings.local.json" in reason
