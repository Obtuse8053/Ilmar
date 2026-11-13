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
        """The suspension reason that set the signal, if a suspension did."""
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
        while not self._done.wait(self._interval):
            try:
                reading = self._read(self._execution_id)
            except (OSError, ValueError):
                continue  # never a reason to interrupt; the worker reports it later
            request = reading.request
            if request is not None and request.execution_id == self._execution_id:
                self.request = request  # before the signal: the worker reads it after
                self.signal.set()
                return
            try:
                reason = self._suspended()
            except (OSError, ValueError):
                reason = None  # unreadable now: the next check, or the pass, decides
            if reason is not None:
                self.suspended_by = reason
                self.signal.set()
                return
