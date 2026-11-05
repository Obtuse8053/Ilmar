# SPDX-License-Identifier: Apache-2.0
"""Port for durable execution state."""

from __future__ import annotations

from collections.abc import Callable, Collection, Sequence
from datetime import datetime
from typing import Protocol

from ilmar.domain.errors import (
    ConcurrencyConflictError,
    ExecutionNotFoundError,
    FencingError,
    LedgerIntegrityError,
)
from ilmar.domain.execution import Execution, ExecutionState, Transition
from ilmar.domain.ledger import (
    Commit,
    Fence,
    Operation,
    OperationKind,
    OperationStatus,
    Resource,
    ResourceStatus,
)
from ilmar.domain.tasks import TaskRef
from ilmar.domain.verification import EvidenceRecord


class ExecutionStore(Protocol):
    """Persists executions and their transition history.

    The store does not decide which transitions are legal; that is the state
    machine's job. It guarantees, atomically:

    * at most one non-terminal execution per task
      (``create`` raises ``DuplicateExecutionError``);
    * no lost updates: ``apply`` succeeds only when the stored version is
      ``updated.version - 1`` (otherwise ``ConcurrencyConflictError``);
    * the execution record and its history entry are written together or not
      at all;
    * transitions and evidence records are append-only: there is no way to
      change or remove one, and evidence read back is checked against its
      digest (``EvidenceIntegrityError``).

    Fencing, the operation journal and the resource ledger (ADR-0020):

    * :meth:`claim_instance` makes a coordinator the store's owner and returns
      a :class:`Fence` with a new, higher epoch. From then on **every** write
      must carry the current fence; a missing or superseded fence raises
      ``FencingError`` and changes nothing.
    * :meth:`commit` writes executions, transitions, operations and resources
      in one transaction, all or nothing, under the rules of
      :func:`check_commit`.

    Inputs must satisfy :func:`check_create` / :func:`check_apply`.
    """

    def create(
        self, execution: Execution, transition: Transition, *, fence: Fence | None = None
    ) -> None: ...

    def get(self, execution_id: str) -> Execution | None: ...

    def active_for_task(self, task: TaskRef) -> Execution | None: ...

    def list_in_states(self, states: Collection[ExecutionState]) -> Sequence[Execution]:
        """Executions in any of ``states``, oldest first (ties broken by id)."""
        ...

    def apply(
        self, updated: Execution, transition: Transition, *, fence: Fence | None = None
    ) -> None:
        """Replace the stored execution with ``updated`` and append ``transition``.

        Raises ``ExecutionNotFoundError`` if the execution does not exist.
        """
        ...

    def history(self, execution_id: str) -> Sequence[Transition]:
        """All transitions in the order they were stored.

        Raises ``ExecutionNotFoundError`` if the execution does not exist.
        """
        ...

    def append_evidence(self, record: EvidenceRecord, *, fence: Fence | None = None) -> None:
        """Append a verification evidence record (ADR-0010, ADR-0018).

        Raises ``ExecutionNotFoundError`` if the execution does not exist.
        """
        ...

    def evidence(self, execution_id: str) -> Sequence[EvidenceRecord]:
        """All evidence records of an execution, in the order they were appended.

        Raises ``ExecutionNotFoundError`` if the execution does not exist.
        """
        ...

    # -- fencing, journal and ledger (ADR-0020) -------------------------------

    def claim_instance(self, instance_id: str, *, now: datetime) -> Fence:
        """Take ownership: return a fence whose epoch exceeds every earlier one.

        Call only while holding the instance lock. Every fence handed out
        before is superseded at once.
        """
        ...

    def fence(self) -> Fence | None:
        """The current owner's fence, or ``None`` if no coordinator ever claimed."""
        ...

    def commit(self, fence: Fence, changes: Commit) -> None:
        """Apply ``changes`` atomically if ``fence`` is current (see :func:`check_commit`)."""
        ...

    def operation(self, key: str) -> Operation | None: ...

    def operations(self, execution_id: str) -> Sequence[Operation]:
        """The journal of one execution, oldest first."""
        ...

    def unfinished_operations(self) -> Sequence[Operation]:
        """Every operation still ``INTENDED`` or ``AMBIGUOUS``, oldest first."""
        ...

    def resource(self, key: str) -> Resource | None: ...

    def resources(
        self,
        *,
        execution_id: str | None = None,
        statuses: Collection[ResourceStatus] | None = None,
    ) -> Sequence[Resource]:
        """Ledger entries, optionally filtered, oldest first."""
        ...


