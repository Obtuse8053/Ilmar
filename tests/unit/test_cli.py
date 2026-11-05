# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

from pathlib import Path

import pytest

from ilmar import __version__
from ilmar.cli import main

REPO_ROOT = Path(__file__).resolve().parents[2]


def test_version(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exit_info:
        main(["--version"])
    assert exit_info.value.code == 0
    assert __version__ in capsys.readouterr().out


def test_validate_example_configuration(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["config", "validate", str(REPO_ROOT / "ilmar.example.toml")]) == 0
    out = capsys.readouterr().out
    assert "OK" in out
    assert "task_provider: github" in out


def test_validate_reports_errors(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    path = tmp_path / "ilmar.toml"
    path.write_text('[adapters.task_provider]\nkind = "github"\n')
    assert main(["config", "validate", str(path)]) == 1
    assert "missing" in capsys.readouterr().err


def test_no_command_prints_help(capsys: pytest.CaptureFixture[str]) -> None:
    assert main([]) == 2
    assert "usage" in capsys.readouterr().out


def test_validate_shows_target_sandbox_and_secret_names_only(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_VISIBLE-IF-LEAKED")
    assert main(["config", "validate", str(REPO_ROOT / "ilmar.example.toml")]) == 0
    out = capsys.readouterr().out
    assert "target: github:Obtuse8053/Ilmar (main)" in out
    assert "network allowlist (api.anthropic.com)" in out
    assert "secrets read from: ANTHROPIC_API_KEY, GITHUB_TOKEN" in out
    assert "ghp_VISIBLE-IF-LEAKED" not in out


def test_check_environment_reports_missing_secrets_without_values(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_VISIBLE-IF-LEAKED")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    path = str(REPO_ROOT / "ilmar.example.toml")
    assert main(["config", "validate", "--check-environment", path]) == 1
    captured = capsys.readouterr()
    assert "secret GITHUB_TOKEN: set" in captured.out
    assert "secret ANTHROPIC_API_KEY: missing" in captured.out
    assert "not set: ANTHROPIC_API_KEY" in captured.err
    assert "ghp_VISIBLE-IF-LEAKED" not in captured.out + captured.err
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-x")
    assert main(["config", "validate", "--check-environment", path]) == 0


def test_validate_runs_the_adapter_checks(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    text = (REPO_ROOT / "ilmar.example.toml").read_text()
    path = tmp_path / "ilmar.toml"
    path.write_text(text.replace('kind = "podman"', 'kind = "docker"'))
    assert main(["config", "validate", str(path)]) == 1
    assert "kind 'docker' is not known" in capsys.readouterr().err


def test_validate_requires_a_verification_policy(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    text = (REPO_ROOT / "ilmar.example.toml").read_text()
    path = tmp_path / "ilmar.toml"
    path.write_text(text.split("[verification]")[0])
    assert main(["config", "validate", str(path)]) == 1
    assert "[verification] is required" in capsys.readouterr().err


def test_run_needs_an_explicit_configuration_and_once(capsys: pytest.CaptureFixture[str]) -> None:
    for argv in (["run", "--once"], ["run", "--config", "ilmar.toml"]):
        with pytest.raises(SystemExit) as info:
            main(argv)
        assert info.value.code == 2  # argparse usage error, before anything happens
    capsys.readouterr()


def test_run_with_an_invalid_configuration_exits_3(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "ilmar.toml"
    path.write_text("[adapters.task_provider]\nkind = 'github'\n")
    path.chmod(0o600)
    assert main(["run", "--once", "--config", str(path)]) == 3
    assert "error" in capsys.readouterr().err


def test_run_on_a_locked_instance_exits_3_and_changes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import stat

    from ilmar.core.layout import WorkspaceLayout
    from ilmar.core.ownership import InstanceLock

    tools = tmp_path / "bin"
    tools.mkdir()
    for name in ("podman", "git"):
        (tools / name).write_text("#!/bin/sh\nexit 99\n")
        (tools / name).chmod(stat.S_IRWXU)
    text = (REPO_ROOT / "ilmar.example.toml").read_text()
    text = text.replace('state_dir = ".ilmar"', f'state_dir = "{tmp_path / "state"}"')
    text = text.replace('kind = "podman"\n', 'kind = "podman"\n', 1)
    text = text.replace(
        "options = { proxy_image", f'options = {{ podman = "{tools / "podman"}", proxy_image', 1
    )
    text = text.replace(
        'base_branch = "main", token_env',
        f'base_branch = "main", git = "{tools / "git"}", token_env',
        1,
    )
    text = text.replace('path = ".ilmar/executions.sqlite3"', f'path = "{tmp_path / "db.sqlite3"}"')
    config = tmp_path / "ilmar.toml"
    config.write_text(text)
    config.chmod(0o600)
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_x")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-y")
    (tmp_path / "state").mkdir()
    held = InstanceLock.acquire(WorkspaceLayout.under((tmp_path / "state").resolve()))
    try:
        assert main(["run", "--once", "--config", str(config)]) == 3
        assert "another coordinator" in capsys.readouterr().err
    finally:
        held.release()
