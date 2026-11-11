# SPDX-License-Identifier: Apache-2.0
"""A required live run must name a pinned Claude Code version (ADR-0035 §6, #13).

An unpinned run under ``ILMAR_LIVE_REQUIRED=1`` fails instead of producing
something that could pass for Gate 1 evidence. The last tests run the real
live-test module in a child pytest, with no credentials and no Podman, to
check the wiring rather than only the helper.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

import live_harness as h

ROOT = Path(__file__).resolve().parents[2]
LIVE_TEST = ROOT / "tests" / "integration" / "test_claude_code_live.py"


@pytest.mark.parametrize("required", [True, False])
def test_a_pinned_version_is_returned(required: bool) -> None:
    assert h.pinned_claude_version({h.VERSION_ENV: "2.1.302"}, required=required) == "2.1.302"


def test_a_required_run_without_a_version_fails() -> None:
    with pytest.raises(h.LiveGuardError, match="required"):
        h.pinned_claude_version({}, required=True)
    with pytest.raises(h.LiveGuardError, match="required"):
        h.pinned_claude_version({h.VERSION_ENV: ""}, required=True)


def test_an_optional_run_without_a_version_is_unpinned() -> None:
    assert h.pinned_claude_version({}, required=False) is None


@pytest.mark.parametrize("version", ["latest", "2.1", "v2.1.302", "2.1.302-beta", "2.1.302 "])
@pytest.mark.parametrize("required", [True, False])
def test_a_version_that_is_not_exact_always_fails(version: str, required: bool) -> None:
    with pytest.raises(h.LiveGuardError, match=r"pinned x\.y\.z"):
        h.pinned_claude_version({h.VERSION_ENV: version}, required=required)


def run_live_module(**env: str) -> subprocess.CompletedProcess[str]:
    child = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith(("ILMAR_", "ANTHROPIC_")) and k != "PYTEST_ADDOPTS"
    }
    child.update(env)
    return subprocess.run(  # noqa: S603 - fixed argv: this interpreter and a repository test file
        [sys.executable, "-m", "pytest", "-p", "no:cacheprovider", "-q", "-m", "integration",
         str(LIVE_TEST)],
        cwd=ROOT, env=child, capture_output=True, text=True, timeout=120, check=False,
    )  # fmt: skip


def test_the_required_live_test_fails_without_a_pinned_version() -> None:
    done = run_live_module(ILMAR_LIVE_REQUIRED="1")
    assert done.returncode != 0, done.stdout
    assert "ILMAR_TEST_CLAUDE_VERSION is required" in done.stdout
    assert " passed" not in done.stdout.splitlines()[-1]


def test_the_required_live_test_fails_with_an_unpinned_version() -> None:
    done = run_live_module(ILMAR_LIVE_REQUIRED="1", ILMAR_TEST_CLAUDE_VERSION="latest")
    assert done.returncode != 0, done.stdout
    assert "pinned x.y.z" in done.stdout


def test_an_optional_live_test_without_its_needs_still_skips() -> None:
    done = run_live_module()
    assert done.returncode == 0, done.stdout
    assert "skipped" in done.stdout.splitlines()[-1]
