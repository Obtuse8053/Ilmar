# SPDX-License-Identifier: Apache-2.0
"""The model gateway as a separate sidecar, on real rootless Podman (#111 P4).

ADR-0037 §1. A ``GATEWAY`` sandbox sits alone with its gateway sidecar on a
private ``--internal`` network. The stand-in provider runs on the host's own
IPv4 address, outside both containers. The tests show that:

* the sandbox reaches the provider only through the sidecar, with the
  throwaway token, and the provider sees the real key instead;
* a wrong or missing token, and every other path, is refused by the sidecar
  and never reaches the provider;
* the sandbox cannot reach the provider (or anything else) directly;
* the real key appears in neither container's definition nor in the
  sandbox, and the host copy is gone once the sidecar has read it;
* stopping the sandbox removes the sidecar and the network.

No Claude Code here (see ``test_claude_code_gateway_compat.py``). Opt-in:

    ILMAR_TEST_PODMAN_IMAGE=docker.io/library/alpine:3.20 \\
    ILMAR_TEST_PODMAN_PROXY_IMAGE=docker.io/library/python:3.12-alpine \\
        pytest -m integration tests/integration/test_podman_gateway.py -rs
"""

from __future__ import annotations

import secrets
from collections.abc import Iterator
from pathlib import Path

import pytest

from ilmar.adapters.podman import GatewaySettings, PodmanRuntime
from ilmar.core.layout import WorkspaceLayout
from ilmar.domain.workspace import ContainerSpec, NetworkPolicy, ResourceOwner

from integration.gateway_support import Provider
from integration.podman_support import (
    IMAGE,
    PODMAN,
    PROXY_IMAGE,
    Sandbox,
    attempt,
    host_ipv4,
    identity_of,
    make_workspace,
    podman,
    remove_everything,
    require,
    sh,
)

pytestmark = pytest.mark.integration

NAME = "ilmar-it-gateway"
OWNER = ResourceOwner(instance_id="it", execution_id="gateway", attempt=1, token="tok-gateway")
REAL_KEY = "sk-ant-it-real-key-" + secrets.token_hex(16)
ASK = sh(
    'wget -q -O - -T 10 --header "x-api-key: $1" --header "content-type: application/json" '
    '--post-data \'{"model":"m","max_tokens":1,"stream":false,"messages":[]}\' "$2"; '
    'echo " exit=$?"',
)


@pytest.fixture(scope="module")
def world(tmp_path_factory: pytest.TempPathFactory) -> Iterator[tuple[Sandbox, Provider, str]]:
    require(("podman", PODMAN), ("ILMAR_TEST_PODMAN_IMAGE", IMAGE))
    require(("ILMAR_TEST_PODMAN_PROXY_IMAGE", PROXY_IMAGE))
    host = host_ipv4()
    if host is None:
        pytest.fail("the stand-in provider needs a non-loopback host IPv4 address")
    root = tmp_path_factory.mktemp("gateway")
    provider = Provider(host, root / "provider.jsonl", REAL_KEY)
    layout = WorkspaceLayout.under(root.resolve() / "state")
    layout.workspaces_root.mkdir(parents=True)
    workspace = make_workspace(layout, "gateway")
    runtime = PodmanRuntime(
        layout=layout,
        gateway=GatewaySettings(
            image=str(PROXY_IMAGE), key=REAL_KEY, upstream=f"http://{host}:{provider.port}",
            allow_plain_http_upstream=True,
        ),
    )  # fmt: skip
    remove_everything(NAME)
    handle = runtime.start(
        ContainerSpec(image=str(IMAGE), host_workspace=workspace, name=NAME, owner=OWNER,
                      workspace_identity=identity_of(workspace),
                      network=NetworkPolicy.GATEWAY, lifetime_seconds=900)
    )  # fmt: skip
    try:
        yield (runtime, handle), provider, host
    finally:
        try:
            runtime.stop(handle, owner=OWNER)
        finally:
            remove_everything(NAME)
            provider.stop()


def route(sandbox: Sandbox) -> tuple[str, str]:
    runtime, handle = sandbox
    access = runtime.model_access(handle)
    assert access is not None
    return access.base_url, access.token


def test_the_sandbox_reaches_the_provider_only_with_its_token_and_the_real_key(
    world: tuple[Sandbox, Provider, str],
) -> None:
    sandbox, provider, _ = world
    base, token = route(sandbox)
    before = len(provider.requests())
    out = attempt(sandbox, [*ASK, token, f"{base}/v1/messages?beta=true"])
    assert out.endswith("exit=0\n"), out
    assert '"type": "message"' in out or '"type":"message"' in out
    seen = provider.requests()[before:]
    assert len(seen) == 1
    assert seen[0]["auth_ok"] is True  # the provider saw the real key, nothing else


@pytest.mark.parametrize(
    ("credential", "path"),
    [
        ("wrong-token-0000000000000000000000000", "/v1/messages"),
        (REAL_KEY, "/v1/messages"),  # not even the real key opens the sidecar
        ("", "/v1/messages"),
        (None, "/v1/files"),
        (None, "/v1/messages/batches"),
    ],
    ids=["wrong-token", "real-key", "empty", "files", "batches"],  # no value in a test ID
)
def test_everything_else_is_refused_by_the_sidecar(
    world: tuple[Sandbox, Provider, str], credential: str | None, path: str
) -> None:
    sandbox, provider, _ = world
    base, token = route(sandbox)
    before = len(provider.requests())
    out = attempt(sandbox, [*ASK, token if credential is None else credential, base + path])
    assert not out.endswith("exit=0\n"), out
    assert provider.requests()[before:] == []


def test_the_sandbox_cannot_reach_the_provider_or_anything_else_directly(
    world: tuple[Sandbox, Provider, str],
) -> None:
    sandbox, provider, host = world
    _, token = route(sandbox)
    before = len(provider.requests())
    for target in (f"http://{host}:{provider.port}/v1/messages", "http://1.1.1.1/"):
        out = attempt(sandbox, [*ASK, token, target])
        assert not out.endswith("exit=0\n"), (target, out)
    assert provider.requests()[before:] == []
    resolve = attempt(sandbox, sh("nslookup api.anthropic.com 2>&1; echo exit=$?"))
    assert "exit=0" not in resolve


def test_the_real_key_is_in_neither_container_nor_the_sandbox(
    world: tuple[Sandbox, Provider, str],
) -> None:
    sandbox, _, _ = world
    for name in (NAME, f"{NAME}-egress"):
        assert REAL_KEY not in podman("inspect", name).stdout
    shown = attempt(sandbox, sh("env; cat /proc/1/environ 2>/dev/null; ls -la / /run 2>&1"))
    assert REAL_KEY not in shown
    assert not list(Path("/tmp").glob("ilmar-gateway-*"))  # the host copy is gone


def test_stopping_the_sandbox_removes_the_sidecar_and_the_network(
    world: tuple[Sandbox, Provider, str],
) -> None:
    (runtime, handle), _, _ = world
    runtime.stop(handle, owner=OWNER)
    assert runtime.model_access(handle) is None
    assert podman("container", "exists", f"{NAME}-egress", check=False).returncode == 1
    assert podman("network", "exists", f"{NAME}-net", check=False).returncode == 1
