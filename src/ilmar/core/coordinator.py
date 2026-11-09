# SPDX-License-Identifier: Apache-2.0
"""One coordinator pass: ``ilmar run --once`` (ADR-0021).

The coordinator owns the instance (the lock and the fence, ADR-0020); work is
per execution. A pass:

1. **recovers** every execution that is not finished, and every finished one
   with unsettled operations or live sandboxes (ADR-0022): each is driven
   from its durable state by a worker under its own lease. Executions that
   cannot be reconciled stay blocked on their own; nothing else waits;
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
import logging
import secrets
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from functools import partial

from ilmar.core.cancel_watcher import WATCH_INTERVAL_SECONDS
from ilmar.core.cancellation import cancellation_accepted
from ilmar.core.clock import NonDecreasingClock
from ilmar.core.components import Components
from ilmar.core.effects import NEVER_TRANSIENT, Effects
from ilmar.core.event_context import PassEvents, new_pass_id, require_failure_codes
from ilmar.core.journal import Journal
from ilmar.core.ownership import ExecutionLeases, Ownership
from ilmar.core.state_machine import ExecutionStateMachine
from ilmar.core.verification import Verifier
from ilmar.core.worker import (
    MAX_ATTEMPTS,
    ExecutionWorker,
    Outcome,
    WorkerResult,
    needs_attention,
)
from ilmar.domain.changes import ChangeRequest, ChangeRequestState
from ilmar.domain.errors import ConflictError, FencingError, ProviderError
from ilmar.domain.execution import Execution, ExecutionState
from ilmar.domain.ledger import (
    Commit,
    OperationKind,
    Resource,
    ResourceKind,
    ResourceStatus,
    operation_key,
)
from ilmar.domain.tasks import Task
from ilmar.ports.execution_store import ExecutionStore

_LOG = logging.getLogger(__name__)
S = ExecutionState


class ExitCode(enum.IntEnum):
    SUCCESS = 0
    FAILURE = 1
    BLOCKED = 2
    """Manual intervention is needed (ambiguous effect, quarantine, unfinished journal)."""
    CONFIGURATION = 3
    """The configuration is invalid or this coordinator does not own the instance."""


_CODES = {
    Outcome.PUBLISHED: ExitCode.SUCCESS,
    Outcome.CANCELLED: ExitCode.SUCCESS,
    Outcome.FAILED: ExitCode.FAILURE,
    Outcome.BLOCKED: ExitCode.BLOCKED,
}


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
        events: PassEvents | None = None,
        watch_interval_seconds: float = WATCH_INTERVAL_SECONDS,
    ) -> None:
        self._c = components
        self._effects = effects
        self._events = events or effects.events
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
            watch_interval_seconds=watch_interval_seconds,
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
        new_pass_id: Callable[[], str] = new_pass_id,
        watch_interval_seconds: float = WATCH_INTERVAL_SECONDS,
    ) -> Coordinator:
        """Wire the journal, the fenced Verifier and the effects for ``components``.

        Every persisted timestamp comes from one non-decreasing clock (ADR-0022).
        """
        clock = NonDecreasingClock(clock)
        events = PassEvents(clock=clock, new_id=new_pass_id)  # one per process, a pass id per pass
        journal = Journal(
            store=components.execution_store,
            ownership=ownership,
            clock=clock,
            events=events,
            always_definitive=NEVER_TRANSIENT,
        )
        verifier = Verifier(
            source_control=components.source_control,
            runtime=components.container_runtime,
            store=components.execution_store,
            layout=components.layout,
            policy=components.verification,
            instance_id=components.instance_id,
            clock=clock,
            fence=ownership.fence,
            events=events,
        )
        effects = Effects(components=components, journal=journal, verifier=verifier, clock=clock)
        coordinator = cls(
            components=components,
            effects=effects,
            machine=ExecutionStateMachine(max_attempts=min(max_attempts, MAX_ATTEMPTS)),
            clock=clock,
            agent_timeout_seconds=agent_timeout_seconds,
            events=events,
            watch_interval_seconds=watch_interval_seconds,
        )
        if new_id is not None:
            coordinator._new_id = new_id
        if new_token is not None:
            coordinator._worker.new_token = new_token
        return coordinator

    @property
    def _store(self) -> ExecutionStore:
        return self._c.execution_store

    def run_once(self) -> PassReport:
        self._events.begin_pass()  # every event this pass writes carries its id
        results = list(self.recover())
        completed = self.follow_up_change_requests(results)
        task = self._pick()
        if task is not None:
            execution, transition = self._machine.start(
                task.ref, execution_id=self._new_id(), now=self._clock()
            )
            fence = self._effects.fence
            created, context = (
                Commit(created=((execution, transition),)),
                self._events.context(fence),
            )
            require_failure_codes(created, context)
            self._store.commit(fence, created, context=context)
            result = self._isolated(execution.id, lambda: self.dispatch(execution.id, task))
            if result is not None:
                results.append(result)
        if not results:
            return PassReport(ExitCode.SUCCESS, "no eligible task", completed_tasks=completed)
        code = max(_CODES[r.outcome] for r in results)
        summary = "; ".join(r.line() for r in results)
        return PassReport(code, summary, tuple(results), completed)

    def recover(self) -> tuple[WorkerResult, ...]:
        """Drive every execution that still has work, from its durable state (ADR-0022)."""
        active = self._store.list_in_states({s for s in ExecutionState if not s.is_terminal})
        finished = [
            e
            for e in self._store.list_in_states({s for s in ExecutionState if s.is_terminal})
            if needs_attention(self._store, e)
        ]
        results = []
        for execution in sorted([*active, *finished], key=lambda e: (e.created_at, e.id)):
            result = self._isolated(execution.id, partial(self._recover_one, execution))
            if result is not None:
                results.append(result)
        results.extend(self.settle_stranded_requests())
        return tuple(results)

    def settle_stranded_requests(self) -> list[WorkerResult]:
        """Requests left for executions that are already terminal and need no other
        work, so no worker visits them: recorded ``already-terminal`` and cleaned
        up by the worker's applier under the execution's lease (ADR-0025 §3).
        Live executions are left to their own worker; unknown ids are left alone.
        Only an unexpected error produces a result line (BLOCKED, never silenced)."""
        failures = []
        for execution_id in self._worker.stranded_requests():
            execution = self._store.get(execution_id)
            if execution is None or not execution.state.is_terminal:
                continue

            def settle(execution_id: str = execution_id) -> None:
                self._worker.apply_stranded(execution_id)

            failure = self._isolated(execution_id, partial(_none_after, settle))
            if failure is not None:
                failures.append(failure)
        return failures

    def _recover_one(self, execution: Execution) -> WorkerResult | None:
        try:
            task = self._c.task_provider.get(execution.task)  # read only
        except ProviderError:
            return WorkerResult(execution.id, Outcome.BLOCKED, "the tracker could not be read",
                                code="transient-provider-error")  # fmt: skip
        return self.dispatch(execution.id, task)

    def _isolated(
        self, execution_id: str, work: Callable[[], WorkerResult | None]
    ) -> WorkerResult | None:
        """One execution's boundary: its failure blocks it alone (ADR-0022, decision 4).

        Everything a worker expects is handled inside it. What reaches here is
        unexpected (an adapter or Ilmar bug). It is logged and reported as
        blocked, never silenced: the pass still exits 2, and the next pass tries
        again from durable state, which the journal keeps consistent. Losing
        ownership stops the whole pass.
        """
        try:
            return work()
        except FencingError:
            raise  # this process no longer owns the instance: stop, never skip
        except ConflictError:
            return None  # another worker holds this execution's lease
        except Exception as error:  # the per-execution boundary
            _LOG.exception("execution %s stopped by an unexpected error", execution_id)
            detail = f"unexpected {type(error).__name__}: {error}"
            return WorkerResult(
                execution_id, Outcome.BLOCKED, detail[:300], code="unexpected-error"
            )

    def dispatch(self, execution_id: str, task: Task | None) -> WorkerResult:
        """Run one execution's worker. Safe to call concurrently for different executions."""
        return self._worker.run(execution_id, task)

    # -- task completion --------------------------------------------------------------

    def follow_up_change_requests(
        self, problems: list[WorkerResult] | None = None
    ) -> tuple[str, ...]:
        """Release the claim of tasks whose change request was merged or closed.

        A change request whose follow-up fails unexpectedly is reported in
        ``problems`` and does not stop the others (ADR-0022, decision 4).
        """
        completed: list[str] = []
        open_requests = [  # RELEASING: an earlier pass was interrupted while releasing
            r
            for r in self._store.resources(
                statuses={ResourceStatus.ACTIVE, ResourceStatus.RELEASING}
            )
            if r.kind is ResourceKind.CHANGE_REQUEST
        ]
        for resource in open_requests:
            blocked = self._isolated(
                resource.execution_id, partial(self._follow_up_one, resource, completed)
            )
            if blocked is not None and problems is not None:
                problems.append(blocked)
        return tuple(completed)

    def _follow_up_one(self, resource: Resource, completed: list[str]) -> None:
        execution = self._store.get(resource.execution_id)
        if execution is None or execution.state is not S.SUCCEEDED or not resource.identity:
            return
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
            return  # unknown for now; asked again next pass
        if state is ChangeRequestState.OPEN:
            return
        merged = state is ChangeRequestState.MERGED
        with self.leases.acquire(execution.id) as lease:  # ConflictError: asked again next pass
            self._effects.stop_tracking_change_request(
                execution,
                lease,
                resource=resource,
                note="merged: task complete" if merged else "closed without merging",
                step=state.value,
                observed="change-request-merged" if merged else "change-request-closed",
            )
            self._effects.post_update(
                execution,
                lease,
                step=state.value,
                message=(
                    "The change request was merged: this task is complete."
                    if merged
                    else "The change request was closed without merging; Ilmar released this task."
                ),
            )
        if merged:
            completed.append(execution.id)

    # -- choosing work ----------------------------------------------------------------

    def _pick(self) -> Task | None:
        """The first eligible task (by key) Ilmar is not already working on or done with.

        Never started again automatically: a task with an active execution;
        one with a published (SUCCEEDED) execution, whose change request
        exists, open or merged; one whose execution FAILED; and one whose
        execution was CANCELLED on request (ADR-0025 §3). Releasing such a
        task's claim makes it eligible on the tracker, but running it again
        would undo a decision: a failure classified as not retryable
        (ADR-0021), or a person's cancellation. A human decides (#12).
        """
        finished = {e.task for e in self._store.list_in_states({S.SUCCEEDED, S.FAILED})}
        finished |= {
            e.task
            for e in self._store.list_in_states({S.CANCELLED})
            if cancellation_accepted(self._store, e.id)
        }
        for task in sorted(self._c.task_provider.list_eligible(), key=lambda t: t.ref.key):
            if task.ref in finished or self._store.active_for_task(task.ref) is not None:
                continue
            return task
        return None


def _none_after(work: Callable[[], object]) -> WorkerResult | None:
    work()
    return None
