# SPDX-License-Identifier: Apache-2.0
"""One coordinator pass: ``ilmar run --once`` (ADR-0021).

The coordinator owns the instance (the lock and the fence, ADR-0020); work is
per execution. A pass:

1. refuses to start anything while the journal has unfinished operations,
   because recovery (#11c) must settle them first (exit 2);
2. follows up published executions: a change request that was **merged**
   completes the task, one **closed** without merging abandons it. Either
   way Ilmar's claim is released. While it is open the task stays claimed;
3. picks at most one eligible task and drives it with a worker.

Execution completion and task completion differ (ADR-0021). An execution is
SUCCEEDED when its verified candidate is published as a change request. The
task completes only when that change request is merged; the change-request
resource is then RELEASED with the note ``merged``.

Nothing here serialises executions: dispatch goes through
:meth:`Coordinator.dispatch`, which a later version can run in a bounded
pool, since each worker holds only its own execution's lease.
"""

from __future__ import annotations

import enum
import secrets
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime

from ilmar.core.components import Components
from ilmar.core.effects import Effects
from ilmar.core.journal import Journal
from ilmar.core.ownership import ExecutionLeases, Ownership
from ilmar.core.state_machine import ExecutionStateMachine
from ilmar.core.verification import Verifier
from ilmar.core.worker import MAX_ATTEMPTS, ExecutionWorker, Outcome, WorkerResult
from ilmar.domain.changes import ChangeRequest, ChangeRequestState
from ilmar.domain.errors import ProviderError
from ilmar.domain.execution import ExecutionState
from ilmar.domain.ledger import OperationKind, ResourceKind, ResourceStatus, operation_key
from ilmar.domain.tasks import Task
from ilmar.ports.execution_store import ExecutionStore

S = ExecutionState


class ExitCode(enum.IntEnum):
    SUCCESS = 0
    FAILURE = 1
    BLOCKED = 2
    """Manual intervention is needed (ambiguous effect, quarantine, unfinished journal)."""
    CONFIGURATION = 3
    """The configuration is invalid or this coordinator does not own the instance."""


@dataclass(frozen=True, slots=True)
class PassReport:
    code: ExitCode
    summary: str
    results: tuple[WorkerResult, ...] = ()
    completed_tasks: tuple[str, ...] = field(default_factory=tuple)
    """Execution ids whose change request was merged during this pass."""


