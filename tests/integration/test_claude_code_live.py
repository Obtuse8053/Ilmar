# SPDX-License-Identifier: Apache-2.0
"""Opt-in live test: real Claude Code in an ALLOWLIST sandbox (#8, ADR-0011 §6).

Records the hosts Claude Code actually contacts through the egress proxy and
fails if it needed anything outside the allow-list. Needs, besides the Podman
suite's requirements:

* ``ILMAR_TEST_CLAUDE_IMAGE``: an agent image built from
  ``images/claude-code/Containerfile`` at a pinned version (built locally under
  Anthropic's terms; Ilmar never distributes it);
* ``ILMAR_TEST_PODMAN_PROXY_IMAGE``: an image with ``python3`` for the proxy;
* ``ANTHROPIC_API_KEY``: a model credential (costs a few cents);
* optionally ``ILMAR_TEST_CLAUDE_VERSION`` to check the pinned version.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

import pytest

from ilmar.adapters.claude_code import ClaudeCodeRunner, ClaudeCodeSettings
from ilmar.adapters.podman import EgressSettings, PodmanRuntime
from ilmar.core.layout import WorkspaceLayout
from ilmar.domain.agent import AgentOutcome, AgentRequest
from ilmar.domain.workspace import ContainerSpec, NetworkPolicy, ResourceOwner

from helpers import task
from integration.podman_support import (
    PODMAN,
    PROXY_IMAGE,
    identity_of,
    make_workspace,
    podman,
    remove_everything,
)

pytestmark = pytest.mark.integration

IMAGE = os.environ.get("ILMAR_TEST_CLAUDE_IMAGE", "")
KEY = os.environ.get("ANTHROPIC_API_KEY", "")
VERSION = os.environ.get("ILMAR_TEST_CLAUDE_VERSION") or None
NAME = "ilmar-it-claude-live"
ALLOWED = ("api.anthropic.com",)


@pytest.mark.skipif(
    not (PODMAN and IMAGE and KEY and PROXY_IMAGE),
    reason="set ILMAR_TEST_CLAUDE_IMAGE, ILMAR_TEST_PODMAN_PROXY_IMAGE and ANTHROPIC_API_KEY",
)
def test_claude_code_completes_a_tiny_task_using_only_allowed_hosts(tmp_path: Path) -> None:
    layout = WorkspaceLayout.under(tmp_path.resolve() / "state")
    layout.workspaces_root.mkdir(parents=True)
    workspace = make_workspace(layout, "live")
    (workspace / "README.md").write_text("A scratch project.\n")
    owner = ResourceOwner(instance_id="it", execution_id="live", attempt=1, token="tok-live")
    runtime = PodmanRuntime(layout=layout, egress=EgressSettings(proxy_image=PROXY_IMAGE))
    remove_everything(NAME)
    handle = runtime.start(
        ContainerSpec(
            image=IMAGE,
            host_workspace=workspace,
            name=NAME,
            owner=owner,
            workspace_identity=identity_of(workspace),
            network=NetworkPolicy.ALLOWLIST,
            allowed_hosts=ALLOWED,
        )
    )
    try:
        runner = ClaudeCodeRunner(
            api_key=KEY, settings=ClaudeCodeSettings(max_turns=5, expected_version=VERSION)
        )
        result = runner.run(
            runtime,
            handle,
            AgentRequest(
                task=task("live", title="Create a file"),
                instructions="Create a file named hello.txt containing exactly: hi",
                timeout_seconds=300,
            ),
        )
        proxy_log = podman("logs", f"{NAME}-egress")
        logs = proxy_log.stdout + proxy_log.stderr
        contacted = sorted(set(re.findall(r"ilmar-egress ALLOW host=(\S+)", logs)))
        denied = [line for line in logs.splitlines() if "ilmar-egress DENY" in line]
        assert not denied, f"Claude Code needed hosts outside the allow-list: {denied}"
        assert set(contacted) <= set(ALLOWED), contacted
        assert result.outcome is AgentOutcome.COMPLETED, result.summary
        assert (workspace / "hello.txt").read_text().strip() == "hi"
    finally:
        runtime.stop(handle, owner=owner)