def check_create(execution: Execution, transition: Transition) -> None:
    """Validate arguments to :meth:`ExecutionStore.create`. Raises ``ValueError``."""
    if execution.version != 1:
        raise ValueError("a new execution must have version 1")
    if transition.from_state is not None:
        raise ValueError("the creation transition must have from_state=None")
    _check_matches(execution, transition)


def check_apply(updated: Execution, transition: Transition) -> None:
    """Validate arguments to :meth:`ExecutionStore.apply`. Raises ``ValueError``."""
    if updated.version < 2:
        raise ValueError("an updated execution must have version >= 2")
    if transition.from_state is None:
        raise ValueError("an update transition must have a from_state")
    _check_matches(updated, transition)


def _check_matches(execution: Execution, transition: Transition) -> None:
    if transition.execution_id != execution.id:
        raise ValueError("transition belongs to a different execution")
    if transition.to_state is not execution.state:
        raise ValueError("transition target does not match the execution state")
    if transition.at != execution.updated_at:
        raise ValueError("transition time must equal the execution's updated_at")


# -- the commit rules, shared by every adapter -------------------------------------------


def check_fence(current: Fence | None, given: Fence | None) -> None:
    """Raise ``FencingError`` unless ``given`` may write to a store owned by ``current``."""
    if current is None:
        if given is not None:
            raise FencingError("the store has no owner; claim it before writing with a fence")
        return
    if given != current:
        raise FencingError(
            "this coordinator's ownership was superseded or never held; "
            "it must perform no further side effects"
        )


def check_commit(
    fence: Fence,
    changes: Commit,
    *,
    execution: Callable[[str], Execution | None],
    operation: Callable[[str], Operation | None],
    resource: Callable[[str], Resource | None],
) -> None:
    """Validate a commit against the stored records (looked up through the callables).

    Call inside the adapter's transaction, after :func:`check_fence`. Raises
    ``ConcurrencyConflictError`` for stale versions, ``LedgerIntegrityError``
    for anything inconsistent, ``ValueError`` for malformed input.
    """
    created = {e.id: e for e, _ in changes.created}
    applied = {e.id: e for e, _ in changes.applied}
    for e, t in changes.created:
        check_create(e, t)
    for e, t in changes.applied:
        check_apply(e, t)

    def current_execution(execution_id: str) -> Execution:
        found = applied.get(execution_id) or created.get(execution_id) or execution(execution_id)
        if found is None:
            raise ExecutionNotFoundError(execution_id)
        return found

    final_ops: dict[str, Operation] = {}
    for new in changes.operations:
        if new.epoch != fence.epoch:
            raise LedgerIntegrityError(f"operation {new.key} carries another epoch")
        owner = current_execution(new.execution_id)
        if new.attempt != owner.attempt:
            raise LedgerIntegrityError(
                f"operation {new.key} is for attempt {new.attempt}, "
                f"but {owner.id} is at attempt {owner.attempt}"
            )
        stored = operation(new.key)
        if new.version == 1:
            if stored is not None:
                raise ConcurrencyConflictError(f"operation {new.key} already exists")
            if new.status is not OperationStatus.INTENDED:
                raise LedgerIntegrityError(f"operation {new.key} must start as intended")
        else:
            _check_operation_update(stored, new)
        final_ops[new.key] = new

    for new_resource in changes.resources:
        owner = current_execution(new_resource.execution_id)
        stored_resource = resource(new_resource.key)
        if new_resource.version == 1:
            if stored_resource is not None:
                raise ConcurrencyConflictError(f"resource {new_resource.key} already exists")
            if new_resource.status is not ResourceStatus.INTENDED:
                raise LedgerIntegrityError(f"resource {new_resource.key} must start as intended")
            if new_resource.attempt != owner.attempt:
                raise LedgerIntegrityError(f"resource {new_resource.key} is for another attempt")
        else:
            _check_resource_update(stored_resource, new_resource)

    for execution_id, version in changes.expected:
        if current_execution(execution_id).version != version:
            raise ConcurrencyConflictError(
                f"execution {execution_id} is no longer at version {version}; "
                "this worker's view is stale"
            )

    # A candidate exists only together with the freeze that produced it, on
    # every path into the store: never on a new execution, and never changed
    # (or re-based) by any transition without a matching successful freeze.
    for e, _ in changes.created:
        if e.candidate_revision is not None:
            raise LedgerIntegrityError(f"new execution {e.id} cannot already carry a candidate")
    for e, _ in changes.applied:
        before = execution(e.id)
        if e.candidate_revision is not None and (
            before is None
            or (before.candidate_revision, before.base_revision)
            != (e.candidate_revision, e.base_revision)
        ):
            _check_frozen(e, final_ops, operation)


