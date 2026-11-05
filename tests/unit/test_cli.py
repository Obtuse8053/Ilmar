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
