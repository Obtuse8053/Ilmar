# SPDX-License-Identifier: Apache-2.0
"""Applying cancellation requests (ADR-0025 §1, §1a and §4).

The worker that holds an execution's lease is the only component that applies
a request durably. :meth:`Cancellations.apply`:

1. reads the request through the one validation routine
   (:func:`~ilmar.core.cancel_inbox.read_cancel_request`), or takes the
   request the watcher already validated;
2. decides (``accepted``, ``too-late``, ``already-terminal`` or
   ``duplicate``) and records ``execution.cancel_requested`` in one fenced,
   lease-checked commit, once per request id;
3. cleans up rename first, verify after: the request name is moved to a
   private name, checked there, and only a match is deleted. Anything else
   is moved to ``rejected/`` and reported, never deleted.

An entry that fails validation is moved aside the same way and reported; it is
never acted on. Leftovers of a crash (``.applying-…``) are settled the next
time.

Acting on an accepted cancellation (stopping, the CANCELLED transition) is the
coordinator's part (#16, part 3). Nothing here performs an effect.
"""

from __future__ import annotations

import contextlib
import os
from collections.abc import Callable
from dataclasses import dataclass, field

from ilmar.core import cancel_inbox as inbox_fs
from ilmar.core.cancel_inbox import CancelRequest, Reading, UnsafeInboxError
from ilmar.core.journal import Journal
from ilmar.core.layout import WorkspaceLayout
from ilmar.core.ownership import ExecutionLease
from ilmar.domain.events import Actor, ActorKind, Event, EventType
from ilmar.domain.execution import Execution
from ilmar.domain.ledger import Commit, OperationKind
from ilmar.ports.execution_store import ExecutionStore

REASON = "cancel-requested"


@dataclass(frozen=True, slots=True)
class Applied:
    """What one :meth:`Cancellations.apply` did; ``reports`` are for the operator."""

    request: CancelRequest | None = None
    decision: str | None = None
    recorded: bool = False  # recorded now (False: nothing, or recorded earlier)
    reports: tuple[str, ...] = field(default_factory=tuple)


