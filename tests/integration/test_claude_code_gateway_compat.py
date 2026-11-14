# SPDX-License-Identifier: Apache-2.0
"""Does the pinned Claude Code work through a plain-HTTP gateway? (ADR-0037, #111)

ADR-0037 puts a model gateway in the sandbox's egress sidecar. Claude Code
would reach it at ``ANTHROPIC_BASE_URL=http://<sidecar>`` with a throwaway
per-attempt token, and the real key would never enter the sandbox. Before that
gateway is built, this test checks the one behaviour it depends on, against the
real, pinned Claude Code. No credential and no network are needed: the sandbox
has no network at all, and a stand-in API (``gateway_stub/stub.py``) runs inside
it on ``127.0.0.1``.

It shows that:
* Claude Code accepts a plain-HTTP ``ANTHROPIC_BASE_URL`` and completes a run
  through it, with the settings locations sealed;
* every request carries the throwaway token and nothing else as its credential.

A second test (C1) runs the real topology: the gateway in a separate sidecar
container on the sandbox's private network, the provider on the host. A pass
of the first test alone shows loopback compatibility only.

It also records the shape of every request (paths, models, ``max_tokens``,
thinking budgets, tool and content-block types, sizes and beta headers; never
prompt text). ADR-0037's request limits and model allow-list are set from that
record.

Needs ``podman``, ``ILMAR_TEST_CLAUDE_IMAGE`` and ``ILMAR_TEST_CLAUDE_VERSION``.
It skips without them unless ``ILMAR_LIVE_REQUIRED=1``. With
``ILMAR_LIVE_REPORT_DIR`` set, the record is written as
``issue-111-gateway-compat.json``.
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest

from gateway_policy import BUDGET, POLICY
from ilmar.adapters.claude_code import ClaudeCodeRunner, ClaudeCodeSettings
from ilmar.adapters.claude_code.credential_route import SEALED_PATHS
from ilmar.adapters.podman import GatewaySettings, PodmanRuntime
from ilmar.core.layout import WorkspaceLayout
from ilmar.domain.agent import AgentOutcome, AgentRequest
from ilmar.domain.workspace import ContainerSpec, NetworkPolicy, ResourceOwner
from live_harness import LiveGuardError, Secret, pinned_claude_version, write_report

from helpers import task
from integration.gateway_support import Provider
from integration.podman_support import (
    PODMAN,
    host_ipv4,
    identity_of,
    make_workspace,
    podman,
    remove_everything,
)

pytestmark = pytest.mark.integration

IMAGE = os.environ.get("ILMAR_TEST_CLAUDE_IMAGE", "")
REQUIRED = os.environ.get("ILMAR_LIVE_REQUIRED") == "1"
NAME = "ilmar-it-gateway-compat"
TOKEN = "ilmar-attempt-0123456789abcdef0123456789abcdef"  # throwaway, like the gateway's
PORT = 18080
STUB = Path(__file__).resolve().parent / "gateway_stub" / "stub.py"
HERE = ".ilmar-gateway"  # in the workspace, so the host can read the log afterwards
NEEDS = "podman, ILMAR_TEST_CLAUDE_IMAGE, ILMAR_TEST_CLAUDE_VERSION"

# Starts the stand-in API, waits for it, then runs the real claude with every
# argument the runner gave. `--version` goes straight to claude.
WRAPPER = f"""#!/bin/sh
if [ "$1" = "--version" ]; then exec claude --version; fi
cd /workspace
PROBE="import socket; socket.create_connection(('127.0.0.1', {PORT}), 1)"
# Its own stdio, so it never holds Claude Code's output stream open.
python3 {HERE}/stub.py {PORT} {HERE}/requests.jsonl "$ANTHROPIC_API_KEY" \
  </dev/null >/dev/null 2>&1 &
for _ in $(seq 1 50); do
  python3 -c "$PROBE" 2>/dev/null && break
  sleep 0.1
