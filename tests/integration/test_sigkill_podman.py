# SPDX-License-Identifier: Apache-2.0
"""The coordinator SIGKILLed during an agent run, on real Podman (#90, ADR-0029 §1).

ADR-0029 requires a runtime-side lifetime that holds with no coordinator
alive, and ADR-0035 lists this as its crash test. A real coordinator process
(``sigkill_child.py``) starts an agent command that would run for ten minutes
in an allowlisted sandbox and is then killed with SIGKILL. With no Ilmar
process running, the sandbox and its egress sidecar must stop by Podman's
``--timeout`` (enforced by conmon) after ``execution_wall_seconds`` plus the
grace period, and not before. A new coordinator then recovers: the attempt is
abandoned, the leftovers are removed, the retry waits for its backoff and a
new attempt succeeds in a new sandbox.

Needs ``ILMAR_TEST_PODMAN_IMAGE`` and ``ILMAR_TEST_PODMAN_PROXY_IMAGE``; skips
without them unless ``ILMAR_TEST_PODMAN_REQUIRED=1``. Takes about 90 seconds.
"""

from __future__ import annotations

import os
import pickle
import selectors
import signal
import subprocess
import sys
import time
import warnings
from collections.abc import Iterator
from pathlib import Path

import pytest

from ilmar.core.coordinator import Coordinator
from ilmar.core.lifetimes import SANDBOX_GRACE_SECONDS
from ilmar.core.naming import container_name
from ilmar.core.ownership import Ownership
from ilmar.domain.agent import AgentOutcome, AgentRequest, AgentResult
from ilmar.domain.execution import ExecutionState
from ilmar.domain.workspace import ContainerHandle, StopSignal
from ilmar.ports.container_runtime import ContainerRuntime

from integration.podman_support import (
    IMAGE,
    PODMAN,
    PROXY_IMAGE,
    podman,
    remove_everything,
    require,
)
from integration.sigkill_child import BACKOFF, INSTANCE, MARKER, components, utc_now, world_path

pytestmark = pytest.mark.integration

ROOT = Path(__file__).resolve().parents[2]
CHILD = Path(__file__).resolve().parent / "sigkill_child.py"
WALL = 10.0
LIFETIME = WALL + SANDBOX_GRACE_SECONDS
FIRST = container_name(INSTANCE, "e1", 1)
NAMES = tuple(container_name(INSTANCE, "e1", attempt) for attempt in (1, 2, 3))


@pytest.fixture(autouse=True)
def _require() -> Iterator[None]:
    require(("podman", PODMAN), ("ILMAR_TEST_PODMAN_IMAGE", IMAGE), ("proxy image", PROXY_IMAGE))
    remove_everything(*NAMES)
    yield
    remove_everything(*NAMES)


def state(name: str) -> tuple[bool, int, int] | None:
    """``(running, started, finished)`` in Unix seconds, or ``None`` if gone."""
    done = podman(
        "inspect", "--format",
        "{{.State.Running}} {{.State.StartedAt.Unix}} {{.State.FinishedAt.Unix}}",
        name, check=False,
    )  # fmt: skip
    if done.returncode != 0:
        return None
    running, started, finished = done.stdout.split()
    return running == "true", int(started), int(finished)


def start_coordinator(tmp: Path) -> tuple[subprocess.Popen[str], str]:
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(str(ROOT / p) for p in ("src", "tests", "scripts"))
    child = subprocess.Popen(  # noqa: S603 - fixed argv: this interpreter and a test file
        [sys.executable, str(CHILD), str(tmp), str(WALL)],
        cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )  # fmt: skip
    assert child.stdout is not None
    selector = selectors.DefaultSelector()
    selector.register(child.stdout, selectors.EVENT_READ)
    deadline = time.monotonic() + 240
    seen: list[str] = []
    while time.monotonic() < deadline:
        if not selector.select(timeout=1.0):
            if child.poll() is not None:
                break
            continue
        line = child.stdout.readline()
        if not line:
            break
        seen.append(line.rstrip())
        if line.startswith(MARKER):
            return child, line.split()[1]
    child.kill()
    child.wait()
    pytest.fail("the coordinator never reached the agent run:\n" + "\n".join(seen[-40:]))


