# SPDX-License-Identifier: Apache-2.0
"""Opt-in tests against real, rootless Podman (``pytest -m integration``).

Needs ``podman`` on ``PATH`` and a local image with a POSIX shell, ``sleep``,
``id``, ``touch`` and a BusyBox-style ``nc`` (Alpine works); the adapter never
pulls (``--pull=never``):

    podman pull docker.io/library/alpine:3.20
    ILMAR_TEST_PODMAN_IMAGE=docker.io/library/alpine:3.20 \\
        pytest -m integration tests/integration/test_podman_runtime.py

The tests skip when either is missing. CI sets ``ILMAR_TEST_PODMAN_REQUIRED=1``,
which turns that skip into a failure, so the suite can never pass by not
running.

Network reachability cases follow ADR-0011 §9. Each has a three-way outcome:
the control (a host-side round trip to the same address, protocol and port,
answered with a per-run nonce) must succeed, or the case is *inconclusive*,
and an inconclusive required case fails. A second control proves that the
probe tool itself works inside the sandbox.
"""

from __future__ import annotations

import secrets
from collections.abc import Iterator, Sequence
from pathlib import Path

import pytest

from ilmar.adapters.podman import PodmanRuntime
from ilmar.adapters.podman.runtime import (
    LABEL_ATTEMPT,
    LABEL_EXECUTION,
    LABEL_INSTANCE,
    LABEL_TOKEN,
)
from ilmar.core.layout import WorkspaceLayout
from ilmar.domain.errors import ProviderError
from ilmar.domain.workspace import (
    ContainerHandle,
    ContainerSpec,
    DirectoryIdentity,
    NetworkPolicy,
    ResourceLimits,
    ResourceOwner,
)
from ilmar.ports.container_runtime import ContainerRuntime

from contracts.container_runtime import NAME, OWNER, ContainerRuntimeContract
from integration.podman_support import (
    IMAGE,
    PODMAN,
    EchoServer,
    Sandbox,
    classify,
    expect_isolated,
    host_ipv4,
    identity_of,
    inside,
    make_workspace,
    podman,
    remove_everything,
    require,
    sandbox_tcp,
    sh,
    tcp_control,
)

pytestmark = pytest.mark.integration

NAMES = (NAME, "ilmar-it-profile", "ilmar-it-open")
SENTINEL = "ilmar-sentinel-must-not-leak"


@pytest.fixture(autouse=True)
def _require_podman() -> None:
    require(("podman", PODMAN), ("ILMAR_TEST_PODMAN_IMAGE", IMAGE))


@pytest.fixture(autouse=True)
def _clean_containers(_require_podman: None) -> Iterator[None]:
    remove_everything(*NAMES)
    yield
    remove_everything(*NAMES)


@pytest.fixture
def layout(tmp_path: Path) -> WorkspaceLayout:
    layout = WorkspaceLayout.under(tmp_path.resolve() / "state")
    layout.workspaces_root.mkdir(parents=True)
    return layout


# -- the shared contract -------------------------------------------------------


class TestContractOnRealPodman(ContainerRuntimeContract):
    _layout: WorkspaceLayout

    @pytest.fixture(autouse=True)
    def _setup(self, layout: WorkspaceLayout) -> None:
        self._layout = layout

    def make_runtime(self) -> ContainerRuntime:
        return PodmanRuntime(layout=self._layout)

    def make_workspace(self, tmp_path: Path) -> Path:
        return make_workspace(self._layout)

    def make_spec(
        self, workspace: Path, name: str, owner: ResourceOwner, identity: DirectoryIdentity
    ) -> ContainerSpec:
        return ContainerSpec(
            image=IMAGE,
            host_workspace=workspace,
            name=name,
            owner=owner,
            workspace_identity=identity,
        )

    def create_foreign(
        self, runtime: ContainerRuntime, spec: ContainerSpec, owner: ResourceOwner | None
    ) -> ContainerHandle:
        labels: list[str] = []
        if owner is not None:
            for key, value in (
                (LABEL_INSTANCE, owner.instance_id),
                (LABEL_EXECUTION, owner.execution_id),
                (LABEL_ATTEMPT, str(owner.attempt)),
                (LABEL_TOKEN, owner.token),
            ):
                labels += ["--label", f"{key}={value}"]
        done = podman(
            "run", "--detach", "--pull=never", "--network=none", "--name", spec.name,
            *labels, IMAGE, "sleep", "600",
        )  # fmt: skip
        return ContainerHandle(runtime=runtime.name, id=done.stdout.strip(), name=spec.name)

    def echo(self, text: str) -> Sequence[str]:
        return sh('printf "%s\\n" "$1"', text)

    def exit_with(self, code: int) -> Sequence[str]:
        return sh(f"exit {code}")

    def sleep(self, seconds: float) -> Sequence[str]:
        return ["sleep", str(int(seconds))]

    def write_file(self, relative_path: str, content: str) -> Sequence[str]:
        return sh('printf "%s" "$2" > "$1"', relative_path, content)