class Cancellations:
    """The coordinator's side of the request inbox, used by the lease holder."""

    def __init__(
        self,
        *,
        store: ExecutionStore,
        journal: Journal,
        layout: WorkspaceLayout,
        uid: int | None = None,
        new_token: Callable[[], str] = inbox_fs.new_token,
        window: Callable[[str], None] = lambda name: None,
    ) -> None:
        self._store = store
        self._journal = journal
        self._root = layout.coordinator_root
        self._uid = uid
        self._new_token = new_token
        self._window = window  # tests run a concurrent change in each window

    # -- reading -------------------------------------------------------------------------

    def read(self, execution_id: str) -> Reading:
        """The validation routine, for the watcher and the worker alike. Never writes."""
        try:
            fd = inbox_fs.open_inbox(self._root, create=False, uid=self._uid)
        except FileNotFoundError:
            return Reading()
        except UnsafeInboxError as error:
            return Reading(problem=f"the inbox is unsafe: {error}")
        try:
            return inbox_fs.read_cancel_request(fd, execution_id, uid=self._uid)
        finally:
            os.close(fd)

    def accepted(self, execution_id: str) -> bool:
        """Whether a cancellation of this execution was accepted (durable state)."""
        return any(e.data.get("decision") == "accepted" for e in self._requests(execution_id))

    def decide(self, execution: Execution) -> str:
        if execution.state.is_terminal:
            return "already-terminal"
        if self.accepted(execution.id):
            return "duplicate"
        if any(o.kind is OperationKind.PUBLISH_REVISION
               for o in self._store.operations(execution.id)):  # fmt: skip
            return "too-late"  # a push may have happened: publish exactly once (I1)
        return "accepted"

    # -- applying ------------------------------------------------------------------------

    def apply(
        self,
        execution: Execution,
        lease: ExecutionLease,
        *,
        validated: CancelRequest | None = None,
    ) -> Applied:
        """Apply this execution's pending request, if any; see the module docstring.

        ``validated`` is the request the watcher validated and that stopped a run:
        it is recorded as it was read, whatever the name holds now.
        """
        lease.check(execution.id)
        try:
            fd = inbox_fs.open_inbox(self._root, create=False, uid=self._uid)
        except FileNotFoundError:
            return self._record_only(execution, lease, validated)
        except UnsafeInboxError as error:
            applied = self._record_only(execution, lease, validated)
            return Applied(applied.request, applied.decision, applied.recorded,
                           (*applied.reports, f"the inbox is unsafe: {error}"))  # fmt: skip
        try:
            return self._apply(fd, execution, lease, validated)
        finally:
            os.close(fd)

    def _record_only(
        self, execution: Execution, lease: ExecutionLease, validated: CancelRequest | None
    ) -> Applied:
        if validated is None:
            return Applied()
        decision, recorded = self._record(execution, lease, validated)
        return Applied(validated, decision, recorded)

    def _apply(
        self, fd: int, execution: Execution, lease: ExecutionLease, validated: CancelRequest | None
    ) -> Applied:
        reports = self._settle_leftovers(fd, execution)
        request = validated
        if request is None:
            reading = inbox_fs.read_cancel_request(fd, execution.id, uid=self._uid)
            if reading.problem is not None:
                return Applied(reports=(*reports, *self._move_invalid(fd, execution, reading)))
            if reading.request is None:
                return Applied(reports=tuple(reports))
            request = reading.request
        self._window("validated")
        decision, recorded = self._record(execution, lease, request)
        self._window("recorded")
        reports += self._clean(fd, execution, request)
        return Applied(request, decision, recorded, tuple(reports))

    def _record(
        self, execution: Execution, lease: ExecutionLease, request: CancelRequest
    ) -> tuple[str | None, bool]:
        """Record the request once (idempotent by request id); its decision."""
        for earlier in self._requests(execution.id):
            if earlier.data.get("request") == request.request_id:
                return str(earlier.data.get("decision")), False
        decision = self.decide(execution)
        fence = self._journal.fence
        context = self._journal.events.context(fence)
        event = Event.create(
            EventType.EXECUTION_CANCEL_REQUESTED,
            at=context.at,
            instance=context.instance,
            epoch=context.epoch,
            actor=Actor(ActorKind.HUMAN, request.actor),
            pass_id=context.pass_id,
            execution=execution.id,
            reason=REASON,
            data={"request": request.request_id, "decision": decision},
        )
        self._journal.record(Commit(extra_events=(event,)), lease=lease, execution_id=execution.id)
        return decision, True

    # -- cleanup: rename first, verify after (ADR-0025 §1a) -------------------------------

    def _clean(self, fd: int, execution: Execution, request: CancelRequest) -> list[str]:
        private = inbox_fs.take(fd, execution.id, token=self._new_token())
        self._window("taken")
        reports: list[str] = []
        if private is not None:
            identity = inbox_fs.identify(fd, private)
            self._window("verified")
            if inbox_fs.matches(identity, request):
                inbox_fs.remove(fd, private)  # exactly what was verified, at a private name
            else:
                reports.append(self._preserve(fd, execution, private, "after it was validated"))
        self._remove_companion(fd, execution, request)
        return reports

    def _remove_companion(self, fd: int, execution: Execution, request: CancelRequest) -> None:
        companion = inbox_fs.temporary_name(execution.id, request.request_id)
        identity = inbox_fs.identify(fd, companion)
        if identity is not None and (identity.device, identity.inode) == (
            request.device,
            request.inode,
        ):
            private = inbox_fs.applying_name(execution.id, self._new_token())
            with contextlib.suppress(FileNotFoundError):
                os.rename(companion, private, src_dir_fd=fd, dst_dir_fd=fd)
                if inbox_fs.matches(inbox_fs.identify(fd, private), request):
                    inbox_fs.remove(fd, private)
                else:
                    self._preserve(fd, execution, private, "in place of a temporary file")

    def _move_invalid(self, fd: int, execution: Execution, reading: Reading) -> list[str]:
        private = inbox_fs.take(fd, execution.id, token=self._new_token())
        if private is None:
            return []
        return [self._preserve(fd, execution, private, f"invalid: {reading.problem}")]

    def _preserve(self, fd: int, execution: Execution, private: str, why: str) -> str:
        """Move an entry the worker did not verify to ``rejected/``; never delete it."""
        aside = inbox_fs.set_aside(fd, private, execution.id, token=self._new_token())
        again = inbox_fs.read_rejected(fd, aside, execution.id, uid=self._uid)
        if again.request is not None:
            request_id = again.request.request_id
            return (
                f"{execution.id}: a valid request ({request_id}) was moved to {aside} "
                f"by a concurrent change {why}; it was not applied: resubmit"
            )
        return f"{execution.id}: an entry was moved to {aside} ({why})"

    def _settle_leftovers(self, fd: int, execution: Execution) -> list[str]:
        """``.applying-…`` names a crash left: unlinked if their request is recorded,
        otherwise preserved in ``rejected/``. Temporary files are inert; they are
        removed once the execution is terminal or a request was applied."""
        applying, temporary = inbox_fs.leftovers(fd, execution.id)
        recorded = {str(e.data.get("request")) for e in self._requests(execution.id)}
        reports: list[str] = []
        for name in applying:
            reading = inbox_fs.read_entry(fd, name, execution.id, inbox=fd, uid=self._uid)
            if reading.request is not None and reading.request.request_id in recorded:
                inbox_fs.remove(fd, name)
            else:
                reports.append(self._preserve(fd, execution, name, "left by a crash"))
        current = self._store.get(execution.id)
        finished = current is not None and current.state.is_terminal
        if temporary and (finished or self.accepted(execution.id)):
            for name in temporary:
                private = inbox_fs.applying_name(execution.id, self._new_token())
                with contextlib.suppress(FileNotFoundError):
                    os.rename(name, private, src_dir_fd=fd, dst_dir_fd=fd)
                    inbox_fs.remove(fd, private)  # an inert temporary file, never a request
        return reports

    def _requests(self, execution_id: str) -> list[Event]:
        return [
            e
            for e in self._store.events(execution_id=execution_id)
            if e.type is EventType.EXECUTION_CANCEL_REQUESTED
        ]
