# SPDX-License-Identifier: Apache-2.0
"""Fencing, the operation journal and the resource ledger (ADR-0012, ADR-0020).

* A :class:`Fence` names the coordinator that owns a store: its instance id
  and an *epoch* the store increments each time a coordinator takes
  ownership. Every write carries the fence; a superseded fence is refused.
* An :class:`Operation` records the *intent* to perform one external side
  effect before it happens, then its outcome. Its ``key`` is the idempotency
  key: the same logical step always derives the same key, so a replay after a
  crash finds the earlier record instead of acting twice.
* A :class:`Resource` tracks something Ilmar creates outside its store (a
  workspace, a container, a branch) from before its creation to its release.
* A :class:`Commit` bundles everything one atomic, fenced write changes.
"""

from __future__ import annotations

import enum
import hashlib
import json
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from types import MappingProxyType
from typing import TYPE_CHECKING

from ilmar.domain.execution import Execution, Transition

if TYPE_CHECKING:  # events imports this module; the store validates extra events
    from ilmar.domain.events import Event

_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
_DISCRIMINATOR = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
_MAX_FIELDS = 32
_MAX_VALUE = 2048


@dataclass(frozen=True, slots=True)
class Fence:
    """Proof of ownership: the coordinator ``instance_id`` at ``epoch``."""

    instance_id: str
    epoch: int

    def __post_init__(self) -> None:
        if not _IDENTIFIER.match(self.instance_id):
            raise ValueError("instance_id must be a safe identifier")
        if self.epoch < 1:
            raise ValueError("epoch starts at 1")


class OperationKind(enum.Enum):
    """External side effects the coordinator performs (ADR-0012 "sensitive operations").

    ``repeatable`` kinds are safe to perform again when an earlier attempt's
    outcome is unknown, because the effect itself is idempotent or checked
    (see ADR-0020, reconciliation table). Every other kind needs explicit
    reconciliation before anything else happens.
    """

    CLAIM_TASK = "claim-task"
    RELEASE_TASK = "release-task"
    POST_UPDATE = "post-update"
    CREATE_WORKSPACE = "create-workspace"
    CHECKOUT_BASE = "checkout-base"
    START_CONTAINER = "start-container"
    RUN_AGENT = "run-agent"
    """Not repeatable: it is costly and changes the workspace. An unknown
    outcome abandons the attempt (ADR-0021)."""
    STOP_CONTAINER = "stop-container"
    FREEZE_CANDIDATE = "freeze-candidate"
    VERIFY = "verify"
    PUBLISH_REVISION = "publish-revision"
    UPSERT_CHANGE_REQUEST = "upsert-change-request"
    DELETE_WORKSPACE = "delete-workspace"

    @property
    def repeatable(self) -> bool:
        return self in _REPEATABLE


_REPEATABLE = frozenset(
    {
        OperationKind.CLAIM_TASK,  # idempotent claim (ADR-0013)
        OperationKind.RELEASE_TASK,  # removes only Ilmar's own label
        OperationKind.STOP_CONTAINER,  # stopping a gone container is a no-op
        OperationKind.FREEZE_CANDIDATE,  # deterministic revision (port contract)
        OperationKind.UPSERT_CHANGE_REQUEST,  # keyed by head branch
    }
)


class OperationStatus(enum.Enum):
    INTENDED = "intended"
    """Recorded before the effect; the effect may or may not have happened."""
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    """The effect definitely did not happen (or definitely had no effect)."""
    AMBIGUOUS = "ambiguous"
    """The effect was attempted and its outcome is unknown."""

    @property
    def is_final(self) -> bool:
        return self in (OperationStatus.SUCCEEDED, OperationStatus.FAILED)

    def can_become(self, other: OperationStatus) -> bool:
        """Legal journal moves. ``INTENDED`` again means a repeatable effect is retried."""
        if other is OperationStatus.INTENDED:
            return not self.is_final
        return other in _OPERATION_MOVES[self]


_OPERATION_MOVES: Mapping[OperationStatus, frozenset[OperationStatus]] = MappingProxyType(
    {
        OperationStatus.INTENDED: frozenset(
            {OperationStatus.SUCCEEDED, OperationStatus.FAILED, OperationStatus.AMBIGUOUS}
        ),
        OperationStatus.AMBIGUOUS: frozenset({OperationStatus.SUCCEEDED, OperationStatus.FAILED}),
        OperationStatus.SUCCEEDED: frozenset(),
        OperationStatus.FAILED: frozenset(),
    }
)


def operation_key(kind: OperationKind, execution_id: str, attempt: int, step: str = "") -> str:
    """The idempotency key of one logical step: ``<kind>:<execution>:<attempt>[:<step>]``.

    ``step`` distinguishes repeated steps of one attempt (a verification run
    number, a container name, an update's purpose). The same inputs always
    give the same key.
    """
    if not _IDENTIFIER.match(execution_id):
        raise ValueError("execution_id must be a safe identifier")
    if attempt < 1:
        raise ValueError("attempt starts at 1")
    key = f"{kind.value}:{execution_id}:{attempt}"
    if step and kind is OperationKind.FREEZE_CANDIDATE:
        # One freeze per attempt: the store finds it by this exact key when a
        # candidate is recorded (ADR-0020).
        raise ValueError("a freeze has no step: there is exactly one per attempt")
    if step:
        if not _DISCRIMINATOR.match(step):
            raise ValueError("step must be a short identifier")
        key += f":{step}"
    return key


