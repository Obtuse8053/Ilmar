# SPDX-License-Identifier: Apache-2.0
"""The manual gateway-compatibility workflow (ADR-0037 prerequisite, #111).

It must stay credential-free and hand-started: no secret, no environment, read
permissions only, actions pinned, and the version input never expanded inside a
shell script.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = (ROOT / ".github" / "workflows" / "gateway-compat.yml").read_text()


def test_it_runs_only_when_started_by_hand_on_main() -> None:
    assert re.search(r"^on:\n  workflow_dispatch:\n", WORKFLOW, re.M)
    for trigger in ("push:", "pull_request", "schedule:", "workflow_run"):
        assert trigger not in WORKFLOW
    assert "if: github.ref == 'refs/heads/main'" in WORKFLOW


def test_it_holds_no_secret_and_only_reads() -> None:
    assert "secrets." not in WORKFLOW
    assert "environment:" not in WORKFLOW
    assert "permissions: {}" in WORKFLOW
    granted = re.findall(r"^      (\w[\w-]*): (\w+)$", WORKFLOW, re.M)
    assert ("contents", "read") in granted
    assert not any(level == "write" for _, level in granted)


def test_every_action_is_pinned_to_a_full_commit() -> None:
    uses = re.findall(r"uses: (\S+)", WORKFLOW)
    assert uses
    for action in uses:
        assert re.fullmatch(r"[\w.-]+/[\w.-]+@[0-9a-f]{40}", action), action


def test_the_version_input_is_only_expanded_in_the_environment() -> None:
    expansions = re.findall(r"\$\{\{[^}]*inputs\.[^}]*\}\}", WORKFLOW)
    assert expansions == ["${{ inputs.claude_version }}"]
    assert "ILMAR_TEST_CLAUDE_VERSION: ${{ inputs.claude_version }}" in WORKFLOW


def test_it_runs_the_compatibility_test_as_required_evidence() -> None:
    assert "ILMAR_LIVE_REQUIRED=1" in WORKFLOW  # a skip would be a failure
    assert "tests/integration/test_claude_code_gateway_compat.py" in WORKFLOW
    assert "grep -Eqx '[0-9]+\\.[0-9]+\\.[0-9]+'" in WORKFLOW
