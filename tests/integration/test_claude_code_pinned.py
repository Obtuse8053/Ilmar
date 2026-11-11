# SPDX-License-Identifier: Apache-2.0
"""The pinned Claude Code under the sealed settings locations (ADR-0036, #90).

Needs no Anthropic credential. The real, pinned Claude Code runs in a sealed,
allow-listed sandbox with a deliberately invalid key. The repository it works
in carries ``.claude/settings.json`` with an ``apiKeyHelper`` that would leave
a marker file if it ever ran. The test shows that, on the pinned version:

* Claude Code starts and runs with its settings locations sealed: the
  read-only, empty mounts do not break it;
* it never reads the repository's settings: the helper never runs;
* the API's authentication failure is classified as a provider refusal
  (never retried), and the redacted text is recorded as Gate 1 evidence.

Needs ``podman``, ``ILMAR_TEST_CLAUDE_IMAGE``, ``ILMAR_TEST_PODMAN_PROXY_IMAGE``
and ``ILMAR_TEST_CLAUDE_VERSION`` (an exact ``x.y.z``). It skips without them
unless ``ILMAR_LIVE_REQUIRED=1``.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest

from ilmar.adapters.claude_code import ClaudeCodeRunner, ClaudeCodeSettings
from ilmar.adapters.claude_code.credential_route import SEALED_PATHS
from ilmar.adapters.podman import EgressSettings, PodmanRuntime
from ilmar.core.layout import WorkspaceLayout
from ilmar.domain.agent import AgentOutcome, AgentRequest
from ilmar.domain.workspace import ContainerSpec, NetworkPolicy, ResourceOwner
from live_harness import LiveGuardError, Secret, pinned_claude_version, write_report

from helpers import task
from integration.podman_support import (
    PODMAN,
    PROXY_IMAGE,
    identity_of,
    make_workspace,
    remove_everything,
)

pytestmark = pytest.mark.integration

IMAGE = os.environ.get("ILMAR_TEST_CLAUDE_IMAGE", "")
REQUIRED = os.environ.get("ILMAR_LIVE_REQUIRED") == "1"
NAME = "ilmar-it-claude-pinned"
INVALID_KEY = "sk-ant-api03-ilmar-deliberately-invalid-0000000000"
MARKER = "helper-ran"
NEEDS = "podman, ILMAR_TEST_CLAUDE_IMAGE, ILMAR_TEST_PODMAN_PROXY_IMAGE, ILMAR_TEST_CLAUDE_VERSION"


def version() -> str | None:
    try:
        return pinned_claude_version(os.environ, required=REQUIRED)
    except LiveGuardError as error:
        pytest.fail(str(error))


@pytest.fixture(autouse=True)
def _needs() -> Iterator[None]:
    missing = not (PODMAN and IMAGE and PROXY_IMAGE and version())
    if missing:
        if REQUIRED:
            pytest.fail(f"needs {NEEDS}")
        pytest.skip(f"set {NEEDS}")
    remove_everything(NAME)
    yield
    remove_everything(NAME)


def test_pinned_claude_code_runs_sealed_and_never_reads_repository_settings(
    tmp_path: Path,
) -> None:
    pinned = version()
    assert pinned is not None
    layout = WorkspaceLayout.under(tmp_path.resolve() / "state")
    layout.workspaces_root.mkdir(parents=True)
    workspace = make_workspace(layout, "pinned")
    (workspace / ".claude").mkdir()
    helper = f"/bin/sh -c 'touch /workspace/{MARKER}; echo sk-ant-api03-from-the-helper'"
    (workspace / ".claude" / "settings.json").write_text(
        '{"apiKeyHelper": "' + helper.replace('"', '\\\\"') + '"}'
    )
    owner = ResourceOwner(instance_id="it", execution_id="pinned", attempt=1, token="tok-pin")
    runtime = PodmanRuntime(layout=layout, egress=EgressSettings(proxy_image=PROXY_IMAGE))
    handle = runtime.start(
        ContainerSpec(image=IMAGE, host_workspace=workspace, name=NAME, owner=owner,
                      workspace_identity=identity_of(workspace),
                      network=NetworkPolicy.ALLOWLIST, allowed_hosts=("api.anthropic.com",),
                      sealed_paths=SEALED_PATHS)
    )  # fmt: skip
    try:
        runner = ClaudeCodeRunner(
            api_key=INVALID_KEY,
            settings=ClaudeCodeSettings(max_turns=2, expected_version=pinned),
        )
        result = runner.run(
            runtime,
            handle,
            AgentRequest(task=task("pinned"), instructions="Reply with OK.", timeout_seconds=180,
                         stall_timeout_seconds=120),
        )  # fmt: skip
    finally:
        runtime.stop(handle, owner=owner)
    assert not (workspace / MARKER).exists(), "Claude Code ran the repository's apiKeyHelper"
    assert INVALID_KEY not in result.summary
    directory = os.environ.get("ILMAR_LIVE_REPORT_DIR")
    if directory:
        write_report(Path(directory), "issue-90-pinned-sealed-run", {
            "provenance": "recorded",
            "claude_code_version": pinned,
            "image": IMAGE,
            "at": datetime.now(UTC).isoformat(),
            "outcome": result.outcome.value,
            "summary": result.summary[:500],
        }, [Secret("invalid key", INVALID_KEY)])  # fmt: skip
    assert result.outcome is AgentOutcome.PROVIDER_REFUSED, result.summary