done
export ANTHROPIC_BASE_URL=http://127.0.0.1:{PORT}
exec claude "$@"
"""


def version() -> str | None:
    try:
        return pinned_claude_version(os.environ, required=REQUIRED)
    except LiveGuardError as error:
        pytest.fail(str(error))


@pytest.fixture(autouse=True)
def _needs() -> Iterator[None]:
    if not (PODMAN and IMAGE and version()):
        if REQUIRED:
            pytest.fail(f"needs {NEEDS}")
        pytest.skip(f"set {NEEDS}")
    remove_everything(NAME)
    yield
    remove_everything(NAME)


def test_pinned_claude_code_works_through_a_plain_http_gateway(tmp_path: Path) -> None:
    pinned = version()
    assert pinned is not None
    layout = WorkspaceLayout.under(tmp_path.resolve() / "state")
    layout.workspaces_root.mkdir(parents=True)
    workspace = make_workspace(layout, "gateway")
    helper = workspace / HERE
    helper.mkdir()
    (helper / "stub.py").write_text(STUB.read_text())
    (helper / "claude-via-gateway").write_text(WRAPPER)
    (helper / "claude-via-gateway").chmod(0o755)
    owner = ResourceOwner(instance_id="it", execution_id="gw", attempt=1, token="tok-gw")
    runtime = PodmanRuntime(layout=layout)
    handle = runtime.start(
        ContainerSpec(image=IMAGE, host_workspace=workspace, name=NAME, owner=owner,
                      workspace_identity=identity_of(workspace), network=NetworkPolicy.NONE,
                      sealed_paths=SEALED_PATHS)
    )  # fmt: skip
    try:
        runner = ClaudeCodeRunner(
            api_key=TOKEN,
            settings=ClaudeCodeSettings(executable=f"/workspace/{HERE}/claude-via-gateway",
                                        max_turns=2, expected_version=pinned),
        )  # fmt: skip
        result = runner.run(
            runtime, handle,
            AgentRequest(task=task("gateway"), instructions="Reply with OK.",
                         timeout_seconds=180, stall_timeout_seconds=120),
        )  # fmt: skip
    finally:
        runtime.stop(handle, owner=owner)
    log = helper / "requests.jsonl"
    requests = [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []
    directory = os.environ.get("ILMAR_LIVE_REPORT_DIR")
    if directory:
        write_report(Path(directory), "issue-111-gateway-compat", {
            "provenance": "recorded", "claude_code_version": pinned, "image": IMAGE,
            "at": datetime.now(UTC).isoformat(), "outcome": result.outcome.value,
            "summary": result.summary[:300], "requests": requests,
        }, [Secret("throwaway token", TOKEN)])  # fmt: skip
    assert requests, f"Claude Code sent nothing to the gateway: {result.summary}"
    assert all(r["auth_ok"] for r in requests), "a request without the throwaway token"
    assert result.outcome is AgentOutcome.COMPLETED, result.summary
    assert any(r["path"].split("?")[0] == "/v1/messages" for r in requests)


# -- C1: the same, through a separate gateway sidecar (ADR-0037 §1, P4) ----------------

SIDECAR_NAME = "ilmar-it-gateway-sidecar"
REAL_KEY = "sk-ant-c1-stand-in-real-key-" + "5" * 24  # what only the sidecar may hold


def test_pinned_claude_code_works_through_a_separate_gateway_sidecar(tmp_path: Path) -> None:
    """The real topology: Claude Code in a ``GATEWAY`` sandbox, the gateway in its
    own sidecar container on the sandbox's private network, and the stand-in
    provider on the host, outside both. The sandbox gets only the throwaway token;
    the provider must see the real key on every request."""
    pinned = version()
    assert pinned is not None
    host = host_ipv4()
    if host is None:
        pytest.fail("the stand-in provider needs a non-loopback host IPv4 address")
    remove_everything(SIDECAR_NAME)
    provider = Provider(host, tmp_path / "provider.jsonl", REAL_KEY)
    layout = WorkspaceLayout.under(tmp_path.resolve() / "state")
    layout.workspaces_root.mkdir(parents=True)
    workspace = make_workspace(layout, "sidecar")
    owner = ResourceOwner(instance_id="it", execution_id="gws", attempt=1, token="tok-gws")
    runtime = PodmanRuntime(
        layout=layout,
        gateway=GatewaySettings(
            image=os.environ.get("ILMAR_TEST_PODMAN_PROXY_IMAGE") or IMAGE,  # needs python3
            key=REAL_KEY, policy=POLICY,
            upstream=f"http://{host}:{provider.port}",
            allow_plain_http_upstream=True,
        ),
    )  # fmt: skip
    try:
        handle = runtime.start(
            ContainerSpec(image=IMAGE, host_workspace=workspace, name=SIDECAR_NAME, owner=owner,
                          workspace_identity=identity_of(workspace),
                          network=NetworkPolicy.GATEWAY, model_budget_micros=BUDGET,
                          sealed_paths=SEALED_PATHS,
                          lifetime_seconds=900)
        )  # fmt: skip
        try:
            access = runtime.model_access(handle)
            assert access is not None
            runner = ClaudeCodeRunner(
                api_key=REAL_KEY,  # replaced by the token: it must never reach the sandbox
                settings=ClaudeCodeSettings(max_turns=2, expected_version=pinned),
            )
            result = runner.run(
                runtime, handle,
                AgentRequest(task=task("gateway"), instructions="Reply with OK.",
                             timeout_seconds=180, stall_timeout_seconds=120),
            )  # fmt: skip
            gateway_log = podman("logs", f"{SIDECAR_NAME}-egress", check=False)
        finally:
            runtime.stop(handle, owner=owner)
    finally:
        provider.stop()
        remove_everything(SIDECAR_NAME)
    requests = provider.requests()
    directory = os.environ.get("ILMAR_LIVE_REPORT_DIR")
    if directory:
        write_report(Path(directory), "issue-111-gateway-sidecar", {
            "provenance": "recorded", "claude_code_version": pinned, "image": IMAGE,
            "at": datetime.now(UTC).isoformat(), "outcome": result.outcome.value,
            "summary": result.summary[:300], "requests": requests,
            "gateway_log": (gateway_log.stdout + gateway_log.stderr).splitlines()[-50:],
        }, [Secret("stand-in real key", REAL_KEY),
            Secret("throwaway token", access.token)])  # fmt: skip
    assert requests, f"nothing reached the provider through the sidecar: {result.summary}"
    assert all(r["auth_ok"] for r in requests), "a request reached the provider without the key"
    assert result.outcome is AgentOutcome.COMPLETED, result.summary
    assert any(str(r["path"]).split("?")[0] == "/v1/messages" for r in requests)
    assert REAL_KEY not in gateway_log.stdout + gateway_log.stderr
    assert access.token not in gateway_log.stdout + gateway_log.stderr