# -- the ADR-0011 profile, observed from inside -----------------------------------


@pytest.fixture
def sandbox(layout: WorkspaceLayout, monkeypatch: pytest.MonkeyPatch) -> Iterator[Sandbox]:
    # Secrets and proxies in the coordinator's environment must never leak in.
    monkeypatch.setenv("GITHUB_TOKEN", SENTINEL)
    monkeypatch.setenv("HTTPS_PROXY", f"http://{SENTINEL}:3128")
    monkeypatch.setenv("https_proxy", f"http://{SENTINEL}:3128")
    runtime = PodmanRuntime(layout=layout)
    workspace = make_workspace(layout)
    spec = ContainerSpec(
        image=IMAGE,
        host_workspace=workspace,
        name="ilmar-it-profile",
        owner=OWNER,
        workspace_identity=identity_of(workspace),
        env={"ILMAR_ALLOWED": "yes"},
        limits=ResourceLimits(cpus=1.0, memory_mb=256, max_processes=128),
    )
    handle = runtime.start(spec)
    yield runtime, handle
    runtime.stop(handle, owner=OWNER)


def test_no_capabilities_and_no_new_privileges(
    sandbox: Sandbox,
) -> None:
    status = dict(
        line.split(":", 1) for line in inside(sandbox, ["cat", "/proc/self/status"]).splitlines()
    )
    for field_name in ("CapInh", "CapPrm", "CapEff", "CapBnd", "CapAmb"):
        assert int(status[field_name].strip(), 16) == 0, field_name
    assert status["NoNewPrivs"].strip() == "1"
    assert status["Seccomp"].strip() == "2"  # filter mode: the default profile is active


def test_runs_as_a_non_root_user(sandbox: Sandbox) -> None:
    assert inside(sandbox, ["id", "-u"]).strip() == "1000"
    assert inside(sandbox, ["id", "-g"]).strip() == "1000"


def test_root_filesystem_is_read_only_with_tmpfs_scratch(
    sandbox: Sandbox,
) -> None:
    runtime, handle = sandbox
    for path in ("/probe", "/etc/probe", "/usr/probe", "/var/tmp/probe", "/run/probe"):
        assert not runtime.run(handle, ["touch", path], timeout_seconds=30).succeeded, path
    inside(sandbox, ["touch", "/tmp/ok", "/home/ilmar/ok"])
    mounts = inside(sandbox, ["cat", "/proc/self/mountinfo"]).splitlines()
    for target in ("/tmp", "/home/ilmar"):
        (line,) = [m for m in mounts if m.split()[4] == target]
        assert "tmpfs" in line.split(" - ")[1]
        assert "nosuid" in line
        assert "nodev" in line


def test_only_the_workspace_directory_comes_from_the_host(
    sandbox: Sandbox,
) -> None:
    virtual = {"proc", "sysfs", "tmpfs", "devpts", "mqueue", "cgroup", "cgroup2", "devtmpfs"}
    directories = []
    for line in inside(sandbox, ["cat", "/proc/self/mountinfo"]).splitlines():
        fields, _, rest = line.partition(" - ")
        mount_point, fstype = fields.split()[4], rest.split()[0]
        if mount_point == "/" or fstype in virtual or fstype == "overlay":
            continue
        is_dir = inside(sandbox, sh('[ -d "$1" ] && echo dir || echo file', mount_point))
        if is_dir.strip() == "dir":
            directories.append(mount_point)
    # Podman also bind-mounts generated *files* (hostname, passwd, the init binary).
    assert directories == ["/workspace"]


def test_environment_is_only_what_was_allowed(
    sandbox: Sandbox,
) -> None:
    env = inside(sandbox, ["env"])
    assert "ILMAR_ALLOWED=yes" in env
    assert "HOME=/home/ilmar" in env
    assert SENTINEL not in env
    assert "proxy" not in env.lower()


