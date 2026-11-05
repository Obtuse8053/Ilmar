# SPDX-License-Identifier: Apache-2.0
"""Where a per-command credential goes on real Podman (review of PR #50, ADR-0023).

The model key reaches the agent as the environment of one ``podman exec``.
These tests put a unique marker value there and search everything Podman
keeps on disk for it (its state database, the containers' bundle
directories, the temporary directory):
* while the command runs;
* after it ends;
* after the coordinator was killed mid-command and the sandbox was then
  removed, as recovery does.

They also check that the descriptor carrying the value is not visible to
processes inside the container. Image layers are not searched, because they
are not written at run time.
"""

from __future__ import annotations

import json
import os
import secrets as random
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

from ilmar.adapters.podman import PodmanRuntime
from ilmar.core.layout import WorkspaceLayout
from ilmar.domain.workspace import ContainerHandle, ContainerSpec, NetworkPolicy, ResourceOwner

from integration.podman_support import (
    IMAGE,
    PODMAN,
    identity_of,
    make_workspace,
    podman,
    remove_everything,
    require,
    sh,
)

pytestmark = pytest.mark.integration

NAME = "ilmar-it-cred"
OWNER = ResourceOwner(instance_id="it-cred", execution_id="exec-1", attempt=1, token="tok-c")
SKIP = {"overlay", "overlay-layers", "overlay-images", "vfs", "vfs-layers", "vfs-images"}


@pytest.fixture(autouse=True)
def _require() -> Iterator[None]:
    require(("podman", PODMAN), ("ILMAR_TEST_PODMAN_IMAGE", IMAGE))
    remove_everything(NAME)
    yield
    remove_everything(NAME)


def storage_roots() -> list[Path]:
    info = json.loads(podman("info", "--format", "json").stdout)
    store = info.get("store", {})
    roots = {Path(store.get("graphRoot", "")), Path(store.get("runRoot", "")),
             Path(tempfile.gettempdir())}  # fmt: skip
    return sorted(r for r in roots if str(r) not in ("", ".") and r.exists())


def holders(marker: bytes) -> list[str]:
    """Every readable file under Podman's roots and the temp dir that contains ``marker``."""
    found = []
    for root in storage_roots():
        for directory, subdirs, files in os.walk(root):
            subdirs[:] = [d for d in subdirs if d not in SKIP]
            for name in files:
                path = Path(directory, name)
                try:
                    info = path.lstat()
                    # Regular files only: Podman keeps FIFOs and sockets here, and
                    # reading a FIFO would block forever.
                    if not stat.S_ISREG(info.st_mode) or info.st_size > 256 * 1024 * 1024:
                        continue
                    if marker in path.read_bytes():
                        found.append(str(path))
                except OSError:
                    continue
    return found


def start(layout: WorkspaceLayout) -> tuple[PodmanRuntime, ContainerHandle]:
    workspace = make_workspace(layout, "cred")
    runtime = PodmanRuntime(layout=layout)
    handle = runtime.start(
        ContainerSpec(
            image=IMAGE,
            host_workspace=workspace,
            name=NAME,
            owner=OWNER,
            workspace_identity=identity_of(workspace),
            network=NetworkPolicy.NONE,
        )
    )
    return runtime, handle


@pytest.fixture
def layout(tmp_path: Path) -> WorkspaceLayout:
    layout = WorkspaceLayout.under(tmp_path.resolve() / "state")
    layout.workspaces_root.mkdir(parents=True)
    return layout


def test_a_command_credential_is_never_written_by_podman(layout: WorkspaceLayout) -> None:
    marker = "ilmar-marker-" + random.token_hex(16)
    runtime, handle = start(layout)
    during: list[str] = []

    def search_while_running() -> None:
        time.sleep(2)
        during.extend(holders(marker.encode()))

    searcher = threading.Thread(target=search_while_running)
    searcher.start()
    result = runtime.run(handle, sh('sleep 6; test -n "$CRED"'), timeout_seconds=60,
                         env={"CRED": marker})  # fmt: skip
    searcher.join()
    assert result.exit_code == 0, result.stderr  # the command did receive it
    assert during == []
    assert holders(marker.encode()) == []
    runtime.stop(handle, owner=OWNER)
    assert holders(marker.encode()) == []


def test_the_credential_descriptor_is_not_visible_inside_the_container(
    layout: WorkspaceLayout,
) -> None:
    marker = "ilmar-marker-" + random.token_hex(16)
    runtime, handle = start(layout)
    try:
        listing = runtime.run(
            handle,
            sh("for f in /proc/self/fd/*; do readlink \"$f\"; done; "
               "for f in /proc/[0-9]*/environ; do cat \"$f\" 2>/dev/null; echo; done"),
            timeout_seconds=30, env={"CRED": marker},
        )  # fmt: skip
        assert listing.exit_code == 0, listing.stderr
        assert "memfd" not in listing.stdout
        assert "ilmar-env" not in listing.stdout
    finally:
        runtime.stop(handle, owner=OWNER)


def test_a_coordinator_killed_mid_command_leaves_the_credential_nowhere(
    layout: WorkspaceLayout, tmp_path: Path
) -> None:
    """The crash drill on one command: kill the coordinator, then remove as recovery does."""
    marker = "ilmar-marker-" + random.token_hex(16)
    runtime, handle = start(layout)
    child = (
        "import sys\n"
        "from pathlib import Path\n"
        "from ilmar.adapters.podman import PodmanRuntime\n"
        "from ilmar.core.layout import WorkspaceLayout\n"
        "from ilmar.domain.workspace import ContainerHandle\n"
        f"state = Path({str(layout.workspaces_root.parent)!r})\n"
        "rt = PodmanRuntime(layout=WorkspaceLayout.under(state))\n"
        f"h = ContainerHandle(runtime={handle.runtime!r}, id={handle.id!r}, name={handle.name!r})\n"
        "rt.run(h, ['sh', '-c', 'sleep 60'], timeout_seconds=120, "
        "env={'CRED': sys.argv[1]})\n"
    )
    src = Path(__file__).resolve().parents[2] / "src"
    env = {**os.environ, "PYTHONPATH": str(src)}
    coordinator = subprocess.Popen(  # noqa: S603 - a fixed child program
        [sys.executable, "-c", child, marker], env=env, start_new_session=True
    )
    try:
        time.sleep(5)  # podman exec is running the command now
        assert coordinator.poll() is None
    finally:
        os.killpg(coordinator.pid, signal.SIGKILL)
        coordinator.wait(timeout=30)
    while_orphaned = holders(marker.encode())
    assert runtime.remove_owned(NAME, owner=OWNER) is True  # what recovery does
    time.sleep(2)
    assert while_orphaned == []
    assert holders(marker.encode()) == []
