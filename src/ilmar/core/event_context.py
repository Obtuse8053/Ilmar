# SPDX-License-Identifier: Apache-2.0
"""The coordinator's side of the event log (ADR-0024 §2, layer 1).

The store derives every mandatory event from the rows a commit changes. What
the rows do not hold comes from here:

* :class:`PassEvents` gives each coordinator pass its random ``pass`` id and
  builds the :class:`~ilmar.domain.events.EventContext` of every commit the
  pass makes: the time, the owner, and the per-row notes (durations, error
  classes, reconciliation observations, reason codes).
* :func:`failure_code` maps a failure to its stable reason code. Nothing on a
  supported path maps to ``unclassified``: a failure the registry does not name
  is ``unexpected-error``, so a missing code shows up in tests as that, never
  as a silent gap.

No event is built here, and nothing here runs inside an effect closure.
"""

from __future__ import annotations

import secrets
from collections.abc import Callable, Mapping
from datetime import datetime

from ilmar.domain.errors import ConcurrencyConflictError, IlmarError, ProviderError
from ilmar.domain.events import (
    DATA_SCHEMAS,
    REASON_CODES,
    EventContext,
    EventType,
    reason_for_error,
)
from ilmar.domain.ledger import Fence


class CodedError(IlmarError):
    """A core failure that carries its own stable reason code (ADR-0024 §4)."""

    default_code = "unexpected-error"

    def __init__(self, message: str, *, code: str | None = None) -> None:
        super().__init__(message)
        code = code or self.default_code
        if code not in REASON_CODES or code == "unclassified":
            raise ValueError(f"{code!r} is not a reason code a failure may carry")
        self.code = code


def failure_code(error: BaseException) -> str:
    """The stable reason code of ``error``; never ``unclassified``."""
    if isinstance(error, CodedError):
        return error.code
    known = reason_for_error(error)
    if known != "unclassified":
        return known
    if isinstance(error, ConcurrencyConflictError):
        return "ledger-integrity"  # the records changed under this worker
    if isinstance(error, (ProviderError, OSError, TimeoutError)):
        return "transient-provider-error"
    return "unexpected-error"


_ERROR_CLASS = DATA_SCHEMAS[EventType.OPERATION_FAILED]["error_class"][0]


def error_class(error: BaseException) -> str | None:
    """The error's class name, when it fits the event schema; otherwise nothing
    (an event field is omitted, never written invalid)."""
    name = type(error).__name__
    return name if _ERROR_CLASS(name) else None


def new_pass_id() -> str:
    return "p-" + secrets.token_hex(6)


class PassEvents:
    """The ``pass`` id and event context for everything one coordinator pass writes."""

    def __init__(
        self, *, clock: Callable[[], datetime], new_id: Callable[[], str] = new_pass_id
    ) -> None:
        self._clock = clock
        self._new_id = new_id
        self.pass_id = new_id()

    def begin_pass(self) -> str:
        """A new pass id: every event of one ``run_once`` shares it."""
        self.pass_id = self._new_id()
        return self.pass_id

    def context(
        self,
        fence: Fence,
        *,
        reasons: Mapping[str, str] | None = None,
        durations_ms: Mapping[str, int] | None = None,
        error_classes: Mapping[str, str] | None = None,
        reconciled: frozenset[str] = frozenset(),
        observations: Mapping[str, str] | None = None,
    ) -> EventContext:
        return EventContext(
            at=self._clock(),
            instance=fence.instance_id,
            epoch=fence.epoch,
            pass_id=self.pass_id,
            reasons=dict(reasons or {}),
            durations_ms=dict(durations_ms or {}),
            error_classes=dict(error_classes or {}),
            reconciled=reconciled,
            observations=dict(observations or {}),
        )
