# SPDX-License-Identifier: Apache-2.0
"""The process that ``test_podman_gateway_crash.py`` kills mid-start (#120 review).

It starts a ``GATEWAY`` sandbox with a real ``PodmanRuntime`` and SIGKILLs
itself at one stage of the key handoff, so nothing in it can clean up:

* ``awaiting``: the gateway is waiting for its key; the handoff never starts;
* ``partial``: the handoff has started and half of the key has been written;
* ``handed``: the gateway has taken the key; it is not yet ready.

Usage: ``python gateway_crash_child.py <state dir> <name> <stage>``, with the
stand-in key in ``ILMAR_TEST_GATEWAY_KEY``.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from gateway_policy import BUDGET, POLICY
from ilmar.adapters.podman import GatewaySettings, PodmanRuntime
from ilmar.adapters.podman.runtime import Completed, subprocess_executor
from ilmar.core.layout import WorkspaceLayout
from ilmar.domain.workspace import ContainerSpec, NetworkPolicy, ResourceOwner

from integration.podman_support import IMAGE, PROXY_IMAGE, identity_of, make_workspace

OWNER = ResourceOwner(instance_id="it", execution_id="gwcrash", attempt=1, token="tok-gwcrash")


def die() -> None:
    sys.stdout.flush()
    os.kill(os.getpid(), signal.SIGKILL)


def main(state: Path, name: str, stage: str) -> None:
    key = os.environ["ILMAR_TEST_GATEWAY_KEY"]

    def executor(argv: Sequence[str], env: Mapping[str, str], timeout: float,
                 **options: Any) -> Completed:  # fmt: skip
        handoff = "exec" in argv[1:2] and options.get("stdin") is not None and "-c" in argv
        if handoff and stage == "awaiting":
            die()
        if handoff and stage == "partial":
            child = subprocess.Popen(list(argv), env=dict(env), stdin=subprocess.PIPE)  # noqa: S603
            assert child.stdin is not None
            child.stdin.write(options["stdin"][: len(options["stdin"]) // 2])
            child.stdin.flush()
            die()
        done = subprocess_executor(argv, env, timeout, **options)
        if handoff and stage == "handed":
            die()
        return done

    layout = WorkspaceLayout.under(state)
    layout.workspaces_root.mkdir(parents=True, exist_ok=True)
    workspace = make_workspace(layout, "gwcrash")
    runtime = PodmanRuntime(
        layout=layout,
        executor=executor,
        gateway=GatewaySettings(image=str(PROXY_IMAGE), key=key, policy=POLICY,
                                upstream="http://192.0.2.1:9", allow_plain_http_upstream=True),
    )  # fmt: skip
    runtime.start(
        ContainerSpec(image=str(IMAGE), host_workspace=workspace, name=name, owner=OWNER,
                      workspace_identity=identity_of(workspace),
                      network=NetworkPolicy.GATEWAY, model_budget_micros=BUDGET,
                      lifetime_seconds=600)
    )  # fmt: skip
    print("STARTED-WITHOUT-DYING", flush=True)


if __name__ == "__main__":
    main(Path(sys.argv[1]), sys.argv[2], sys.argv[3])