def _frozen_fields(values: Mapping[str, str], what: str) -> Mapping[str, str]:
    if len(values) > _MAX_FIELDS:
        raise ValueError(f"{what} has too many fields")
    for key, value in values.items():
        if not isinstance(key, str) or not isinstance(value, str):
            raise ValueError(f"{what} keys and values must be strings")
        if len(value) > _MAX_VALUE:
            raise ValueError(f"{what}.{key} is too long")
    return MappingProxyType(dict(sorted(values.items())))


@dataclass(frozen=True, slots=True)
class Operation:
    """The journal entry of one external side effect.

    ``intent`` and ``result`` hold small, non-secret strings (identifiers,
    revisions, names) that recovery needs. Never put credentials in them.
    """

    key: str
    execution_id: str
    attempt: int
    kind: OperationKind
    status: OperationStatus
    epoch: int
    version: int
    created_at: datetime
    updated_at: datetime
    intent: Mapping[str, str] = field(default_factory=dict)
    result: Mapping[str, str] = field(default_factory=dict)
    note: str = ""

    def __post_init__(self) -> None:
        if self.version < 1 or self.epoch < 1 or self.attempt < 1:
            raise ValueError("version, epoch and attempt start at 1")
        expected = f"{self.kind.value}:{self.execution_id}:{self.attempt}"
        if self.key != expected and not self.key.startswith(expected + ":"):
            raise ValueError("the key does not belong to this kind, execution and attempt")
        object.__setattr__(self, "intent", _frozen_fields(self.intent, "intent"))
        object.__setattr__(self, "result", _frozen_fields(self.result, "result"))
        if len(self.note) > _MAX_VALUE:
            raise ValueError("note is too long")

    @classmethod
    def intended(
        cls,
        kind: OperationKind,
        execution_id: str,
        attempt: int,
        *,
        fence: Fence,
        now: datetime,
        step: str = "",
        intent: Mapping[str, str] | None = None,
    ) -> Operation:
        return cls(
            key=operation_key(kind, execution_id, attempt, step),
            execution_id=execution_id,
            attempt=attempt,
            kind=kind,
            status=OperationStatus.INTENDED,
            epoch=fence.epoch,
            version=1,
            created_at=now,
            updated_at=now,
            intent=intent or {},
        )

    def moved(
        self,
        status: OperationStatus,
        *,
        fence: Fence,
        now: datetime,
        result: Mapping[str, str] | None = None,
        note: str = "",
    ) -> Operation:
        """This operation at ``status``, one version later. Raises on an illegal move."""
        if not self.status.can_become(status):
            raise ValueError(f"operation cannot move from {self.status.value} to {status.value}")
        return Operation(
            key=self.key,
            execution_id=self.execution_id,
            attempt=self.attempt,
            kind=self.kind,
            status=status,
            epoch=fence.epoch,
            version=self.version + 1,
            created_at=self.created_at,
            updated_at=now,
            intent=self.intent,
            result=self.result if result is None else result,
            note=note,
        )


class ResourceKind(enum.Enum):
    WORKSPACE = "workspace"
    VERIFICATION_CHECKOUT = "verification-checkout"
    CONTAINER = "container"
    NETWORK = "network"
    BRANCH = "branch"
    CHANGE_REQUEST = "change-request"


class ResourceStatus(enum.Enum):
    INTENDED = "intended"
    """Recorded before creation; it may or may not exist."""
    ACTIVE = "active"
    """Created, with its immutable identity recorded."""
    RELEASING = "releasing"
    """Removal started; it may or may not still exist."""
    RELEASED = "released"
    """Confirmed gone (or confirmed never created)."""
    QUARANTINED = "quarantined"
    """Ownership or removal could not be confirmed. Never reused, mounted or
    deleted automatically; a human resolves it."""


_RESOURCE_MOVES: Mapping[ResourceStatus, frozenset[ResourceStatus]] = MappingProxyType(
    {
        ResourceStatus.INTENDED: frozenset(
            {ResourceStatus.ACTIVE, ResourceStatus.RELEASED, ResourceStatus.QUARANTINED}
        ),
        ResourceStatus.ACTIVE: frozenset({ResourceStatus.RELEASING, ResourceStatus.QUARANTINED}),
        ResourceStatus.RELEASING: frozenset({ResourceStatus.RELEASED, ResourceStatus.QUARANTINED}),
        ResourceStatus.RELEASED: frozenset(),
        ResourceStatus.QUARANTINED: frozenset(),
    }
)


