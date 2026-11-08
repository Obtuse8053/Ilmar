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
* :func:`require_failure_codes` is the boundary check: a coordinator commit
  whose failure lacks a valid code is refused before it reaches the store, so
  it never becomes a successful commit with an ``unclassified`` event. The
  store's own fallback stays for other callers and for history (ADR-0024 §2).

No event is built here, and nothing here runs inside an effect closure.
"""

from __future__ import annotations

import secrets
from collections.abc import Callable, Mapping
from datetime import datetime

from ilmar.domain.errors import (
    ConcurrencyConflictError,
    IlmarError,
    LedgerIntegrityError,
    ProviderError,
)
from ilmar.domain.events import (
    DATA_SCHEMAS,
    EventContext,
    EventType,
    is_failure_code,
    reason_for_error,
    rows_needing_a_reason,
)
from ilmar.domain.ledger import Commit, Fence


class CodedError(IlmarError):
    """A core failure that carries its own stable reason code (ADR-0024 §4)."""

    default_code = "unexpected-error"

    def __init__(self, message: str, *, code: str | None = None) -> None:
        super().__init__(message)
        code = self.default_code if code is None else code
        if not is_failure_code(code):
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


def require_failure_codes(changes: Commit, context: EventContext) -> None:
    """Refuse a coordinator commit unless every failure in it has a valid code.

    Every given code must be registered and not ``unclassified``, and every row
    that needs a reason (:func:`~ilmar.domain.events.rows_needing_a_reason`)
    must have one. Raises ``LedgerIntegrityError`` before anything is written:
    the caller's change is refused, never committed with an ``unclassified``
    event, and the execution is blocked for a human (ADR-0022).
    """
    given = {**changes.reasons, **context.reasons}
    if invalid := sorted(k for k, code in given.items() if not is_failure_code(code)):
        raise LedgerIntegrityError(f"invalid reason codes for {invalid}; nothing is written")
    if missing := sorted(set(rows_needing_a_reason(changes)) - set(given)):
        raise LedgerIntegrityError(f"no reason code for {missing}; nothing is written")


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
        if invalid := sorted(k for k, code in (reasons or {}).items() if not is_failure_code(code)):
            raise ValueError(f"not a reason code a caller may give, for {invalid}")
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
