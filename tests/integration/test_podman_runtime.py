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

import contextlib
import enum
import os
import secrets
import shutil
import socket
import subprocess
import threading
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
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
from ilmar.core.safe_fs import ensure_directory
from ilmar.domain.errors import ProviderError
from ilmar.domain.workspace import (
    ContainerHandle,
    ContainerSpec,
    DirectoryIdentity,
    ResourceLimits,
    ResourceOwner,
)
from ilmar.ports.container_runtime import ContainerRuntime

from contracts.container_runtime import NAME, OWNER, ContainerRuntimeContract

pytestmark = pytest.mark.integration

IMAGE = os.environ.get("ILMAR_TEST_PODMAN_IMAGE", "")
REQUIRED = os.environ.get("ILMAR_TEST_PODMAN_REQUIRED") == "1"
PODMAN = shutil.which("podman")
NAMES = (NAME, "ilmar-it-profile")
SENTINEL = "ilmar-sentinel-must-not-leak"


@pytest.fixture(autouse=True)
def _require_podman() -> None:
    missing = [
        what
        for what, present in (("podman", PODMAN), ("ILMAR_TEST_PODMAN_IMAGE", IMAGE))
        if not present
    ]
    if missing:
        message = f"needs {' and '.join(missing)}"
        if REQUIRED:
            pytest.fail(f"ILMAR_TEST_PODMAN_REQUIRED=1 but the suite {message}")
        pytest.skip(message)


def podman(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    assert PODMAN is not None
    return subprocess.run(  # noqa: S603 - test helper, fixed executable
        [PODMAN, *args], capture_output=True, text=True, check=check, timeout=120
    )


@pytest.fixture(autouse=True)
def _clean_containers(_require_podman: None) -> Iterator[None]:
    for name in NAMES:
        podman("rm", "--force", "--time", "0", name, check=False)
    yield
    for name in NAMES:
        podman("rm", "--force", "--time", "0", name, check=False)


@pytest.fixture
def layout(tmp_path: Path) -> WorkspaceLayout:
    layout = WorkspaceLayout.under(tmp_path.resolve() / "state")
    layout.workspaces_root.mkdir(parents=True)
    return layout


def make_workspace(layout: WorkspaceLayout, execution: str = "exec-1") -> Path:
    path = layout.agent_workspace(execution, 1)
    ensure_directory(path, root=layout.workspaces_root)
    return path


def identity_of(path: Path) -> DirectoryIdentity:
    info = path.lstat()
    return DirectoryIdentity(info.st_dev, info.st_ino)


def sh(script: str, *args: str) -> list[str]:
    return ["sh", "-c", script, "sh", *args]


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
def sandbox(
    layout: WorkspaceLayout, monkeypatch: pytest.MonkeyPatch
) -> Iterator[tuple[PodmanRuntime, ContainerHandle]]:
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


def inside(sandbox: tuple[PodmanRuntime, ContainerHandle], argv: Sequence[str]) -> str:
    runtime, handle = sandbox
    result = runtime.run(handle, argv, timeout_seconds=60)
    assert result.succeeded, (argv, result.stderr)
    return result.stdout


def test_no_capabilities_and_no_new_privileges(
    sandbox: tuple[PodmanRuntime, ContainerHandle],
) -> None:
    status = dict(
        line.split(":", 1) for line in inside(sandbox, ["cat", "/proc/self/status"]).splitlines()
    )
    for field_name in ("CapInh", "CapPrm", "CapEff", "CapBnd", "CapAmb"):
        assert int(status[field_name].strip(), 16) == 0, field_name
    assert status["NoNewPrivs"].strip() == "1"
    assert status["Seccomp"].strip() == "2"  # filter mode: the default profile is active


def test_runs_as_a_non_root_user(sandbox: tuple[PodmanRuntime, ContainerHandle]) -> None:
    assert inside(sandbox, ["id", "-u"]).strip() == "1000"
    assert inside(sandbox, ["id", "-g"]).strip() == "1000"


def test_root_filesystem_is_read_only_with_tmpfs_scratch(
    sandbox: tuple[PodmanRuntime, ContainerHandle],
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
    sandbox: tuple[PodmanRuntime, ContainerHandle],
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
    sandbox: tuple[PodmanRuntime, ContainerHandle],
) -> None:
    env = inside(sandbox, ["env"])
    assert "ILMAR_ALLOWED=yes" in env
    assert "HOME=/home/ilmar" in env
    assert SENTINEL not in env
    assert "proxy" not in env.lower()


def test_resource_limits_are_in_force(sandbox: tuple[PodmanRuntime, ContainerHandle]) -> None:
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
    sandbox: tuple[PodmanRuntime, ContainerHandle],
) -> None:
    assert inside(sandbox, ["ls", "/sys/class/net"]).split() == ["lo"]
    ipv4_routes = inside(sandbox, ["cat", "/proc/net/route"]).splitlines()[1:]
    assert ipv4_routes == []
    ipv6 = inside(sandbox, sh("cat /proc/net/if_inet6 2>/dev/null; true")).splitlines()
    assert all(line.split()[-1] == "lo" for line in ipv6)
    ipv6_routes = inside(sandbox, sh("cat /proc/net/ipv6_route 2>/dev/null; true"))
    assert all(line.split()[-1] == "lo" for line in ipv6_routes.splitlines())


