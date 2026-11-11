# SPDX-License-Identifier: Apache-2.0
"""The Podman adapter's executor with a stdout consumer (ADR-0035 §3, §4, §8).

Real short-lived processes (no Podman): the idle timer, the end on a
violation, output that is never kept, bounded standard error, and a consumer
that fails. Timings use wide margins.
"""

from __future__ import annotations

import os
import sys
import time

import pytest

from ilmar.adapters.podman.runtime import (
    STREAM_HEAD_BYTES,
    STREAM_TAIL_BYTES,
    subprocess_executor,
)
from ilmar.domain.errors import ProviderError
from ilmar.domain.workspace import StreamVerdict

ENV = {"PATH": os.environ.get("PATH", "/usr/bin:/bin")}


class Lines:
    """``ok`` lines are progress, ``bad`` is a violation; counts every byte."""

    def __init__(self) -> None:
        self.bytes = 0
        self.closed = False
        self._partial = b""

    def feed(self, chunk: bytes) -> StreamVerdict:
        self.bytes += len(chunk)
        *complete, self._partial = (self._partial + chunk).split(b"\n")
        if b"bad" in complete:
            return StreamVerdict.VIOLATION
        return StreamVerdict.PROGRESS if b"ok" in complete else StreamVerdict.NONE

    def close(self) -> StreamVerdict:
        self.closed = True
        return StreamVerdict.NONE


def python(code: str) -> list[str]:
    return [sys.executable, "-c", code]


def run(code: str, consumer: Lines, *, idle: float | None, timeout: float = 30.0):  # type: ignore[no-untyped-def]
    began = time.monotonic()
    done = subprocess_executor(
        python(code), ENV, timeout, stdout_consumer=consumer, idle_timeout=idle
    )
    return done, time.monotonic() - began


PRINT = "import sys, time\ndef p(s): sys.stdout.write(s + '\\n'); sys.stdout.flush()\n"


def test_silence_longer_than_the_idle_timeout_stalls_and_kills() -> None:
    done, took = run(PRINT + "p('ok'); time.sleep(30)", Lines(), idle=1.0)
    assert done.stalled
    assert (done.timed_out, done.rejected, done.cancelled) == (False, False, False)
    assert done.stdout == ""
    assert took < 10


def test_a_silent_stretch_under_the_timeout_does_not_stall() -> None:
    done, _ = run(PRINT + "p('ok'); time.sleep(0.6); p('ok'); time.sleep(0.6); p('ok')",
                  Lines(), idle=2.0)  # fmt: skip
    assert done.returncode == 0
    assert not done.stalled


def test_steady_progress_never_stalls_however_long_the_run() -> None:
    done, took = run(PRINT + "for _ in range(12): p('ok'); time.sleep(0.25)", Lines(), idle=1.0)
    assert done.returncode == 0
    assert not done.stalled
    assert took >= 2.5  # the run lasted longer than the idle timeout


def test_output_that_is_not_progress_does_not_reset_the_timer() -> None:
    done, took = run(PRINT + "p('ok')\nfor _ in range(100): p('noise'); time.sleep(0.1)",
                     Lines(), idle=1.0)  # fmt: skip
    assert done.stalled
    assert took < 8


def test_standard_error_never_resets_the_timer() -> None:
    code = PRINT + (
        "p('ok')\n"
        "for _ in range(100): sys.stderr.write('e\\n'); sys.stderr.flush(); time.sleep(0.1)"
    )
    done, _ = run(code, Lines(), idle=1.0)
    assert done.stalled


def test_a_violation_ends_the_command_at_once() -> None:
    consumer = Lines()
    done, took = run(PRINT + "p('ok'); p('bad'); time.sleep(30); p('ok')", consumer, idle=None)
    assert done.rejected
    assert took < 10  # no further spending: the command did not run on
    assert done.stdout == ""


def test_twenty_mebibytes_of_output_are_consumed_and_none_is_kept() -> None:
    consumer = Lines()
    code = (
        "import sys\nline = b'x' * 1023 + b'\\n'\n"
        "for _ in range(20 * 1024): sys.stdout.buffer.write(line)"
    )
    done, _ = run(code, consumer, idle=None)
    assert done.returncode == 0
    assert done.stdout == ""
    assert consumer.bytes == 20 * 1024 * 1024
    assert consumer.closed


def test_standard_error_beside_a_consumer_keeps_only_its_head_and_tail() -> None:
    code = (
        "import sys\nsys.stderr.write('HEAD' + 'h' * 300000 + 'm' * 2000000 + 't' * 70000 + 'TAIL')"
    )
    done, _ = run(code, Lines(), idle=None)
    assert done.stderr.startswith("HEAD")
    assert done.stderr.endswith("TAIL")
    assert "further bytes of output discarded" in done.stderr
    assert len(done.stderr) < STREAM_HEAD_BYTES + STREAM_TAIL_BYTES + 200


class Broken(Lines):
    def feed(self, chunk: bytes) -> StreamVerdict:
        raise RuntimeError("consumer bug")


def test_a_failing_consumer_fails_closed_and_never_hangs() -> None:
    began = time.monotonic()
    with pytest.raises(ProviderError, match="output consumer"):
        subprocess_executor(
            python(PRINT + "p('ok'); time.sleep(30)"), ENV, 30.0, stdout_consumer=Broken()
        )
    assert time.monotonic() - began < 10


def test_an_idle_timeout_needs_a_consumer() -> None:
    with pytest.raises(ValueError, match="consumer"):
        subprocess_executor(python("pass"), ENV, 5.0, idle_timeout=1.0)
