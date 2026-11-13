# SPDX-License-Identifier: Apache-2.0
"""No persistent copy of the model key after a crash mid-handoff (#120 review).

ADR-0037 §1. The coordinator process is SIGKILLed while it starts a
``GATEWAY`` sandbox (``gateway_crash_child.py``): before the key handoff,
halfway through it, and just after it. In every case, the key must not be
found anywhere it could persist:

* the host's temporary directory and Ilmar's state directory;
* ``podman inspect`` and ``podman logs`` of the gateway sidecar and the sandbox;
* the sidecar's container storage (its ``userdata``, which holds its
  configuration, logs and exec records) and its writable layer;
* the sidecar's exported file system.

The FIFO the key passes through lives in the sidecar's tmpfs and holds no
data: once the gateway has read it, the FIFO is gone too.

Opt-in, like the other Podman tests:

    ILMAR_TEST_PODMAN_IMAGE=docker.io/library/alpine:3.20 \\
    ILMAR_TEST_PODMAN_PROXY_IMAGE=docker.io/library/python:3.12-alpine \\
        pytest -m integration tests/integration/test_podman_gateway_crash.py -rs
"""

from __future__ import annotations

import os
import secrets
import signal
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

from integration.podman_support import (
    IMAGE,
    PODMAN,
    PROXY_IMAGE,
    podman,
    remove_everything,
    require,
)

pytestmark = pytest.mark.integration

ROOT = Path(__file__).resolve().parents[2]
CHILD = Path(__file__).resolve().parent / "gateway_crash_child.py"


def contains(directory: Path, needle: str) -> list[str]:
    """Files under ``directory`` holding ``needle``, read as the rootless user's
    namespace sees them (``podman unshare``): container storage is owned by
    mapped IDs."""
    if not directory.exists():
        return []
    found = podman("unshare", "grep", "-rlaF", "--", needle, str(directory), check=False)
    return [line for line in found.stdout.splitlines() if line]


def host_files_holding(directory: Path, needle: bytes) -> list[Path]:
    """Regular files under ``directory`` (not following links) that hold ``needle``."""
    found = []
    for root, _dirs, files in os.walk(directory):
        for name in files:
            path = Path(root) / name
            try:
                if path.is_symlink() or not path.is_file() or path.stat().st_size > 64 << 20:
                    continue
                if needle in path.read_bytes():
                    found.append(path)
            except OSError:
                continue  # another user's file, or gone: not ours to have written
    return found


@pytest.mark.parametrize("stage", ["awaiting", "partial", "handed"])
def test_a_crash_during_the_handoff_leaves_no_copy_of_the_key(tmp_path: Path, stage: str) -> None:
    require(("podman", PODMAN), ("ILMAR_TEST_PODMAN_IMAGE", IMAGE))
    require(("ILMAR_TEST_PODMAN_PROXY_IMAGE", PROXY_IMAGE))
    name = f"ilmar-it-gwcrash-{stage}"
    key = "sk-ant-crash-test-" + secrets.token_hex(24)
    remove_everything(name)
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(str(ROOT / p) for p in ("src", "tests", "scripts"))
    env["ILMAR_TEST_GATEWAY_KEY"] = key
    state = tmp_path / "state"
    try:
        child = subprocess.run(  # noqa: S603 - fixed argv: this interpreter and a test file
            [sys.executable, str(CHILD), str(state), name, stage],
            cwd=ROOT, env=env, capture_output=True, text=True, timeout=180, check=False,
        )  # fmt: skip
        assert child.returncode == -signal.SIGKILL, child.stdout + child.stderr
        sidecar = f"{name}-egress"
        assert podman("container", "exists", sidecar, check=False).returncode == 0
        # What podman shows and keeps about both containers.
        for container in (sidecar, name):
            for shown in (podman("inspect", container, check=False),
                          podman("logs", container, check=False)):  # fmt: skip
                assert key not in shown.stdout + shown.stderr, container
        # Container storage: configuration, logs, exec records and the writable layer.
        sidecar_id = podman("inspect", "--format", "{{.Id}}", sidecar).stdout.strip()
        upper = podman("inspect", "--format", "{{.GraphDriver.Data.UpperDir}}", sidecar)
        storage = Path(podman("info", "--format", "{{.Store.GraphRoot}}").stdout.strip())
        for directory in (
            storage / "overlay-containers" / sidecar_id,
            Path(upper.stdout.strip()) if upper.stdout.strip() else storage / "missing",
        ):
            assert contains(directory, key) == [], directory
        exported = subprocess.run(  # noqa: S603
            [str(PODMAN), "export", sidecar], capture_output=True, timeout=120, check=False
        )
        assert exported.returncode == 0
        assert key.encode() not in exported.stdout
        # The host: its temporary directory and Ilmar's state.
        for directory in (Path(tempfile.gettempdir()), state):
            assert host_files_holding(directory, key.encode()) == [], directory
        # The FIFO: still waiting before the handoff, gone once the key was read. (After
        # ``partial`` the orphaned writer may still finish, so either is possible.)
        listing = podman("exec", sidecar, "ls", "/run/ilmar-gateway", check=False).stdout
        if stage != "partial":
            assert ("key" in listing.split()) is (stage == "awaiting")
    finally:
        remove_everything(name)
