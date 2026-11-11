# SPDX-License-Identifier: Apache-2.0
"""One model-credential path, checked before Claude Code starts (ADR-0029 §5, ADR-0036).

The probe runs in the sandbox with exactly the environment Claude Code gets.
Every other route, every unsealed settings location and every incomplete
scan refuses the run before ``claude`` is invoked, naming what was found but
never a value. The last part runs the real probe script in a real shell,
including with ``awk`` or ``grep`` missing and with unreadable settings.
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
    SEALED_PATHS,
    forbidden_variables,
    parse_probe,
    refusal,
)
from ilmar.domain.agent import AgentRequest
from ilmar.domain.errors import PolicyViolationError, ProviderError
from ilmar.domain.workspace import CommandResult, ContainerSpec, ResourceOwner, SealedPath

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


def run(
    tmp_path: Path, probe: ProbeStandIn, claude: Claude, *, base_url: str | None = None
) -> None:
    runtime = FakeContainerRuntime()
    runtime.register("claude", claude)
    allow_probe(runtime, probe)
    handle = runtime.start(
        ContainerSpec(image="img", host_workspace=tmp_path, name="agent", owner=OWNER,
                      workspace_identity=identity_of(tmp_path), sealed_paths=SEALED_PATHS)
    )  # fmt: skip
    runner = ClaudeCodeRunner(api_key=KEY, settings=ClaudeCodeSettings(base_url=base_url))
    runner.run(runtime, handle, REQUEST)


def refused(tmp_path: Path, probe: ProbeStandIn, match: str) -> str:
    claude = Claude()
    with pytest.raises(PolicyViolationError, match=match) as error:
        run(tmp_path, probe, claude)
    assert claude.calls == 0, "claude ran"
    assert KEY not in str(error.value)
    return str(error.value)


FORBIDDEN = [
    "ANTHROPIC_AUTH_TOKEN",
    "ANTHROPIC_BASE_URL",  # no gateway configured
    "ANTHROPIC_VERTEX_PROJECT_ID",
    "CLAUDE_CODE_OAUTH_TOKEN",
    "CLAUDE_CODE_USE_BEDROCK",
    "CLAUDE_CODE_USE_VERTEX",
    "CLAUDE_CODE_USE_FOUNDRY",
    "CLAUDE_CONFIG_DIR",
    "AWS_BEARER_TOKEN_BEDROCK",
    "AWS_ACCESS_KEY_ID",
    "AWS_PROFILE",
    "GOOGLE_APPLICATION_CREDENTIALS",
    "CLOUD_ML_REGION",
    "VERTEX_REGION_CLAUDE_3_5_SONNET",
]


@pytest.mark.parametrize("name", FORBIDDEN)
def test_each_forbidden_variable_refuses_before_claude_starts(tmp_path: Path, name: str) -> None:
    refused(tmp_path, ProbeStandIn(image_env=[name]), name)


def test_a_configured_gateway_url_is_the_one_allowed_base_url(tmp_path: Path) -> None:
    claude = Claude()
    run(tmp_path, ProbeStandIn(), claude, base_url=GATEWAY)
    assert claude.calls == 1
    assert claude.env["ANTHROPIC_BASE_URL"] == GATEWAY


@pytest.mark.parametrize(
    "path",
    [
        "/home/ilmar/.claude.json",
        "/home/ilmar/.aws",
        "/home/ilmar/.config/gcloud",
        "/etc/claude-code/managed-settings.json",
    ],
)
def test_each_credential_file_refuses(tmp_path: Path, path: str) -> None:
    refused(tmp_path, ProbeStandIn(files=[path]), "files")


@pytest.mark.parametrize("path", [".claude/settings.json", ".claude/settings.local.json"])
def test_project_settings_with_a_credential_setting_refuse(tmp_path: Path, path: str) -> None:
    refused(tmp_path, ProbeStandIn(settings=[path]), "project settings")


# -- the sealed settings locations (ADR-0036) ------------------------------------------------

TARGETS = [
    "/workspace/.claude",
    "/home/ilmar/.claude",
    "/home/ilmar/.claude/settings.json",
    "/home/ilmar/.claude/settings.local.json",
    "/home/ilmar/.claude/.credentials.json",
]


def test_the_sealed_paths_cover_every_settings_location_claude_code_reads() -> None:
    assert {(s.path, s.writable) for s in SEALED_PATHS} == {
        (".claude", False),
        ("~/.claude", True),
        ("~/.claude/settings.json", False),
        ("~/.claude/settings.local.json", False),
        ("~/.claude/.credentials.json", False),
    }


@pytest.mark.parametrize("target", TARGETS)
def test_each_unsealed_settings_location_refuses(tmp_path: Path, target: str) -> None:
    message = refused(tmp_path, ProbeStandIn(unsealed=[target]), "not sealed")
    assert target in message


@pytest.mark.parametrize("target", TARGETS)
def test_a_sealed_path_with_the_wrong_access_refuses(tmp_path: Path, target: str) -> None:
    refused(tmp_path, ProbeStandIn(wrong_access=[target]), "not sealed")


@pytest.mark.parametrize(
    ("parent", "hidden"),
    [("/workspace", "/workspace/.claude"), ("/home/ilmar", "/home/ilmar/.claude"),
     ("/home/ilmar/.claude", "/home/ilmar/.claude/settings.json"), ("/", "/workspace/.claude")],
)  # fmt: skip
def test_a_seal_hidden_by_a_later_mount_on_a_parent_refuses(
    tmp_path: Path, parent: str, hidden: str
) -> None:
    """The seal must be the mount that is visible, not one buried under a
    later mount of a parent directory."""
    message = refused(tmp_path, ProbeStandIn(mounted_after=[parent]), "not sealed")
    assert hidden in message


def test_a_non_empty_seal_refuses(tmp_path: Path) -> None:
    refused(tmp_path, ProbeStandIn(nonempty=[".claude"]), "not empty")


# -- a probe that does not finish never lets Claude Code start -----------------------------


@pytest.mark.parametrize(
    "probe",
    [
        ProbeStandIn(exit_code=127),
        ProbeStandIn(exit_code=3),
        ProbeStandIn(drop=["ILMAR-PROBE-END"]),
        ProbeStandIn(drop=["ENV-DONE"]),
        ProbeStandIn(drop=["MOUNTS-DONE"]),
        ProbeStandIn(drop=["HOME-DIR"]),
        ProbeStandIn(drop=["WORKDIR"]),
        ProbeStandIn(extra=["ILMAR-PROBE-ERROR read .claude/settings.json"]),
        ProbeStandIn(extra=["ILMAR-PROBE-ERROR missing awk"]),
    ],
    ids=["exit-127", "exit-3", "no-end", "no-env-done", "no-mounts-done", "no-home",
         "no-workdir", "read-error", "missing-tool"],
)  # fmt: skip
def test_an_incomplete_probe_never_lets_claude_start(tmp_path: Path, probe: ProbeStandIn) -> None:
    claude = Claude()
    with pytest.raises(ProviderError, match="probe did not complete"):
        run(tmp_path, probe, claude)
    assert claude.calls == 0


@pytest.mark.parametrize(
    "hidden", ["ANTHROPIC_API_KEY", "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"]
)
def test_an_environment_scan_that_misses_a_variable_refuses(tmp_path: Path, hidden: str) -> None:
    """Every variable the runner passes must come back: otherwise the scan was
    incomplete and could have missed others."""
    refused(tmp_path, ProbeStandIn(hide_env=[hidden]), "incomplete environment scan")


def test_the_probe_sees_exactly_the_environment_claude_gets(tmp_path: Path) -> None:
    probe = ProbeStandIn()
    claude = Claude()
    run(tmp_path, probe, claude)
    assert claude.calls == 1
    assert probe.seen == [claude.env]


def test_the_runner_environment_passes_its_own_rule() -> None:
    env = ClaudeCodeRunner(api_key=KEY)._environment()
    assert forbidden_variables(env, gateway_configured=False) == []
    assert "CLAUDE_CONFIG_DIR" not in env
    gateway_env = ClaudeCodeRunner(
        api_key=KEY, settings=ClaudeCodeSettings(base_url=GATEWAY)
    )._environment()
    assert forbidden_variables(gateway_env, gateway_configured=True) == []


# -- the real probe script in a real shell --------------------------------------------------


def real_probe(
    workdir: Path, home: Path, env: Mapping[str, str], *, path: str | None = None
) -> subprocess.CompletedProcess[str]:
    sh = shutil.which("sh")
    assert sh is not None
    return subprocess.run(  # noqa: S603 - fixed argv: a shell and the probe script
        [sh, "-c", PROBE_SCRIPT], cwd=workdir, capture_output=True, text=True, check=False,
        env={"PATH": path or os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(home), **env},
    )  # fmt: skip


@pytest.fixture
def dirs(tmp_path: Path) -> tuple[Path, Path]:
    home, work = tmp_path / "home", tmp_path / "work"
    home.mkdir()
    (work / ".claude").mkdir(parents=True)
    return work, home


def test_the_real_probe_reports_a_clean_sandbox_completely(dirs: tuple[Path, Path]) -> None:
    work, home = dirs
    done = real_probe(work, home, {"ANTHROPIC_API_KEY": "x"})
    assert done.returncode == 0, done.stdout
    report = parse_probe(done.stdout)
    assert report is not None
    assert str(report.home) == str(home)
    assert str(report.workdir) == str(work)
    assert any(point == "/" for point, _ in report.mounts)  # the real mount table was read
    assert report.nonempty == ()
    # Outside a sandbox nothing is sealed; everything else is clean.
    assert refusal(report, gateway_configured=False, expected_variables=["ANTHROPIC_API_KEY"],
                   sealed_paths=()) is None  # fmt: skip
    reason = refusal(report, gateway_configured=False)
    assert reason is not None
    assert "not sealed" in reason


def test_the_real_probe_finds_every_kind_of_route(dirs: tuple[Path, Path]) -> None:
    work, home = dirs
    (home / ".claude").mkdir()
    (home / ".claude" / ".credentials.json").write_text("{}")
    (home / ".claude.json").write_text("{}")
    (work / ".claude" / "settings.local.json").write_text('{"apiKeyHelper": "/bin/key"}')
    done = real_probe(work, home, {"ANTHROPIC_API_KEY": "x", "ANTHROPIC_AUTH_TOKEN": "secret"})
    assert "secret" not in done.stdout  # names only, never values
    report = parse_probe(done.stdout)
    assert report is not None
    reason = refusal(report, gateway_configured=False, sealed_paths=())
    assert reason is not None
    for found in ("ANTHROPIC_AUTH_TOKEN", ".claude.json", ".claude/settings.local.json"):
        assert found in reason
    # The credentials file is a seal: outside a sandbox it is simply not sealed.
    sealed = refusal(report, gateway_configured=False)
    assert sealed is not None
    assert str(home / ".claude" / ".credentials.json") in sealed


@pytest.mark.parametrize(
    ("make", "reported"),
    [
        (lambda w, h: (w / ".claude" / "settings.json").write_text("{}"), ".claude"),
        (lambda w, h: (w / ".claude" / ".hidden").write_text("x"), ".claude"),
        (lambda w, h: (w / ".claude" / "..odd").mkdir(), ".claude"),
        (
            lambda w, h: (
                (h / ".claude" / "settings.json").mkdir(parents=True)
                or (h / ".claude" / "settings.json" / "x").write_text("x")
            ),
            "/.claude/settings.json",
        ),
        (
            lambda w, h: (
                (h / ".claude" / "settings.local.json").mkdir(parents=True)
                or (h / ".claude" / "settings.local.json" / "link").symlink_to("/nowhere")
            ),
            "/.claude/settings.local.json",
        ),
    ],
    ids=["project-file", "project-hidden", "project-dotdot-name", "user-file", "user-link"],
)
def test_the_real_probe_finds_anything_inside_a_read_only_seal(
    dirs: tuple[Path, Path], make: object, reported: str
) -> None:
    """A read-only seal must be empty: Podman's copy-up once copied the
    repository's settings into it (the CI failure on #109)."""
    work, home = dirs
    make(work, home)  # type: ignore[operator]
    report = parse_probe(real_probe(work, home, {}).stdout)
    assert report is not None
    assert any(d.endswith(reported) for d in report.nonempty), report.nonempty
    reason = refusal(report, gateway_configured=False, sealed_paths=())
    assert reason is not None
    assert "not empty" in reason


