# SPDX-License-Identifier: Apache-2.0
"""Write-ahead journal for external side effects (ADR-0012, ADR-0020).

Every external side effect runs through :meth:`Journal.perform`, by a worker
holding the execution's lease:

1. The *intent* is committed first, under the current fence, together with
   any ledger or state changes that must precede the effect (for example a
   resource recorded as ``INTENDED`` before it is created). The commit also
   requires the execution to be exactly the version this worker last saw.
2. Immediately before the effect: the instance lock and fence
   (:meth:`Ownership.check`), the lease, and the execution's stored version
   are checked again. If any of them changed, the effect does not happen.
3. The effect runs.
4. The outcome is committed:

   * ``SUCCEEDED``, with its result and any follow-up changes, atomically;
   * ``FAILED`` for the exception types the caller declares definitive (the
     effect certainly did not happen);
   * ``AMBIGUOUS`` for everything else;
   * ``AMBIGUOUS`` *with the result* if the effect succeeded but its
     follow-up changes were refused, so the knowledge that it happened is
     never lost (``LedgerIntegrityError`` is raised).

A crash, or a lost fence, between steps leaves the operation ``INTENDED``,
which recovery treats exactly like ``AMBIGUOUS``.

Every commit carries its event context (ADR-0024): the pass, the effect's
duration measured with the monotonic clock around the effect alone, the error
class and stable reason code of a failed or ambiguous outcome, and for a
reconciliation, what was observed.

Replaying a step with the same idempotency key never acts twice blindly:

* ``SUCCEEDED`` → the recorded result is returned and the effect is skipped;
* ``FAILED`` → ``LedgerIntegrityError``: a definitive failure is retried
  only by a new attempt, which has new keys;
* ``INTENDED`` or ``AMBIGUOUS`` → repeated only for kinds that are safe to
  repeat (``OperationKind.repeatable``). Otherwise ``AmbiguousOperationError``,
  until :meth:`reconcile` settles it from what the external system shows.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping
from datetime import datetime

from ilmar.core.event_context import (
    PassEvents,
    error_class,
    failure_code,
    require_failure_codes,
)
from ilmar.core.ownership import ExecutionLease, Ownership
from ilmar.domain.changes import CandidateRevision
from ilmar.domain.errors import (
    AmbiguousOperationError,
    ConcurrencyConflictError,
    FencingError,
    LedgerIntegrityError,
    SuspendedError,
)
from ilmar.domain.events import OBSERVATIONS, EventContext, is_failure_code
from ilmar.domain.execution import Execution
from ilmar.domain.ledger import (
    Commit,
    Fence,
    Operation,
    OperationKind,
    OperationStatus,
    changed_paths_digest,
    instance_operation_key,
    operation_key,
)
from ilmar.ports.execution_store import ExecutionStore

Result = Mapping[str, str]
_NOTE_LIMIT = 500


HELD_WHILE_SUSPENDED = frozenset(
    {
        OperationKind.CLAIM_TASK,
        OperationKind.START_CONTAINER,
        OperationKind.RUN_AGENT,
        OperationKind.VERIFY,
        OperationKind.PUBLISH_REVISION,
        OperationKind.UPSERT_CHANGE_REQUEST,
    }
)
"""ADR-0029 §3: the effects a suspension holds, whether new or repeated after an
unknown outcome. Every other kind is cleanup or local bookkeeping and still
runs: releasing claims, notes, stopping containers, freezing a finished
agent's output, deleting workspaces and reconciling labels."""