def _check_operation_update(stored: Operation | None, new: Operation) -> None:
    if stored is None:
        raise ConcurrencyConflictError(f"operation {new.key} does not exist")
    if stored.version != new.version - 1:
        raise ConcurrencyConflictError(
            f"operation {new.key} is at version {stored.version}, update expected {new.version - 1}"
        )
    if (stored.kind, stored.execution_id, stored.attempt, stored.created_at, stored.intent) != (
        new.kind,
        new.execution_id,
        new.attempt,
        new.created_at,
        new.intent,
    ):
        raise LedgerIntegrityError(f"operation {new.key}: kind, owner and intent are immutable")
    if not stored.status.can_become(new.status):
        raise LedgerIntegrityError(
            f"operation {new.key} cannot move from {stored.status.value} to {new.status.value}"
        )


def _check_resource_update(stored: Resource | None, new: Resource) -> None:
    if stored is None:
        raise ConcurrencyConflictError(f"resource {new.key} does not exist")
    if stored.version != new.version - 1:
        raise ConcurrencyConflictError(
            f"resource {new.key} is at version {stored.version}, update expected {new.version - 1}"
        )
    if (stored.owner_token, stored.created_at) != (new.owner_token, new.created_at):
        raise LedgerIntegrityError(f"resource {new.key}: owner and creation are immutable")
    try:
        stored.moved(new.status, now=new.updated_at, identity=new.identity)
    except ValueError as error:
        raise LedgerIntegrityError(f"resource {new.key}: {error}") from None
    if stored.identity is not None and new.identity != stored.identity:
        raise LedgerIntegrityError(f"resource {new.key}: its identity never changes")


def _check_frozen(
    execution: Execution,
    in_commit: dict[str, Operation],
    stored: Callable[[str], Operation | None],
) -> None:
    """A candidate is recorded only with the freeze that produced it (ADR-0020)."""
    prefix = f"{OperationKind.FREEZE_CANDIDATE.value}:{execution.id}:{execution.attempt}"
    freeze = in_commit.get(prefix) or stored(prefix)
    if (
        freeze is None
        or freeze.status is not OperationStatus.SUCCEEDED
        or freeze.result.get("revision") != execution.candidate_revision
        or freeze.result.get("base_revision") != execution.base_revision
        or not freeze.result.get("changed_paths_digest", "").startswith("sha256:")
    ):
        raise LedgerIntegrityError(
            f"{execution.id}: candidate {execution.candidate_revision} has no matching "
            "successful freeze in the journal"
        )
