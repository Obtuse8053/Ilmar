# SPDX-License-Identifier: Apache-2.0
"""The stand-in provider for the separate-sidecar gateway tests (ADR-0037).

It runs ``gateway_stub/stub.py`` on the host's own IPv4 address, outside every
container, as the provider the gateway sidecar forwards to. It accepts only
the given key, the real key that only the sidecar holds, and logs each
request's shape (never its text).
"""

from __future__ import annotations

import json
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

STUB = Path(__file__).parent / "gateway_stub" / "stub.py"


def free_port(host: str) -> int:
    with socket.socket() as probe:
        probe.bind((host, 0))
        return int(probe.getsockname()[1])


class Provider:
    """The stand-in provider on the host's own address; logs request shapes."""

    def __init__(self, host: str, log: Path, key: str) -> None:
        self.port = free_port(host)
        self.log = log
        self.process = subprocess.Popen(  # noqa: S603 - fixed test helper
            [sys.executable, "-I", str(STUB), str(self.port), str(log), key, host],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )  # fmt: skip
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            try:
                socket.create_connection((host, self.port), timeout=1).close()
                return
            except OSError:
                time.sleep(0.1)
        pytest.fail("the stand-in provider did not start")

    def requests(self) -> list[dict[str, object]]:
        if not self.log.exists():
            return []
        return [json.loads(line) for line in self.log.read_text().splitlines()]

    def stop(self) -> None:
        self.process.terminate()
        self.process.wait(10)