class Journal:
    def __init__(
        self,
        *,
        store: ExecutionStore,
        ownership: Ownership,
        clock: Callable[[], datetime],
        events: PassEvents | None = None,
        monotonic: Callable[[], float] = time.monotonic,
        always_definitive: tuple[type[BaseException], ...] = (),
    ) -> None:
        """``always_definitive`` errors are recorded as ``FAILED`` for every kind.

        The coordinator passes its never-transient classes (security, policy,
        ownership, integrity): their outcome is known, and recording it as
        final (with the class as the note) is what lets recovery refuse to
        retry them after a crash (ADR-0021, ADR-0022).
        """
        self._store = store
        self._ownership = ownership
        self._clock = clock
        self._events = events or PassEvents(clock=clock)
        self._monotonic = monotonic
        self._always_definitive = always_definitive

    @property
    def events(self) -> PassEvents:
        return self._events

    @property
    def fence(self) -> Fence:
        return self._ownership.fence

    def perform(
        self,
        kind: OperationKind,
        execution: Execution,
        effect: Callable[[], Result],
        *,
        lease: ExecutionLease,
        step: str = "",
        intent: Mapping[str, str] | None = None,
        before: Commit = Commit(),  # noqa: B008 - an immutable, empty value
        after: Callable[[Result], Commit] | None = None,
        definitive: tuple[type[BaseException], ...] = (),
        observed: str | None = None,
        outcome_observed: Callable[[Result], str | None] | None = None,
    ) -> Result:
        """Perform one side effect with write-ahead intent; see the module docstring.

        ``execution`` is this worker's last view of the execution; ``before``
        is committed atomically with the intent and ``after(result)``
        atomically with the success. ``observed`` is the read that decided to
        perform it (:data:`~ilmar.domain.events.OBSERVATIONS`, ADR-0024 §1): it
        is recorded on the operation's intent and outcome events, in the same
        commits as ``before`` and ``after``. ``outcome_observed(result)``, if
        given, names what the effect itself found instead, for the success
        event (ADR-0026 §5: ``claim-absent``).
        """
        if observed is not None and observed not in OBSERVATIONS:
            raise ValueError(f"{observed!r} is not a registered observation")
        if kind.instance_scoped:
            raise LedgerIntegrityError(f"{kind.value} is instance-scoped; use perform_instance")
        lease.check(execution.id)
        if kind in HELD_WHILE_SUSPENDED and (held := self._store.suspension()) is not None:
            # ADR-0029 §3: no new claim, container, agent run, verification run or
            # publication while suspended, also for executions resumed by recovery.
            # Nothing is written; cleanup kinds still run.
            raise SuspendedError(
                f"{kind.value} for {execution.id} is held: new work is suspended "
                f"({held.reason.value})",
                reason=held.reason.value,
            )
        _check_scope(before, execution.id)
        definitive = (*definitive, *self._always_definitive)
        fence = self._ownership.fence
        expected = next(
            (e.version for e, _ in before.applied if e.id == execution.id), execution.version
        )
        key = operation_key(kind, execution.id, execution.attempt, step)
        seen = {key: observed} if observed is not None else None
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
        intent_commit = _with(before, operation)
        self._commit(  # 1. intent, write-ahead, only on the version this worker saw
            fence,
            Commit(
                created=intent_commit.created,
                applied=intent_commit.applied,
                operations=intent_commit.operations,
                resources=intent_commit.resources,
                expected=((execution.id, expected),),
                reasons=intent_commit.reasons,
                extra_events=intent_commit.extra_events,
            ),
            context=self._events.context(fence, observations=seen),
        )

        # 2. Right before acting: still the owner, still the lease holder, and
        # nobody else has moved the execution since.
        self._ownership.check()
        lease.check(execution.id)
        stored = self._store.get(execution.id)
        if stored is None or stored.version != expected:
            raise ConcurrencyConflictError(
                f"{execution.id} changed after {key} was recorded; no side effect"
            )
        started = self._monotonic()
        try:
            result = dict(effect())  # 3. the effect
        except definitive as error:
            self._settle(operation, OperationStatus.FAILED, error, self._elapsed(started), seen)
            raise
        except BaseException as error:
            self._settle(operation, OperationStatus.AMBIGUOUS, error, self._elapsed(started), seen)
            raise
        duration = self._elapsed(started)
        succeeded = operation.moved(
            OperationStatus.SUCCEEDED, fence=fence, now=self._clock(), result=result
        )
        found = _found(outcome_observed, result, seen, key)
        try:  # 4. the outcome, with its follow-up changes
            follow_up = after(result) if after is not None else Commit()
            _check_scope(follow_up, execution.id)
            self._commit(
                fence,
                _with(follow_up, succeeded),
                context=self._events.context(
                    fence, durations_ms={key: duration}, observations=found
                ),
            )
        except FencingError:
            raise  # ownership was lost: nothing more may be written or done
        except Exception as error:
            # The effect happened. Keep that knowledge (and its result) even
            # though the follow-up was refused: recovery reconciles it.
            self._keep_result(operation, result, error, duration, seen)
            raise LedgerIntegrityError(
                f"{key} succeeded but its follow-up changes were refused "
                f"({type(error).__name__}); it is recorded as ambiguous with its result"
            ) from error
        return result

    def reconcile(
        self,
        key: str,
        status: OperationStatus,
        *,
        lease: ExecutionLease,
        reason: str,
        observed: str,
        code: str | None = None,
        result: Mapping[str, str] | None = None,
        changes: Commit = Commit(),  # noqa: B008 - an immutable, empty value
    ) -> Operation:
        """Settle an unfinished operation from what the external system shows.

        ``status`` must be final. ``reason`` is the human-readable note,
        ``observed`` what was seen (:data:`~ilmar.domain.events.OBSERVATIONS`),
        and ``code`` the stable reason code, required when it failed.
        """
        if not status.is_final:
            raise ValueError("reconciliation settles an operation as succeeded or failed")
        if observed not in OBSERVATIONS:
            raise ValueError(f"{observed!r} is not a registered observation")
        if code is not None and not is_failure_code(code):
            raise ValueError(f"{code!r} is not a reason code a caller may give")
        if status is OperationStatus.FAILED and code is None:
            raise ValueError("a failed reconciliation needs a reason code")
        operation = self._store.operation(key)
        if operation is None:
            raise LedgerIntegrityError(f"no operation {key} to reconcile")
        # Like perform: only the lease holder settles an execution's operations,
        # and only while this coordinator still owns the instance (ADR-0020).
        if operation.execution_id is None:
            raise LedgerIntegrityError(f"{key} is instance-scoped; no lease settles it")
        lease.check(operation.execution_id)
        self._ownership.check()
        if operation.status.is_final:
            raise LedgerIntegrityError(f"{key} is already {operation.status.value}")
        if operation.kind is OperationKind.FREEZE_CANDIDATE:
            # A candidate's identity must come from an actual freeze, never
            # from an observation; a freeze is repeatable, so it is redone.
            raise LedgerIntegrityError(f"{key}: a freeze is performed again, never reconciled")
        fence = self._ownership.fence
        if operation.status is OperationStatus.INTENDED:  # recovery sees it as ambiguous
            operation = operation.moved(
                OperationStatus.AMBIGUOUS, fence=fence, now=self._clock(), note="after a crash"
            )
            self._commit(
                fence,
                Commit(operations=(operation,)),
                context=self._events.context(fence, reasons={key: "interrupted"}),
            )
        settled = operation.moved(
            status, fence=fence, now=self._clock(), result=result, note=reason[:_NOTE_LIMIT]
        )
        self._commit(
            fence,
            _with(changes, settled),
            context=self._events.context(
                fence,
                reasons={key: code} if code is not None else None,
                reconciled=frozenset({key}),
                observations={key: observed},
            ),
        )
        return settled

    def intend_instance(
        self, kind: OperationKind, *, step: str, intent: Mapping[str, str], reason: str
    ) -> Operation:
        """Commit only the write-ahead intent of an instance-scoped operation.

        ADR-0027 §1: the intent is the durable first observation, and the effect
        runs only when :meth:`perform_instance` resumes it in a later pass. An
        existing operation with this key is returned as it is: nothing is
        recorded twice.
        """
        if not kind.instance_scoped:
            raise LedgerIntegrityError(f"{kind.value} names an execution; use perform")
        self._ownership.check()
        fence = self._ownership.fence
        key = instance_operation_key(kind, fence.instance_id, step)
        existing = self._store.operation(key)
        if existing is not None:
            return existing
        operation = Operation.intended_for_instance(
            kind, fence.instance_id, fence=fence, now=self._clock(), step=step, intent=intent
        )
        self._commit(
            fence,
            Commit(operations=(operation,)),
            context=self._events.context(fence, reasons={key: reason}),
        )
        return operation

    def perform_instance(
        self,
        kind: OperationKind,
        effect: Callable[[], Result],
        *,
        step: str,
        intent: Mapping[str, str],
        reason: str,
        revalidate: Callable[[], None],
        definitive: tuple[type[BaseException], ...] = (),
        outcome_observed: Callable[[Result], str | None] | None = None,
    ) -> Result:
        """Perform an instance-scoped side effect (ADR-0026 §5, the ADR-0020 amendment).

        It names no execution, so no lease guards it: the instance lock and the
        fence do, checked again right before the effect, as in :meth:`perform`.
        ``revalidate`` runs after the intent commit and that check; if it raises,
        the effect never happens and the operation is recorded as failed.
        ``reason`` is the reason code on the operation's events.

        A repeated call for an unfinished operation repeats it from the intent
        **stored** with it, never from the caller's: the stored intent names the
        object to act on.
        """
        if not kind.instance_scoped:
            raise LedgerIntegrityError(f"{kind.value} names an execution; use perform")
        self._ownership.check()
        fence = self._ownership.fence
        definitive = (*definitive, *self._always_definitive)
        key = instance_operation_key(kind, fence.instance_id, step)
        reasons = {key: reason}
        existing = self._store.operation(key)
        if existing is not None:
            if existing.status is OperationStatus.SUCCEEDED:
                return existing.result
            if existing.status is OperationStatus.FAILED:
                raise LedgerIntegrityError(f"{key} already failed definitively")
            if dict(existing.intent) != dict(intent):
                raise LedgerIntegrityError(f"{key}: the intent differs from the recorded one")
            operation = existing.moved(
                OperationStatus.INTENDED, fence=fence, now=self._clock(), note="repeated"
            )
        else:
            operation = Operation.intended_for_instance(
                kind, fence.instance_id, fence=fence, now=self._clock(), step=step, intent=intent
            )
        self._commit(  # 1. intent, write-ahead
            fence,
            Commit(operations=(operation,)),
            context=self._events.context(fence, reasons=reasons),
        )
        self._ownership.check()  # 2. still the owner, right before acting
        try:
            revalidate()
        except BaseException as error:
            self._settle(operation, OperationStatus.FAILED, error, 0)
            raise
        started = self._monotonic()
        try:
            result = dict(effect())  # 3. the effect
        except definitive as error:
            self._settle(operation, OperationStatus.FAILED, error, self._elapsed(started))
            raise
        except BaseException as error:
            self._settle(operation, OperationStatus.AMBIGUOUS, error, self._elapsed(started))
            raise
        duration = self._elapsed(started)
        succeeded = operation.moved(
            OperationStatus.SUCCEEDED, fence=fence, now=self._clock(), result=result
        )
        self._commit(  # 4. the outcome
            fence,
            Commit(operations=(succeeded,)),
            context=self._events.context(
                fence,
                reasons=reasons,
                durations_ms={key: duration},
                observations=_found(outcome_observed, result, None, key),
            ),
        )
        return result

    def record(self, changes: Commit, *, lease: ExecutionLease, execution_id: str) -> None:
        """A state or ledger change with no external effect (a transition, a quarantine):
        by the lease holder, under the fence, with this pass's event context."""
        lease.check(execution_id)
        _check_scope(changes, execution_id)
        fence = self._ownership.fence
        self._commit(fence, changes, context=self._events.context(fence))

    def _commit(self, fence: Fence, changes: Commit, *, context: EventContext) -> None:
        """Every write the journal makes: refused before the store if a failure in it
        lacks a valid code, so the change and its events are written together or
        not at all."""
        require_failure_codes(changes, context)
        self._store.commit(fence, changes, context=context)

    def _elapsed(self, started: float) -> int:
        return max(0, round((self._monotonic() - started) * 1000))

    def _keep_result(
        self,
        operation: Operation,
        result: Result,
        error: Exception,
        duration: int,
        seen: Mapping[str, str] | None,
    ) -> None:
        try:
            fence = self._ownership.fence
            moved = operation.moved(
                OperationStatus.AMBIGUOUS,
                fence=fence,
                now=self._clock(),
                result=result,
                note=f"effect succeeded; follow-up refused: {type(error).__name__}",
            )
            self._commit(
                fence,
                Commit(operations=(moved,)),
                context=self._outcome(fence, moved, error, duration, seen),
            )
        except Exception as failure:  # then it stays intended, which is also safe
            error.add_note(f"{operation.key}: could not keep the result ({type(failure).__name__})")

    def _outcome(
        self,
        fence: Fence,
        operation: Operation,
        error: BaseException,
        duration: int,
        seen: Mapping[str, str] | None,
    ) -> EventContext:
        named = error_class(error)
        return self._events.context(
            fence,
            reasons={operation.key: failure_code(error)},
            durations_ms={operation.key: duration},
            error_classes={operation.key: named} if named else None,
            observations=seen,
        )

    def _settle(
        self,
        operation: Operation,
        status: OperationStatus,
        error: BaseException,
        duration: int,
        seen: Mapping[str, str] | None = None,
    ) -> None:
        """Record a failed or ambiguous outcome.

        If that write fails too (the fence was lost, the store is gone), the
        operation stays ``INTENDED``, which recovery treats as ambiguous; the
        effect's own error still propagates, with a note saying so.
        """
        try:
            fence = self._ownership.fence
            moved = operation.moved(status, fence=fence, now=self._clock(), note=_describe(error))
            self._commit(
                fence,
                Commit(operations=(moved,)),
                context=self._outcome(fence, moved, error, duration, seen),
            )
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


