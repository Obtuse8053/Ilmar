# SPDX-License-Identifier: Apache-2.0
"""Shared helpers for the real-Podman integration tests (ADR-0011 §9).

Reachability cases have three outcomes. Before each sandbox attempt, a
*control* makes a host-side round trip to the same address, protocol and port,
and the target must answer with a per-run nonce:

* **pass**: the control succeeded and the sandbox attempt did not get the nonce;
* **fail**: the sandbox got the nonce, so isolation is broken;
* **inconclusive**: the control failed, which proves nothing. A *required*
  case fails on this; an *optional* case (external targets) is skipped with
  the reason, so it is always reported.
"""

from __future__ import annotations

import contextlib
import enum
import os
import shutil
import socket
import subprocess
import threading
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from ilmar.adapters.podman import PodmanRuntime
from ilmar.core.layout import WorkspaceLayout
from ilmar.core.safe_fs import ensure_directory
from ilmar.domain.workspace import ContainerHandle, DirectoryIdentity

IMAGE = os.environ.get("ILMAR_TEST_PODMAN_IMAGE", "")
PROXY_IMAGE = os.environ.get("ILMAR_TEST_PODMAN_PROXY_IMAGE", "")
REQUIRED = os.environ.get("ILMAR_TEST_PODMAN_REQUIRED") == "1"
PODMAN = shutil.which("podman")

Sandbox = tuple[PodmanRuntime, ContainerHandle]


def require(*needs: tuple[str, object]) -> None:
    missing = [what for what, present in needs if not present]
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


def remove_everything(*names: str) -> None:
    """Remove test containers and their egress sidecars and networks by name."""
    for name in names:
        for container in (name, f"{name}-egress"):
            podman("rm", "--force", "--time", "0", container, check=False)
        podman("network", "rm", "--force", f"{name}-net", check=False)


def make_workspace(layout: WorkspaceLayout, execution: str = "exec-1") -> Path:
    path = layout.agent_workspace(execution, 1)
    ensure_directory(path, root=layout.workspaces_root)
    return path


def identity_of(path: Path) -> DirectoryIdentity:
    info = path.lstat()
    return DirectoryIdentity(info.st_dev, info.st_ino)


def sh(script: str, *args: str) -> list[str]:
    return ["sh", "-c", script, "sh", *args]


def inside(sandbox: Sandbox, argv: Sequence[str], *, timeout: float = 60) -> str:
    runtime, handle = sandbox
    result = runtime.run(handle, argv, timeout_seconds=timeout)
    assert result.succeeded, (argv, result.stderr)
    return result.stdout


def attempt(sandbox: Sandbox, argv: Sequence[str], *, timeout: float = 30) -> str:
    """Run a probe that is expected to fail; return whatever it printed."""
    runtime, handle = sandbox
    return runtime.run(handle, argv, timeout_seconds=timeout).stdout


# -- targets ----------------------------------------------------------------------


class Outcome(enum.Enum):
    PASS = "pass"
    FAIL = "fail"
    INCONCLUSIVE = "inconclusive"


@dataclass
class EchoServer:
    """Answers every TCP connection with the nonce, or every UDP datagram with it."""

    host: str
    port: int
    nonce: str
    udp: bool = False
    _stop: threading.Event = field(default_factory=threading.Event)

    def __post_init__(self) -> None:
        kind = socket.SOCK_DGRAM if self.udp else socket.SOCK_STREAM
        family = socket.AF_INET6 if ":" in self.host else socket.AF_INET
        self._socket = socket.socket(family, kind)
        self._socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._socket.bind((self.host, self.port))
        if not self.udp:
            self._socket.listen(16)
        self._socket.settimeout(0.3)
        self.port = self._socket.getsockname()[1]
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self) -> None:
        reply = f"{self.nonce}\n".encode()
        while not self._stop.is_set():
            try:
                if self.udp:
                    _, peer = self._socket.recvfrom(2048)
                    self._socket.sendto(reply, peer)
                else:
                    connection, _ = self._socket.accept()
                    with connection, contextlib.suppress(OSError):
                        connection.sendall(reply)
            except (TimeoutError, OSError):
                continue

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


def tcp_control(host: str, port: int, nonce: str) -> bool:
    try:
        with socket.create_connection((host, port), timeout=3) as connection:
            return nonce in connection.recv(256).decode(errors="replace")
    except OSError:
        return False


def udp_control(host: str, port: int, nonce: str) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
        probe.settimeout(3)
        try:
            probe.sendto(b"probe\n", (host, port))
            return nonce in probe.recv(256).decode(errors="replace")
        except OSError:
            return False


def sandbox_tcp(sandbox: Sandbox, host: str, port: int) -> str:
    return attempt(sandbox, sh('nc -w 3 "$1" "$2" </dev/null', host, str(port)))


def sandbox_udp(sandbox: Sandbox, host: str, port: int) -> str:
    return attempt(sandbox, sh('echo probe | nc -u -w 3 "$1" "$2"', host, str(port)))


def classify(control_ok: bool, seen: str, nonce: str) -> tuple[Outcome, str]:
    if not control_ok:
        return Outcome.INCONCLUSIVE, "the host-side control did not get the nonce"
    if nonce in seen:
        return Outcome.FAIL, "the sandbox reached the target"
    return Outcome.PASS, "control answered; sandbox attempt failed"


def expect_isolated(outcome: Outcome, reason: str, case: str, *, required: bool) -> None:
    if outcome is Outcome.FAIL:
        pytest.fail(f"{case}: FAIL: {reason}")
    if outcome is Outcome.INCONCLUSIVE:
        if required:
            pytest.fail(f"{case}: required case inconclusive: {reason}")
        pytest.skip(f"{case}: inconclusive (optional): {reason}")
