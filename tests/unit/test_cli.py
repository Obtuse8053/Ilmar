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