def _check_scope(changes: Commit, execution_id: str) -> None:
    """A worker's journaled changes stay inside the execution it holds the lease for.

    Executions run in parallel; one worker must never move another
    execution's state, journal or resources (ADR-0020).
    """
    owners = {e.id for e, _ in (*changes.created, *changes.applied)}
    owners |= {o.execution_id or "(the instance)" for o in changes.operations}
    owners |= {r.execution_id for r in changes.resources}
    owners |= {execution for execution, _ in changes.expected}
    owners |= {str(event.execution) for event in changes.extra_events}
    foreign = sorted(owners - {execution_id})
    if foreign:
        raise LedgerIntegrityError(
            f"a worker for {execution_id} cannot change records of {', '.join(foreign)}"
        )


def _found(
    outcome_observed: Callable[[Result], str | None] | None,
    result: Result,
    seen: Mapping[str, str] | None,
    key: str,
) -> Mapping[str, str] | None:
    """The observation recorded with a success: what the effect found, if it says."""
    if outcome_observed is None:
        return seen
    found = outcome_observed(result)
    if found is None:
        return seen
    if found not in OBSERVATIONS:
        raise ValueError(f"{found!r} is not a registered observation")
    return {key: found}


def _with(changes: Commit, operation: Operation) -> Commit:
    return Commit(
        created=changes.created,
        applied=changes.applied,
        operations=(operation, *changes.operations),
        resources=changes.resources,
        reasons=changes.reasons,
        extra_events=changes.extra_events,
    )


def _describe(error: BaseException) -> str:
    return f"{type(error).__name__}"