class Coordinator:
    def __init__(
        self,
        *,
        components: Components,
        effects: Effects,
        machine: ExecutionStateMachine,
        clock: Callable[[], datetime],
        agent_timeout_seconds: float,
        new_id: Callable[[], str] = lambda: "x" + secrets.token_hex(6),
        new_token: Callable[[], str] = lambda: secrets.token_hex(16),
    ) -> None:
        self._c = components
        self._effects = effects
        self._machine = machine
        self._clock = clock
        self._new_id = new_id
        self.leases = ExecutionLeases()
        self._worker = ExecutionWorker(
            components=components,
            effects=effects,
            leases=self.leases,
            machine=machine,
            clock=clock,
            agent_timeout_seconds=agent_timeout_seconds,
            new_token=new_token,
        )

    @classmethod
    def create(
        cls,
        components: Components,
        ownership: Ownership,
        *,
        max_attempts: int,
        agent_timeout_seconds: float,
        clock: Callable[[], datetime],
        new_id: Callable[[], str] | None = None,
        new_token: Callable[[], str] | None = None,
    ) -> Coordinator:
        """Wire the journal, the fenced Verifier and the effects for ``components``."""
        journal = Journal(store=components.execution_store, ownership=ownership, clock=clock)
        verifier = Verifier(
            source_control=components.source_control,
            runtime=components.container_runtime,
            store=components.execution_store,
            layout=components.layout,
            policy=components.verification,
            instance_id=components.instance_id,
            clock=clock,
            fence=ownership.fence,
        )
        effects = Effects(components=components, journal=journal, verifier=verifier, clock=clock)
        extra: dict[str, Callable[[], str]] = {}
        if new_id is not None:
            extra["new_id"] = new_id
        if new_token is not None:
            extra["new_token"] = new_token
        return cls(
            components=components,
            effects=effects,
            machine=ExecutionStateMachine(max_attempts=min(max_attempts, MAX_ATTEMPTS)),
            clock=clock,
            agent_timeout_seconds=agent_timeout_seconds,
            **extra,
        )

    @property
    def _store(self) -> ExecutionStore:
        return self._c.execution_store

    def run_once(self) -> PassReport:
        unfinished = self._store.unfinished_operations()
        if unfinished:
            keys = ", ".join(o.key for o in unfinished[:10])
            return PassReport(
                ExitCode.BLOCKED,
                f"{len(unfinished)} unfinished operation(s) need reconciliation first: {keys}",
            )
        completed = self.follow_up_change_requests()
        task = self._pick()
        if task is None:
            return PassReport(ExitCode.SUCCESS, "no eligible task", completed_tasks=completed)
        execution, transition = self._machine.start(
            task.ref, execution_id=self._new_id(), now=self._clock()
        )
        self._store.create(execution, transition, fence=self._effects.fence)
        result = self.dispatch(execution.id, task)
        code = {
            Outcome.PUBLISHED: ExitCode.SUCCESS,
            Outcome.CANCELLED: ExitCode.SUCCESS,
            Outcome.FAILED: ExitCode.FAILURE,
            Outcome.BLOCKED: ExitCode.BLOCKED,
        }[result.outcome]
        return PassReport(
            code,
            f"{execution.id}: {result.outcome.value} {result.detail}".strip(),
            (result,),
            completed,
        )

    def dispatch(self, execution_id: str, task: Task) -> WorkerResult:
        """Run one execution's worker. Safe to call concurrently for different executions."""
        return self._worker.run(execution_id, task)

    # -- task completion --------------------------------------------------------------

    def follow_up_change_requests(self) -> tuple[str, ...]:
        """Release the claim of tasks whose change request was merged or closed."""
        completed: list[str] = []
        open_requests = [
            r
            for r in self._store.resources(statuses={ResourceStatus.ACTIVE})
            if r.kind is ResourceKind.CHANGE_REQUEST
        ]
        for resource in open_requests:
            execution = self._store.get(resource.execution_id)
            if execution is None or execution.state is not S.SUCCEEDED or not resource.identity:
                continue
            upsert = self._store.operation(
                operation_key(OperationKind.UPSERT_CHANGE_REQUEST, execution.id, execution.attempt)
            )
            request = ChangeRequest(
                repository=self._c.target.repository,
                key=resource.identity,
                url=upsert.result.get("url", "") if upsert else "",
                source_branch=resource.name,
                target_branch=self._c.target.base_branch,
            )
            try:
                state = self._c.source_control.change_request_state(request)  # read only
            except ProviderError:
                continue  # unknown for now; asked again next pass
            if state is ChangeRequestState.OPEN:
                continue
            merged = state is ChangeRequestState.MERGED
            with self.leases.acquire(execution.id) as lease:
                self._effects.stop_tracking_change_request(
                    execution,
                    lease,
                    resource=resource,
                    note="merged: task complete" if merged else "closed without merging",
                    step=state.value,
                )
                self._effects.post_update(
                    execution,
                    lease,
                    step=state.value,
                    message=(
                        "The change request was merged: this task is complete."
                        if merged
                        else "The change request was closed without merging; "
                        "Ilmar released this task."
                    ),
                )
            if merged:
                completed.append(execution.id)
        return tuple(completed)

    # -- choosing work ----------------------------------------------------------------

    def _pick(self) -> Task | None:
        """The first eligible task (by key) Ilmar is not already working on or done with.

        A task with an active execution, or with a published (SUCCEEDED)
        execution, is never started again automatically: its change request
        exists, open or merged.
        """
        published = {e.task for e in self._store.list_in_states({S.SUCCEEDED})}
        for task in sorted(self._c.task_provider.list_eligible(), key=lambda t: t.ref.key):
            if task.ref in published or self._store.active_for_task(task.ref) is not None:
                continue
            return task
        return None