def tool_dir(tmp_path: Path, *tools: str) -> str:
    """A PATH holding only ``sh`` and ``tools`` (symlinks to the real ones)."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for tool in ("sh", *tools):
        real = shutil.which(tool)
        assert real is not None, tool
        (bin_dir / tool).symlink_to(real)
    return str(bin_dir)


@pytest.mark.parametrize(("present", "missing"), [(("grep",), "awk"), (("awk",), "grep")])
def test_the_real_probe_fails_when_a_tool_is_missing(
    dirs: tuple[Path, Path], tmp_path: Path, present: tuple[str, ...], missing: str
) -> None:
    work, home = dirs
    done = real_probe(work, home, {}, path=tool_dir(tmp_path, *present))
    assert done.returncode != 0
    assert f"ILMAR-PROBE-ERROR missing {missing}" in done.stdout
    assert parse_probe(done.stdout) is None


@pytest.mark.parametrize("kind", ["directory", "dangling-symlink", "unreadable"])
def test_the_real_probe_fails_when_project_settings_cannot_be_read(
    dirs: tuple[Path, Path], kind: str
) -> None:
    work, home = dirs
    settings = work / ".claude" / "settings.json"
    if kind == "directory":
        settings.mkdir()
    elif kind == "dangling-symlink":
        settings.symlink_to(work / "nowhere.json")
    else:
        if os.geteuid() == 0:
            pytest.skip("root can read a mode-000 file")
        settings.write_text('{"apiKeyHelper": "x"}')
        settings.chmod(0)
    done = real_probe(work, home, {})
    assert done.returncode == 4, done.stdout
    assert "ILMAR-PROBE-ERROR read .claude/settings.json" in done.stdout
    assert parse_probe(done.stdout) is None


def test_parse_requires_every_step() -> None:
    full = (
        "ENV ANTHROPIC_API_KEY\nENV-DONE\nHOME-DIR /h\nWORKDIR /w\nMOUNT / rw\nMOUNTS-DONE\n"
        "ILMAR-PROBE-END\n"
    )
    assert parse_probe(full) is not None
    for marker in ("ENV-DONE", "HOME-DIR /h", "WORKDIR /w", "MOUNTS-DONE", "ILMAR-PROBE-END"):
        assert parse_probe(full.replace(marker + "\n", "")) is None, marker
    assert parse_probe(full + "ILMAR-PROBE-ERROR read x\n") is None


def test_a_later_mount_over_the_same_point_wins() -> None:
    out = (
        "ENV-DONE\nHOME-DIR /h\nWORKDIR /w\nMOUNT /w/.claude ro\nMOUNT /w/.claude rw\n"
        "MOUNTS-DONE\nILMAR-PROBE-END\n"
    )
    report = parse_probe(out)
    assert report is not None
    reason = refusal(report, gateway_configured=False, sealed_paths=(SealedPath(".claude"),))
    assert reason is not None
    assert "/w/.claude" in reason
