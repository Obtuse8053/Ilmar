# SPDX-License-Identifier: Apache-2.0
"""The watcher that stops a running agent on a valid request (ADR-0025 §2).

It runs only while the agent runs. Every ``interval`` seconds it calls the
one validation routine for its execution and, **only for a valid request for
this execution**, sets the stop signal the runtime watches and hands the
validated request to the worker.

It also stops the agent when the instance is suspended (ADR-0029 §3, §4): the
kill-switch file appears, or another process writes a suspension. That stop is
not a cancellation: the worker ends the attempt as interrupted and retries it,
with a fresh reservation, only after ``ilmar resume``.

The two checks are independent: a failing inbox read never skips the
suspension check, and a suspension check that fails stops the agent, because
whether it may go on is then unknown (fail closed). The worker ends such an
attempt as ``suspension-check-failed``, without freezing its output.

It writes nothing and removes nothing; its only reads are the cancellation
inbox and the ``suspended`` check. The stop itself happens inside the
journaled ``run-agent`` effect, and the worker records the request after the
run returns. An invalid, unsafe or foreign entry never sets
the signal; the worker moves it aside at its next safe point.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from types import TracebackType
from typing import Self

from ilmar.core.cancel_inbox import CancelRequest, Reading

WATCH_INTERVAL_SECONDS = 2.0
SUSPENSION_CHECK_FAILED = "suspension-check-failed"
"""``suspended_by`` when the suspension check itself failed: the agent is stopped."""


class Watcher:
    """``with Watcher(read, execution_id) as watcher:`` around the agent run."""

    def __init__(
        self,
        read: Callable[[str], Reading],
        execution_id: str,
        *,
        interval: float = WATCH_INTERVAL_SECONDS,
        suspended: Callable[[], str | None] = lambda: None,
    ) -> None:
        if interval <= 0:
            raise ValueError("the watch interval must be positive")
        self._read = read
        self._execution_id = execution_id
        self._interval = interval
        self._done = threading.Event()
        self.signal = threading.Event()
        """The stop signal: set once, only for a valid request for this execution."""
        self.request: CancelRequest | None = None
        """The request that set the signal, exactly as it was validated."""
        self._suspended = suspended
        self.suspended_by: str | None = None
        """The suspension reason that set the signal, if a suspension did, or
        ``SUSPENSION_CHECK_FAILED`` when the check could not be made."""
        self.read_error: BaseException | None = None
        """The last failed inbox read; it never stops the agent on its own."""
        self.check_error: BaseException | None = None
        """The failure that made the watcher stop the agent, if the check failed."""
        self._thread = threading.Thread(target=self._watch, daemon=True,
                                        name=f"ilmar-watch-{execution_id}")  # fmt: skip

    def __enter__(self) -> Self:
        self._thread.start()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self._done.set()
        self._thread.join()

    def _watch(self) -> None:
        # The whole body runs on its own thread, which is a boundary: an exception
        # that escaped would end the thread silently and with it every later check.
        # So every check is guarded here, and the two never depend on each other:
        # a failing inbox read never skips the suspension check (#122 review).
        while not self._done.wait(self._interval):
            try:
                reading = self._read(self._execution_id)
            except Exception as error:  # thread boundary: kept for the worker
                self.read_error = error  # the worker reports the inbox at its next safe point
            else:
                request = reading.request
                if request is not None and request.execution_id == self._execution_id:
                    self.request = request  # before the signal: the worker reads it after
                    self.signal.set()
                    return
            try:
                reason = self._suspended()
            except Exception as error:  # thread boundary: fail closed
                # Whether work must stop is unknown: stop it (ADR-0029 §4). The
                # worker ends the attempt without freezing anything.
                self.check_error = error
                reason = SUSPENSION_CHECK_FAILED
            if reason is not None:
                self.suspended_by = reason
                self.signal.set()
                return
