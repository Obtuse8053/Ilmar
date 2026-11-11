# SPDX-License-Identifier: Apache-2.0
"""Every real-Podman integration test runs in CI's required Podman job.

The job lists its test files explicitly. A new ``*_podman.py`` file left off
that list would never run, and a required test (such as the coordinator
SIGKILL test, ADR-0029 §1) would look present while proving nothing.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = (ROOT / ".github" / "workflows" / "ci.yml").read_text()


def test_every_podman_integration_file_is_in_the_podman_job() -> None:
    listed = set(re.findall(r"tests/integration/(test_\w+\.py)", WORKFLOW))
    on_disk = {
        path.name
        for path in (ROOT / "tests" / "integration").glob("test_*.py")
        if "podman" in path.stem
    }
    assert on_disk, "no Podman integration tests found"
    assert on_disk <= listed, sorted(on_disk - listed)


def test_the_sigkill_test_is_required_in_ci() -> None:
    assert "tests/integration/test_sigkill_podman.py" in WORKFLOW
    assert 'ILMAR_TEST_PODMAN_REQUIRED: "1"' in WORKFLOW
