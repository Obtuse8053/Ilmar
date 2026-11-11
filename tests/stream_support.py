# SPDX-License-Identifier: Apache-2.0
"""Simulated standard-output streams for the fake runtimes (ADR-0035).

The fakes have no real processes or clocks. They give a command's output to a
:class:`~ilmar.ports.container_runtime.StreamConsumer` exactly as the real
runtime would, chunk by chunk, and decide the ending from simulated time:
``silence`` seconds pass after the last line.
"""

from __future__ import annotations

import json
from collections.abc import Sequence

from ilmar.domain.workspace import StreamVerdict
from ilmar.ports.container_runtime import StreamConsumer

CHUNK_BYTES = 4096


def emit_command(lines: Sequence[str], silence: float = 0.0) -> list[str]:
    """The fakes' ``emit`` program: print ``lines`` as given, then stay silent."""
    return ["emit", json.dumps(list(lines)), str(silence)]


def parse_emit(argv: Sequence[str]) -> tuple[list[str], float]:
    lines = json.loads(argv[1])
    assert isinstance(lines, list)
    return [str(line) for line in lines], float(argv[2])


def consume(
    consumer: StreamConsumer,
    data: bytes,
    *,
    silence: float = 0.0,
    idle_timeout: float | None = None,
    timeout: float = float("inf"),
) -> str:
    """Feed ``data`` and say how the command ends: ``"done"``, ``"rejected"``,
    ``"stalled"`` or ``"timed-out"``. Every chunk counts as arriving at once."""
    for start in range(0, len(data), CHUNK_BYTES):
        if consumer.feed(data[start : start + CHUNK_BYTES]) is StreamVerdict.VIOLATION:
            return "rejected"
    if idle_timeout is not None and silence >= idle_timeout and idle_timeout < timeout:
        return "stalled"
    if silence >= timeout:
        return "timed-out"
    if consumer.close() is StreamVerdict.VIOLATION:
        return "rejected"
    return "done"