def resource_key(kind: ResourceKind, execution_id: str, attempt: int, name: str) -> str:
    if not _IDENTIFIER.match(execution_id) or attempt < 1:
        raise ValueError("execution_id must be a safe identifier and attempt >= 1")
    if not name or len(name) > 512 or "\0" in name:
        raise ValueError("resource name must be non-empty and short")
    return f"{kind.value}:{execution_id}:{attempt}:{name}"


@dataclass(frozen=True, slots=True)
class Resource:
    """One external resource, from intent to release.

    ``identity`` is the immutable identity captured once the resource exists
    (a container id, ``<device>:<inode>`` of a directory, a commit id), so a
    later operation can act on exactly that resource (ADR-0012 level 3).
    """

    key: str
    execution_id: str
    attempt: int
    kind: ResourceKind
    name: str
    owner_token: str
    status: ResourceStatus
    version: int
    created_at: datetime
    updated_at: datetime
    identity: str | None = None
    note: str = ""

    def __post_init__(self) -> None:
        if self.key != resource_key(self.kind, self.execution_id, self.attempt, self.name):
            raise ValueError("the key does not match the resource")
        if not self.owner_token:
            raise ValueError("a resource needs an owner token")
        if self.version < 1:
            raise ValueError("version starts at 1")
        if self.status is ResourceStatus.ACTIVE and not self.identity:
            raise ValueError("an active resource needs its identity")

    @classmethod
    def intended(
        cls,
        kind: ResourceKind,
        execution_id: str,
        attempt: int,
        name: str,
        *,
        owner_token: str,
        now: datetime,
    ) -> Resource:
        return cls(
            key=resource_key(kind, execution_id, attempt, name),
            execution_id=execution_id,
            attempt=attempt,
            kind=kind,
            name=name,
            owner_token=owner_token,
            status=ResourceStatus.INTENDED,
            version=1,
            created_at=now,
            updated_at=now,
        )

    def moved(
        self, status: ResourceStatus, *, now: datetime, identity: str | None = None, note: str = ""
    ) -> Resource:
        if status not in _RESOURCE_MOVES[self.status]:
            raise ValueError(f"resource cannot move from {self.status.value} to {status.value}")
        if self.identity is not None and identity is not None and identity != self.identity:
            raise ValueError("a resource's identity never changes")
        return Resource(
            key=self.key,
            execution_id=self.execution_id,
            attempt=self.attempt,
            kind=self.kind,
            name=self.name,
            owner_token=self.owner_token,
            status=status,
            version=self.version + 1,
            created_at=self.created_at,
            updated_at=now,
            identity=identity or self.identity,
            note=note,
        )


@dataclass(frozen=True, slots=True)
class Commit:
    """Everything one atomic, fenced write changes.

    * ``created`` / ``applied``: new executions and transitions of existing
      ones (the usual version rules apply);
    * ``operations`` / ``resources``: version 1 inserts a new record, any
      later version replaces the stored version - 1 (otherwise the whole
      commit fails with ``ConcurrencyConflictError``);
    * ``expected``: ``(execution_id, version)`` pairs that must hold once the
      commit's own changes are applied. A worker uses it to write an intent
      only while the execution is still the version it last wrote (ADR-0012
      fencing level 2).
    * ``reasons``: the stable reason code (ADR-0024 §4) of a changed row, keyed
      by its execution id, operation key or resource key: why an execution
      failed, was cancelled or waits to retry, why an operation failed, why a
      resource was quarantined. It travels with the change it explains.
    * ``extra_events``: events no row derives (ADR-0024 §9), such as
      ``execution.cancel_requested`` (ADR-0025). The store validates them and
      appends them after the commit's mandatory events, in the same
      transaction; they change no row.
    """

    created: tuple[tuple[Execution, Transition], ...] = ()
    applied: tuple[tuple[Execution, Transition], ...] = ()
    operations: tuple[Operation, ...] = ()
    resources: tuple[Resource, ...] = ()
    expected: tuple[tuple[str, int], ...] = ()
    reasons: Mapping[str, str] = field(default_factory=dict)
    extra_events: tuple[Event, ...] = ()

    def __post_init__(self) -> None:
        keys = [o.key for o in self.operations] + [r.key for r in self.resources]
        ids = [e.id for e, _ in (*self.created, *self.applied)]
        if len(set(keys)) != len(keys) or len(set(ids)) != len(ids):
            raise ValueError("a commit changes each record at most once")
        if unknown := sorted(set(self.reasons) - set(keys) - set(ids)):
            raise ValueError(f"reasons for records this commit does not change: {unknown}")
        object.__setattr__(self, "reasons", MappingProxyType(dict(self.reasons)))

    @property
    def empty(self) -> bool:
        return not (
            self.created or self.applied or self.operations or self.resources or self.extra_events
        )


# -- candidate integrity ---------------------------------------------------------------


def changed_paths_digest(paths: Iterable[str]) -> str:
    """A digest of a candidate's changed paths, independent of their order."""
    canonical = json.dumps(sorted(set(paths)), ensure_ascii=False, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()