class FixingAgent:
    def __init__(self) -> None:
        self.containers: list[str] = []

    @property
    def name(self) -> str:
        return "fixing"

    def run(
        self,
        runtime: ContainerRuntime,
        container: ContainerHandle,
        request: AgentRequest,
        *,
        stop: StopSignal | None = None,
    ) -> AgentResult:
        self.containers.append(container.name)
        done = runtime.run(
            container, ["sh", "-c", "printf \"print('fixed')\\n\" > src/app.py"],
            timeout_seconds=60,
        )  # fmt: skip
        assert done.exit_code == 0, done.stderr
        return AgentResult(AgentOutcome.COMPLETED, "fixed it", 0)


def recover(tmp: Path, agent: FixingAgent) -> ExecutionState:
    with warnings.catch_warnings():  # pickled itertools state (DeprecationWarning on 3.12+)
        warnings.simplefilter("ignore", DeprecationWarning)
        world = pickle.loads(world_path(tmp).read_bytes())  # noqa: S301 - written by this test
    store, parts = components(tmp, world, agent)
    owner = Ownership.take(layout=parts.layout, store=store, instance_id=INSTANCE, now=utc_now())
    try:
        Coordinator.create(
            parts, owner, max_attempts=3, agent_timeout_seconds=WALL,
            execution_wall_seconds=WALL, retry_backoff=BACKOFF, clock=utc_now,
            new_id=lambda: "e1",
        ).run_once()  # fmt: skip
        execution = store.get("e1")
        assert execution is not None
        return execution.state
    finally:
        owner.release()
        store.close()


def test_a_sigkilled_coordinator_leaves_no_sandbox_running(tmp_path: Path) -> None:
    tmp = tmp_path.resolve()
    (tmp / "state").mkdir()
    child, name = start_coordinator(tmp)
    assert name == FIRST
    running = state(FIRST)
    assert running is not None, "the sandbox does not exist"
    assert running[0], "the sandbox is not running"
    sidecar = state(FIRST + "-egress")
    assert sidecar is not None, "the egress sidecar does not exist"
    assert sidecar[0], "the egress sidecar is not running"

    os.kill(child.pid, signal.SIGKILL)
    assert child.wait(timeout=30) == -signal.SIGKILL
    assert child.stdout is not None
    child.stdout.close()

    # No Ilmar process is alive now. Only conmon's --timeout can stop the sandbox.
    deadline = time.monotonic() + LIFETIME + 60
    while time.monotonic() < deadline:
        current = state(FIRST)
        if current is None or not current[0]:
            break
        time.sleep(2)
    stopped = state(FIRST)
    assert stopped is not None, "the sandbox was removed, not stopped by its lifetime"
    is_running, started, finished = stopped
    assert not is_running, f"the sandbox still runs {LIFETIME + 60:g}s after the kill"
    lived = finished - started
    assert LIFETIME - 2 <= lived <= LIFETIME + 20, f"lived {lived}s, lifetime {LIFETIME:g}s"
    proxy = state(FIRST + "-egress")
    assert proxy is None or not proxy[0], "the egress sidecar outlived its sandbox"

    # A new coordinator: the attempt is abandoned and the retry waits for backoff.
    first = recover(tmp, FixingAgent())
    assert first is ExecutionState.RETRY_WAIT
    assert state(FIRST) is None
    assert state(FIRST + "-egress") is None
    time.sleep(BACKOFF.base_seconds + 1)
    agent = FixingAgent()
    assert recover(tmp, agent) is ExecutionState.SUCCEEDED
    assert agent.containers == [container_name(INSTANCE, "e1", 2)]