def test_resource_limits_are_in_force(sandbox: Sandbox) -> None:
    assert inside(sandbox, ["cat", "/sys/fs/cgroup/pids.max"]).strip() == "128"
    assert inside(sandbox, ["cat", "/sys/fs/cgroup/memory.max"]).strip() == str(256 * 2**20)
    assert inside(sandbox, ["cat", "/sys/fs/cgroup/cpu.max"]).split()[0] != "max"


def test_missing_images_are_never_pulled(layout: WorkspaceLayout) -> None:
    workspace = make_workspace(layout)
    spec = ContainerSpec(
        image="localhost/ilmar-does-not-exist:0",
        host_workspace=workspace,
        name="ilmar-it-profile",
        owner=OWNER,
        workspace_identity=identity_of(workspace),
    )
    with pytest.raises(ProviderError):
        PodmanRuntime(layout=layout).start(spec)


# -- network NONE: structure (ADR-0011 §9) --------------------------------------------


def test_network_none_has_only_loopback_and_no_routes(
    sandbox: Sandbox,
) -> None:
    assert inside(sandbox, ["ls", "/sys/class/net"]).split() == ["lo"]
    ipv4_routes = inside(sandbox, ["cat", "/proc/net/route"]).splitlines()[1:]
    assert ipv4_routes == []
    ipv6 = inside(sandbox, sh("cat /proc/net/if_inet6 2>/dev/null; true")).splitlines()
    assert all(line.split()[-1] == "lo" for line in ipv6)
    ipv6_routes = inside(sandbox, sh("cat /proc/net/ipv6_route 2>/dev/null; true"))
    assert all(line.split()[-1] == "lo" for line in ipv6_routes.splitlines())


def test_network_none_has_no_resolver(sandbox: Sandbox) -> None:
    resolv = inside(sandbox, sh("cat /etc/resolv.conf 2>/dev/null; true"))
    assert "nameserver" not in resolv


# -- network NONE: reachability with controls (ADR-0011 §9) ------------------------------


def test_probe_tool_works_inside_the_sandbox(sandbox: Sandbox) -> None:
    """Control for the probe itself: an in-sandbox listener on loopback is reachable."""
    nonce = secrets.token_hex(8)
    out = inside(
        sandbox,
        sh(
            '(printf "%s\\n" "$1" | nc -l -p 18080 >/dev/null 2>&1 &) ; sleep 1; '
            "nc -w 3 127.0.0.1 18080 </dev/null",
            nonce,
        ),
    )
    assert nonce in out, "nc in the image cannot make a loopback round trip"


def test_network_none_cannot_reach_a_host_ipv4_service(sandbox: Sandbox) -> None:
    target = host_ipv4()
    assert target is not None, "inconclusive: the host has no non-loopback IPv4 address"
    server = EchoServer("0.0.0.0", 0, secrets.token_hex(8))  # noqa: S104
    try:
        control = tcp_control(target, server.port, server.nonce)
        outcome, reason = classify(control, sandbox_tcp(sandbox, target, server.port), server.nonce)
    finally:
        server.close()
    expect_isolated(outcome, reason, f"NONE tcp {target}:{server.port}", required=True)


def test_the_same_probe_reaches_the_target_without_isolation(layout: WorkspaceLayout) -> None:
    """Counter-control: the reachability case is not vacuous.

    With ``UNRESTRICTED`` the identical probe must reach the identical target,
    so the ``NONE`` result above is due to isolation, not a broken probe.
    """
    target = host_ipv4()
    assert target is not None, "inconclusive: the host has no non-loopback IPv4 address"
    runtime = PodmanRuntime(layout=layout, allow_unrestricted_network=True)
    workspace = make_workspace(layout, "exec-open")
    handle = runtime.start(
        ContainerSpec(
            image=IMAGE,
            host_workspace=workspace,
            name="ilmar-it-open",
            owner=OWNER,
            workspace_identity=identity_of(workspace),
            network=NetworkPolicy.UNRESTRICTED,
        )
    )
    server = EchoServer("0.0.0.0", 0, secrets.token_hex(8))  # noqa: S104
    try:
        assert tcp_control(target, server.port, server.nonce)
        seen = sandbox_tcp((runtime, handle), target, server.port)
        assert server.nonce in seen, "an unisolated sandbox should reach the host target"
    finally:
        server.close()
        runtime.stop(handle, owner=OWNER)
