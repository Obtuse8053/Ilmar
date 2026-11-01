# SPDX-License-Identifier: Apache-2.0
"""Repository-wide policies from MANIFEST §7 and ADR-0003."""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SPDX_HEADER = "# SPDX-License-Identifier: Apache-2.0"
PYTHON_DIRS = ("src", "tests", "scripts")


def _python_files() -> list[Path]:
    return sorted(p for d in PYTHON_DIRS for p in (REPO_ROOT / d).rglob("*.py"))


@pytest.mark.parametrize("path", _python_files(), ids=lambda p: str(p.relative_to(REPO_ROOT)))
def test_python_files_carry_spdx_header(path: Path) -> None:
    first_line = path.read_text().splitlines()[:1]
    assert first_line == [SPDX_HEADER], f"{path} must start with '{SPDX_HEADER}'"


@pytest.mark.parametrize(
    "name",
    ["LICENSE", "NOTICE", "THIRD_PARTY_NOTICES.md", "MANIFEST.md", "AGENTS.md"],
)
def test_required_files_exist(name: str) -> None:
    assert (REPO_ROOT / name).is_file()


def test_license_is_apache_2() -> None:
    text = (REPO_ROOT / "LICENSE").read_text()
    assert "Apache License" in text
    assert "Version 2.0, January 2004" in text
    assert "END OF TERMS AND CONDITIONS" in text


def test_core_has_no_runtime_dependencies() -> None:
    project = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text())["project"]
    assert project["dependencies"] == [], (
        "The core must not have runtime dependencies (ADR-0003). "
        "Declare adapter dependencies as optional extras."
    )
    assert project["license"] == "Apache-2.0"