def test_network_none_has_no_resolver(sandbox: tuple[PodmanRuntime, ContainerHandle]) -> None:
    resolv = inside(sandbox, sh("cat /etc/resolv.conf 2>/dev/null; true"))
    assert "nameserver" not in resolv


# -- network NONE: reachability with controls (ADR-0011 §9) ------------------------------


class Outcome(enum.Enum):
    PASS = "pass"  # the control succeeded and the sandbox attempt failed
    FAIL = "fail"  # the sandbox reached the target: isolation is broken
    INCONCLUSIVE = "inconclusive"  # the control failed: proves nothing


@dataclass
class EchoServer:
    """Answers every TCP connection with the per-run nonce."""

    host: str
    nonce: str
    family: socket.AddressFamily = socket.AF_INET

    def __post_init__(self) -> None:
        self._socket = socket.socket(self.family, socket.SOCK_STREAM)
        self._socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._socket.bind((self.host, 0))
        self._socket.listen(8)
        self._socket.settimeout(0.5)
        self.port = self._socket.getsockname()[1]
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self) -> None:
        while not self._stop.is_set():
            try:
                connection, _ = self._socket.accept()
            except (TimeoutError, OSError):
                continue
            with connection, contextlib.suppress(OSError):
                connection.sendall(f"{self.nonce}\n".encode())

    def close(self) -> None:
        self._stop.set()
        self._thread.join(timeout=5)
        self._socket.close()


def host_ipv4() -> str | None:
    """The host's primary non-loopback IPv4 address (no packet is sent)."""
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
        try:
            probe.connect(("192.0.2.1", 9))  # TEST-NET-1: route lookup only
        except OSError:
            return None
        address = str(probe.getsockname()[0])
    return None if address.startswith("127.") else address


def host_round_trip(host: str, port: int, nonce: str) -> bool:
    try:
        with socket.create_connection((host, port), timeout=3) as connection:
            return nonce in connection.recv(256).decode(errors="replace")
    except OSError:
        return False


def probe_from_sandbox(sandbox: tuple[PodmanRuntime, ContainerHandle], host: str, port: int) -> str:
    runtime, handle = sandbox
    result = runtime.run(
        handle, sh('nc -w 3 "$1" "$2" </dev/null', host, str(port)), timeout_seconds=30
    )
    return result.stdout


def reachability(
    sandbox: tuple[PodmanRuntime, ContainerHandle], server: EchoServer, target: str
) -> tuple[Outcome, str]:
    if not host_round_trip(target, server.port, server.nonce):
        return Outcome.INCONCLUSIVE, "host-side control did not get the nonce"
    seen = probe_from_sandbox(sandbox, target, server.port)
    if server.nonce in seen:
        return Outcome.FAIL, "the sandbox reached the target"
    return Outcome.PASS, "control answered; sandbox attempt failed"


def test_probe_tool_works_inside_the_sandbox(
    sandbox: tuple[PodmanRuntime, ContainerHandle],
) -> None:
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


@pytest.mark.parametrize("bind", ["0.0.0.0"])  # noqa: S104 - the test target must be reachable
def test_network_none_cannot_reach_a_host_ipv4_service(
    sandbox: tuple[PodmanRuntime, ContainerHandle], bind: str
) -> None:
    target = host_ipv4()
    assert target is not None, "inconclusive: the host has no non-loopback IPv4 address"
    server = EchoServer(bind, secrets.token_hex(8))
    try:
        outcome, reason = reachability(sandbox, server, target)
    finally:
        server.close()
    assert outcome is Outcome.PASS, f"{outcome.value}: {reason} ({target}:{server.port})"
