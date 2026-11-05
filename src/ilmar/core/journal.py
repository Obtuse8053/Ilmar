# SPDX-License-Identifier: Apache-2.0
"""Write-ahead journal for external side effects (ADR-0012, ADR-0020).

Every external side effect runs through :meth:`Journal.perform`:

1. The *intent* is committed first, under the current fence, together with
   any ledger or state changes that must precede the effect (for example a
   resource recorded as ``INTENDED`` before it is created).
2. :meth:`Ownership.check` runs immediately before the effect. If ownership
   was lost, the effect does not happen.
3. The effect runs.
4. The outcome is committed:

   * ``SUCCEEDED``, with its result and any follow-up changes, atomically;
   * ``FAILED`` for the exception types the caller declares definitive (the
     effect certainly did not happen);
   * ``AMBIGUOUS`` for everything else.

A crash, or a lost fence, between steps leaves the operation ``INTENDED``,
which recovery treats exactly like ``AMBIGUOUS``.

Replaying a step with the same idempotency key never acts twice blindly:

* ``SUCCEEDED`` → the recorded result is returned and the effect is skipped;
* ``FAILED`` → ``LedgerIntegrityError``: a definitive failure is retried
  only by a new attempt, which has new keys;
* ``INTENDED`` or ``AMBIGUOUS`` → repeated only for kinds that are safe to
  repeat (``OperationKind.repeatable``). Otherwise ``AmbiguousOperationError``,
  until :meth:`reconcile` settles it from what the external system shows.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import datetime

from ilmar.core.ownership import Ownership
from ilmar.domain.changes import CandidateRevision
from ilmar.domain.errors import AmbiguousOperationError, LedgerIntegrityError
from ilmar.domain.execution import Execution
from ilmar.domain.ledger import (
    Commit,
    Operation,
    OperationKind,
    OperationStatus,
    changed_paths_digest,
    operation_key,
)
from ilmar.ports.execution_store import ExecutionStore

Result = Mapping[str, str]
_NOTE_LIMIT = 500


class Journal:
    def __init__(
        self,
        *,
        store: ExecutionStore,
        ownership: Ownership,
        clock: Callable[[], datetime],
    ) -> None:
        self._store = store
        self._ownership = ownership
        self._clock = clock

    def perform(
        self,
        kind: OperationKind,
        execution: Execution,
        effect: Callable[[], Result],
        *,
        step: str = "",
        intent: Mapping[str, str] | None = None,
        before: Commit = Commit(),  # noqa: B008 - an immutable, empty value
        after: Callable[[Result], Commit] | None = None,
        definitive: tuple[type[BaseException], ...] = (),
    ) -> Result:
        """Perform one side effect with write-ahead intent; see the module docstring.

        ``before`` is committed atomically with the intent and ``after(result)``
        atomically with the success.
        """
        fence = self._ownership.fence
        key = operation_key(kind, execution.id, execution.attempt, step)
        existing = self._store.operation(key)
        if existing is not None:
            if existing.status is OperationStatus.SUCCEEDED:
                return existing.result
            if existing.status is OperationStatus.FAILED:
                raise LedgerIntegrityError(
                    f"{key} already failed definitively; only a new attempt may retry it"
                )
            if not kind.repeatable:
                raise AmbiguousOperationError(
                    f"{key} may or may not have happened; reconcile it first", key=key
                )
            if not before.empty:
                raise LedgerIntegrityError(f"{key}: changes for a first attempt were given")
            operation = existing.moved(
                OperationStatus.INTENDED, fence=fence, now=self._clock(), note="repeated"
            )
        else:
            operation = Operation.intended(
                kind,
                execution.id,
                execution.attempt,
                fence=fence,
                now=self._clock(),
                step=step,
                intent=intent,
            )
        self._store.commit(fence, _with(before, operation))  # 1. intent, write-ahead

        self._ownership.check()  # 2. still the owner, right before acting
        try:
            result = dict(effect())  # 3. the effect
        except definitive as error:
            self._settle(operation, OperationStatus.FAILED, error)
            raise
        except BaseException as error:
            self._settle(operation, OperationStatus.AMBIGUOUS, error)
            raise
        succeeded = operation.moved(
            OperationStatus.SUCCEEDED, fence=fence, now=self._clock(), result=result
        )
        follow_up = after(result) if after is not None else Commit()
        self._store.commit(fence, _with(follow_up, succeeded))  # 4. the outcome
        return result

    def reconcile(
        self,
        key: str,
        status: OperationStatus,
        *,
        reason: str,
        result: Mapping[str, str] | None = None,
        changes: Commit = Commit(),  # noqa: B008 - an immutable, empty value
    ) -> Operation:
        """Settle an unfinished operation from what the external system shows.

        ``status`` must be final. ``reason`` records the observation, for
        example "branch head equals the candidate".
        """
        if not status.is_final:
            raise ValueError("reconciliation settles an operation as succeeded or failed")
        operation = self._store.operation(key)
        if operation is None:
            raise LedgerIntegrityError(f"no operation {key} to reconcile")
        if operation.status.is_final:
            raise LedgerIntegrityError(f"{key} is already {operation.status.value}")
        fence = self._ownership.fence
        if operation.status is OperationStatus.INTENDED:  # recovery sees it as ambiguous
            operation = operation.moved(
                OperationStatus.AMBIGUOUS, fence=fence, now=self._clock(), note="after a crash"
            )
            self._store.commit(fence, Commit(operations=(operation,)))
        settled = operation.moved(
            status, fence=fence, now=self._clock(), result=result, note=reason[:_NOTE_LIMIT]
        )
        self._store.commit(fence, _with(changes, settled))
        return settled

    def _settle(self, operation: Operation, status: OperationStatus, error: BaseException) -> None:
        """Record a failed or ambiguous outcome.

        If that write fails too (the fence was lost, the store is gone), the
        operation stays ``INTENDED``, which recovery treats as ambiguous; the
        effect's own error still propagates, with a note saying so.
        """
        try:
            moved = operation.moved(
                status, fence=self._ownership.fence, now=self._clock(), note=_describe(error)
            )
            self._store.commit(self._ownership.fence, Commit(operations=(moved,)))
        except Exception as failure:  # the effect's error is the one to raise
            error.add_note(
                f"{operation.key}: the outcome could not be recorded "
                f"({type(failure).__name__}); it stays intended"
            )


# -- candidate identity (ADR-0010, ADR-0020) ---------------------------------------------


def freeze_result(candidate: CandidateRevision) -> dict[str, str]:
    """What the journal keeps of a frozen candidate: enough to prove it later."""
    return {
        "revision": candidate.revision,
        "base_revision": candidate.base_revision,
        "changed_paths_digest": changed_paths_digest(candidate.changed_paths),
        "changed_path_count": str(len(set(candidate.changed_paths))),
    }


def check_candidate(candidate: CandidateRevision, freeze: Operation) -> None:
    """Fail closed unless ``candidate`` is exactly the one ``freeze`` recorded.

    Use it whenever a candidate is obtained other than straight from the
    freeze, for example when it is rebuilt from the coordinator's repository
    after a crash (#11c). The revision, base revision and changed paths must
    all match.
    """
    if freeze.kind is not OperationKind.FREEZE_CANDIDATE or (
        freeze.status is not OperationStatus.SUCCEEDED
    ):
        raise LedgerIntegrityError(f"{freeze.key} is not a successful freeze")
    if dict(freeze.result) != freeze_result(candidate):
        raise LedgerIntegrityError(
            f"candidate {candidate.revision[:12]} does not match what {freeze.key} recorded "
            "(revision, base revision or changed paths differ)"
        )


def _with(changes: Commit, operation: Operation) -> Commit:
    return Commit(
        created=changes.created,
        applied=changes.applied,
        operations=(operation, *changes.operations),
        resources=changes.resources,
    )


def _describe(error: BaseException) -> str:
    return f"{type(error).__name__}"
