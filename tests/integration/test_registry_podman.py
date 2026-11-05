# SPDX-License-Identifier: Apache-2.0
"""Components built from a real ``ilmar.toml`` run real sandboxes (#10, ADR-0019).

Needs rootless Podman, ``ILMAR_TEST_PODMAN_IMAGE`` and
``ILMAR_TEST_PODMAN_PROXY_IMAGE``; skips without them unless
``ILMAR_TEST_PODMAN_REQUIRED=1``. GitHub is never contacted: the adapters
are only constructed.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import pytest

from ilmar.adapters.registry import build
from ilmar.config import load_config
from ilmar.core.components import Components
from ilmar.core.naming import container_name
from ilmar.core.safe_fs import ensure_directory
from ilmar.domain.errors import ConfigError
from ilmar.domain.workspace import ContainerSpec, NetworkPolicy, ResourceOwner

from integration.podman_support import (
    IMAGE,
    PODMAN,
    PROXY_IMAGE,
    identity_of,
    podman,
    remove_everything,
    require,
    sh,
)

pytestmark = pytest.mark.integration

INSTANCE = "it-registry"
SECRET = "ghp_registry-integration-never-shown"
ENVIRON = {"GITHUB_TOKEN": SECRET, "ANTHROPIC_API_KEY": "sk-ant-registry-integration"}
NAMES = tuple(container_name(INSTANCE, f"reg{n}", 1) for n in range(1, 4))


def toml(tmp: Path, *, proxy_image: str) -> Path:
    path = tmp / "ilmar.toml"
    runtime = f'{{ podman = "{PODMAN}", proxy_image = "{proxy_image}", memory_mb = 256 }}'
    path.write_text(
        f"""
[ilmar]
state_dir = "state"
instance_id = "{INSTANCE}"

[adapters.task_provider]
kind = "github"
options = {{ repository = "owner/repo", token_env = "GITHUB_TOKEN" }}

[adapters.source_control]
kind = "github"
options = {{ repository = "owner/repo", token_env = "GITHUB_TOKEN" }}

[adapters.container_runtime]
kind = "podman"
options = {runtime}

[adapters.agent_runner]
kind = "claude-code"
options = {{ image = "{IMAGE}", api_key_env = "ANTHROPIC_API_KEY" }}

[adapters.execution_store]
kind = "sqlite"

[verification]
image = "{IMAGE}"
[[verification.commands]]
name = "tests"
argv = ["true"]
"""
    )
    return path


@pytest.fixture(autouse=True)
def _require() -> Iterator[None]:
    require(("podman", PODMAN), ("ILMAR_TEST_PODMAN_IMAGE", IMAGE), ("proxy image", PROXY_IMAGE))
    remove_everything(*NAMES)
    yield
    remove_everything(*NAMES)


@pytest.fixture
def components(tmp_path: Path) -> Iterator[Components]:
    with build(load_config(toml(tmp_path, proxy_image=PROXY_IMAGE)), environ=ENVIRON) as built:
        built.layout.workspaces_root.mkdir(parents=True)
        yield built


def spec(components: Components, execution: str, *, agent: bool) -> ContainerSpec:
    workspace = components.layout.agent_workspace(execution, 1)
    ensure_directory(workspace, root=components.layout.workspaces_root)
    sandbox = components.agent_sandbox
    return ContainerSpec(
        image=sandbox.image if agent else components.verification.image,
        host_workspace=workspace,
        name=container_name(components.instance_id, execution, 1),
        owner=ResourceOwner(INSTANCE, execution, 1, token=f"tok-{execution}"),
        workspace_identity=identity_of(workspace),
        network=sandbox.network if agent else NetworkPolicy.NONE,
        allowed_hosts=sandbox.allowed_hosts if agent else (),
        limits=sandbox.limits if agent else components.verification.limits,
    )


def test_a_built_runtime_runs_a_hardened_sandbox_with_configured_limits(
    components: Components,
) -> None:
    runtime = components.container_runtime
    handle = runtime.start(spec(components, "reg1", agent=False))
    try:
        probe = runtime.run(
            handle,
            sh('[ "$(id -u)" != 0 ] && [ "$(ls /sys/class/net)" = lo ] && ! touch /probe'),
            timeout_seconds=60,
        )
        assert probe.exit_code == 0, probe.stderr
        details = json.loads(podman("inspect", handle.id).stdout)[0]
        assert details["HostConfig"]["Memory"] == 256 * 1024 * 1024  # from ilmar.toml
        assert details["HostConfig"]["CapDrop"]  # everything dropped (ADR-0011)
    finally:
        runtime.stop(handle, owner=ResourceOwner(INSTANCE, "reg1", 1, token="tok-reg1"))


def test_the_agent_sandbox_from_configuration_gets_only_its_endpoint(
    components: Components,
) -> None:
    runtime = components.container_runtime
    handle = runtime.start(spec(components, "reg2", agent=True))
    try:
        probe = runtime.run(handle, sh('[ -n "$HTTPS_PROXY" ]'), timeout_seconds=60)
        assert probe.exit_code == 0, "the agent reaches the outside only through the proxy"
        assert components.agent_sandbox.allowed_hosts == ("api.anthropic.com",)
    finally:
        runtime.stop(handle, owner=ResourceOwner(INSTANCE, "reg2", 1, token="tok-reg2"))
    leftovers = podman("ps", "-a", "--format", "{{.Names}}").stdout.split()
    assert not [name for name in leftovers if name.startswith(NAMES[1])]


def test_the_store_built_from_configuration_persists_across_builds(tmp_path: Path) -> None:
    from ilmar.core.state_machine import ExecutionStateMachine

    from helpers import at, task

    path = toml(tmp_path, proxy_image=PROXY_IMAGE)
    execution, transition = ExecutionStateMachine(max_attempts=3).start(
        task("1").ref, execution_id="reg3", now=at(0)
    )
    with build(load_config(path), environ=ENVIRON) as first:
        first.execution_store.create(execution, transition)
    with build(load_config(path), environ=ENVIRON) as second:
        assert second.execution_store.get("reg3") == execution


def test_a_wrong_podman_binary_fails_closed_without_leaking(tmp_path: Path) -> None:
    path = toml(tmp_path, proxy_image=PROXY_IMAGE)
    path.write_text(path.read_text().replace(f'podman = "{PODMAN}"', 'podman = "/nonexistent"'))
    with pytest.raises(ConfigError) as info:
        build(load_config(path), environ=ENVIRON)
    assert "not found" in str(info.value)
    assert SECRET not in str(info.value)
