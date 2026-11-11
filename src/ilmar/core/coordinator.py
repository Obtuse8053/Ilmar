# SPDX-License-Identifier: Apache-2.0
"""One coordinator pass: ``ilmar run --once`` (ADR-0021).

The coordinator owns the instance (the lock and the fence, ADR-0020); work is
per execution. A pass:

1. **recovers** every execution that is not finished, and every finished one
   with unsettled operations or live sandboxes (ADR-0022): each is driven
   from its durable state by a worker under its own lease. Executions that
   cannot be reconciled stay blocked on their own; nothing else waits;
2. releases **orphaned claims**: this instance's authentic claims that no
   execution backs, exactly the observed claim object each (ADR-0026 §5);
3. follows up published executions: a change request that was **merged**
   completes the task, one **closed** without merging abandons it. Either
   way Ilmar's claim is released. While it is open the task stays claimed;
4. picks at most one eligible task and drives it with a worker.

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

from ilmar.core import tracker
from ilmar.core.cancel_watcher import WATCH_INTERVAL_SECONDS
from ilmar.core.cancellation import cancellation_accepted
from ilmar.core.clock import NonDecreasingClock
from ilmar.core.components import Components
from ilmar.core.effects import (
    NEVER_TRANSIENT,
    ClaimChangedError,
    Effects,
    claim_from_intent,
    orphan_step,
    stray_number,
)
from ilmar.core.event_context import (
    PassEvents,
    failure_code,
    new_pass_id,
    require_failure_codes,
)
from ilmar.core.journal import Journal
from ilmar.core.lifetimes import agent_sandbox_lifetime, execution_wall
from ilmar.core.ownership import ExecutionLeases, Ownership
from ilmar.core.state_machine import ExecutionStateMachine, RetryBackoff, clock_behind
from ilmar.core.verification import Verifier
from ilmar.core.worker import (
    MAX_ATTEMPTS,
    ExecutionWorker,
    Outcome,
    WorkerResult,
    blocked_for_maintainer,
    needs_attention,
)
from ilmar.domain.budget import BudgetLimits, Suspension, SuspensionReason, exhausted
from ilmar.domain.changes import ChangeRequest, ChangeRequestState
from ilmar.domain.errors import (
    ClockBehindError,
    ConflictError,
    FencingError,
    LedgerIntegrityError,
    ProviderError,
    SuspendedError,
)
from ilmar.domain.events import Event, EventType
from ilmar.domain.execution import Execution, ExecutionState
from ilmar.domain.ledger import (
    Commit,
    Operation,
    OperationKind,
    OperationStatus,
    Resource,
    ResourceKind,
    ResourceStatus,
    instance_operation_key,
    operation_key,
)
from ilmar.domain.tasks import ClearResult, HeldClaim, ReleaseResult, Task, TaskRef
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
    SUSPENDED = 78
    """No new work may start: a suspension is in force (ADR-0029 §3, ADR-0030)."""


_BUDGET_REASONS = frozenset({"budget-daily", "budget-monthly"})

_CODES = {
    Outcome.PUBLISHED: ExitCode.SUCCESS,
    Outcome.CANCELLED: ExitCode.SUCCESS,
    Outcome.FAILED: ExitCode.FAILURE,
    Outcome.BLOCKED: ExitCode.BLOCKED,
    Outcome.WAITING: ExitCode.SUCCESS,
    Outcome.SUSPENDED: ExitCode.SUSPENDED,
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
        retry_backoff: RetryBackoff,
        stall_timeout_seconds: float | None = None,
        budget: BudgetLimits | None = None,
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
            retry_backoff=retry_backoff,
            stall_timeout_seconds=stall_timeout_seconds,
            budget=budget,
            new_token=new_token,
            watch_interval_seconds=watch_interval_seconds,
        )
        self._budget = budget

    @classmethod
    def create(
        cls,
        components: Components,
        ownership: Ownership,
        *,
        max_attempts: int,
        agent_timeout_seconds: float,
        retry_backoff: RetryBackoff,
        clock: Callable[[], datetime],
        stall_timeout_seconds: float | None = None,
        execution_wall_seconds: float | None = None,
        budget: BudgetLimits | None = None,
        new_id: Callable[[], str] | None = None,
        new_token: Callable[[], str] | None = None,
        new_pass_id: Callable[[], str] = new_pass_id,
        watch_interval_seconds: float = WATCH_INTERVAL_SECONDS,
    ) -> Coordinator:
        """Wire the journal, the fenced Verifier and the effects for ``components``.

        Every persisted timestamp comes from one non-decreasing clock (ADR-0022).

        Every agent sandbox gets a finite, runtime-enforced lifetime (ADR-0029
        §1): ``execution_wall_seconds`` plus a grace period. The wall time must
        be at least ``agent_timeout_seconds``; omitted, it is derived from it.

        With ``budget``, every attempt is admitted against its allowances and
        charged its reservation as it starts (ADR-0029 §2).
        """
        lifetime = agent_sandbox_lifetime(
            execution_wall(agent_timeout_seconds, execution_wall_seconds)
        )
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
            scanner=components.secret_scanner,
        )
        effects = Effects(
            components=components,
            journal=journal,
            verifier=verifier,
            clock=clock,
            sandbox_lifetime_seconds=lifetime,
        )
        coordinator = cls(
            components=components,
            effects=effects,
            machine=ExecutionStateMachine(max_attempts=min(max_attempts, MAX_ATTEMPTS)),
            clock=clock,
            agent_timeout_seconds=agent_timeout_seconds,
            retry_backoff=retry_backoff,
            stall_timeout_seconds=stall_timeout_seconds,
            budget=budget,
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
        released = self.release_orphaned_claims(results)
        cleared = self.clear_stray_labels(results)
        completed = self.follow_up_change_requests(results)
        now = self._clock()
        behind = clock_behind(now, self._store.latest_update())
        suspended = self.suspend_if_exhausted(results, now)
        task = self._pick(not_now=cleared) if behind is None and suspended is None else None
        if suspended is not None:
            # ADR-0029 §3: no new claim and no new container while suspended.
            results.append(WorkerResult("admission", Outcome.SUSPENDED,
                                        f"new work is suspended ({suspended.reason.value})",
                                        code=suspended.reason.value))  # fmt: skip
        if behind is not None and self._pick(not_now=cleared) is not None:
            # ADR-0028 §3: no new execution while the clock is behind the store.
            results.append(WorkerResult("admission", Outcome.WAITING, behind))
        if task is not None:
            execution, transition = self._machine.start(
                task.ref, execution_id=self._new_id(), now=now
            )
            fence = self._effects.fence
            created, context = (
                Commit(created=((execution, transition),)),
                self._events.context(fence),
            )
            require_failure_codes(created, context)
            try:
                self._store.commit(fence, created, context=context)
            except ClockBehindError as refused:  # the store's own check: nothing written
                results.append(WorkerResult("admission", Outcome.WAITING, str(refused)))
                task = None
            except SuspendedError as refused:  # suspended since the check above
                results.append(WorkerResult("admission", Outcome.SUSPENDED, str(refused),
                                            code=refused.reason))  # fmt: skip
                task = None
        if task is not None:
            result = self._isolated(execution.id, lambda: self.dispatch(execution.id, task))
            if result is not None:
                results.append(result)
                self.suspend_if_exhausted([result], self._clock())
        orphans = f"released {released} orphaned claim(s)" if released else ""
        if not results:
            summary = orphans or "no eligible task"
            return PassReport(ExitCode.SUCCESS, summary, completed_tasks=completed)
        code = max(_CODES[r.outcome] for r in results)
        summary = "; ".join([*(r.line() for r in results), *([orphans] if orphans else [])])
        return PassReport(code, summary, tuple(results), completed)

    def suspend_if_exhausted(self, results: list[WorkerResult], now: datetime) -> Suspension | None:
        """The suspension in force after this check (ADR-0029 §2 and §3).

        A budget refusal from the store (a result with a ``budget-*`` code), or
        allowances that cannot take one more reservation, write a durable
        suspension, unless one is already in force. A crash before the write
        loses nothing: the next pass is refused again and suspends then."""
        current = self._store.suspension()
        if current is not None:
            return current
        reason: SuspensionReason | None = None
        for result in results:
            if result.outcome is Outcome.SUSPENDED and result.code in _BUDGET_REASONS:
                reason = SuspensionReason(result.code)
        if reason is None and self._budget is not None:
            reason = exhausted(
                self._budget, self._store.budget_totals(now), self._budget.reserve_micros
            )
        if reason is None:
            return None
        period = "daily" if reason is SuspensionReason.BUDGET_DAILY else "monthly"
        suspension = Suspension(
            reason, now, detail=f"the next reservation would exceed the {period} allowance"
        )
        fence = self._effects.fence
        try:
            self._store.commit(
                fence, Commit(suspend=suspension), context=self._events.context(fence)
            )
        except LedgerIntegrityError:
            if self._store.suspension() is None:  # not a concurrent suspension: a real fault
                raise
        return self._store.suspension()

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

    # -- orphaned claims (ADR-0026 §5) -----------------------------------------------

    def release_orphaned_claims(self, problems: list[WorkerResult]) -> int:
        """Release this instance's authentic claims that nothing in the store backs.

        After recovery, before the change-request follow-up and before picking
        new work. An interrupted release is performed again from its recorded
        intent first. Each release acts on exactly the observed claim object,
        revalidated against the store right before its effect. Returns how many
        were released; problems are reported, never silenced.
        """
        released = 0
        pending: list[HeldClaim] = [
            claim_from_intent(o.intent)
            for o in self._store.instance_operations()
            if o.kind is OperationKind.RELEASE_ORPHAN_CLAIM
            and o.status in (OperationStatus.INTENDED, OperationStatus.AMBIGUOUS)
        ]
        try:
            listed = list(self._c.task_provider.held_claims())  # read only
        except ProviderError as error:
            listed = []
            problems.append(WorkerResult("claims", Outcome.BLOCKED,
                                         f"held claims could not be read: {error}",
                                         code="transient-provider-error"))  # fmt: skip
        settled = {self._orphan_key(c) for c in pending}
        orphans = pending + [
            c for c in listed if not self._backed(c.ref) and self._orphan_key(c) not in settled
        ]
        for claim in orphans:
            existing = self._store.operation(self._orphan_key(claim))
            if existing is not None and existing.status.is_final:
                continue  # released, or changed: never retried as a delete
            known = {e.id for e in self._executions_of(claim.ref)}

            def revalidate(ref: TaskRef = claim.ref, known: set[str] = known) -> None:
                now = {e.id for e in self._executions_of(ref)}
                if now - known or self._backed(ref):
                    raise LedgerIntegrityError(f"{ref} is backed again; its claim is kept")

            try:
                result = self._effects.release_orphan_claim(claim, revalidate=revalidate)
            except FencingError:
                raise
            except (ClaimChangedError, LedgerIntegrityError, ProviderError) as error:
                problems.append(WorkerResult("claims", Outcome.BLOCKED,
                                             f"orphaned claim on {claim.ref}: {error}",
                                             code=failure_code(error)))  # fmt: skip
                continue
            if result is ReleaseResult.RELEASED:
                released += 1
        return released

    # -- stray claim labels (ADR-0027 §1) -----------------------------------------------

    def clear_stray_labels(self, problems: list[WorkerResult]) -> frozenset[TaskRef]:
        """Two passes per removal: resume intents recorded in an **earlier** pass
        (revalidate, then a fresh ``clear_label``), then record new intents for the
        stray labels listed now. Returns the tasks whose label went in this pass;
        none of them is picked in it. Nothing here is an input to eligibility."""
        cleared: set[TaskRef] = set()
        current = self._events.pass_id
        for operation in self._stray_operations(unfinished=True):
            intended = self._intent_event(operation.key)
            if intended is None or intended.pass_id in (None, current):
                continue  # never in the pass of its own intent
            ref = TaskRef(*operation.intent["task"].split(":", 1))
            seq = intended.seq or 0

            def revalidate(ref: TaskRef = ref, seq: int = seq) -> None:
                if self._backed(ref) or self._created_after(ref, seq):
                    raise LedgerIntegrityError(f"{ref} is backed; its label is kept")

            try:
                result = self._effects.clear_stray_label(
                    ref, n=stray_number(operation.key), revalidate=revalidate
                )
            except FencingError:
                raise
            except (LedgerIntegrityError, ProviderError) as error:
                problems.append(WorkerResult("claims", Outcome.BLOCKED,
                                             f"stray label on {ref}: {error}",
                                             code=failure_code(error)))  # fmt: skip
                continue
            if result is ClearResult.CLEARED:
                cleared.add(ref)
        try:
            listed = list(self._c.task_provider.stray_labels())  # read only
        except ProviderError as error:
            problems.append(WorkerResult("claims", Outcome.BLOCKED,
                                         f"stray labels could not be read: {error}",
                                         code="transient-provider-error"))  # fmt: skip
            return frozenset(cleared)
        for ref in listed:
            mine = [o for o in self._stray_operations(unfinished=False)
                    if o.intent.get("task") == str(ref)]  # fmt: skip
            if self._backed(ref) or any(not o.status.is_final for o in mine):
                continue
            self._effects.intend_stray_label(ref, n=len(mine) + 1)
        return frozenset(cleared)

    def _stray_operations(self, *, unfinished: bool) -> list[Operation]:
        return [
            o
            for o in self._store.instance_operations()
            if o.kind is OperationKind.CLEAR_STRAY_LABEL
            and (not unfinished or not o.status.is_final)
        ]

    def _intent_event(self, key: str) -> Event | None:
        return next(
            (e for e in self._store.events()
             if e.operation == key and e.type is EventType.OPERATION_INTENDED),
            None,
        )  # fmt: skip

    def _created_after(self, ref: TaskRef, seq: int) -> bool:
        """Whether an execution of the task was created after ``seq`` (store order)."""
        return any(
            e.type is EventType.EXECUTION_CREATED and (e.seq or 0) > seq
            for execution in self._executions_of(ref)
            for e in self._store.events(execution_id=execution.id)
        )

    def _orphan_key(self, claim: HeldClaim) -> str:
        return instance_operation_key(
            OperationKind.RELEASE_ORPHAN_CLAIM, self._effects.fence.instance_id, orphan_step(claim)
        )

    def _executions_of(self, ref: TaskRef) -> list[Execution]:
        return [e for e in self._store.list_in_states(set(ExecutionState)) if e.task == ref]

    def _backed(self, ref: TaskRef) -> bool:
        """ADR-0026 §5, rule 3: a claim is backed while any execution of its task is
        non-terminal, SUCCEEDED with its change request still tracked, terminal with
        quarantined resources, or still needs attention (its own worker releases it)."""
        for execution in self._executions_of(ref):
            if not execution.state.is_terminal or needs_attention(self._store, execution):
                return True
            if blocked_for_maintainer(self._store, execution):  # ADR-0031 §3
                return True
            rows = self._store.resources(execution_id=execution.id)
            if any(r.status is ResourceStatus.QUARANTINED for r in rows):
                return True
            if execution.state is S.SUCCEEDED and any(
                r.kind is ResourceKind.CHANGE_REQUEST
                and r.status in (ResourceStatus.ACTIVE, ResourceStatus.RELEASING)
                for r in rows
            ):
                return True
        return False

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

    def _pick(self, *, not_now: frozenset[TaskRef] = frozenset()) -> Task | None:
        """The first eligible task (by key) Ilmar is not already working on or done with.

        Never started again automatically: a task with an active execution;
        one with a published (SUCCEEDED) execution, whose change request
        exists, open or merged; one whose execution FAILED; and one whose
        execution was CANCELLED on request (ADR-0025 §3), or because its claim
        was lost or the task vanished (ADR-0026 §3). Releasing such a task's
        claim makes it eligible on the tracker, but running it again would undo
        a decision: a failure classified as not retryable (ADR-0021), or a
        person's cancellation. A human decides.

        A task cancelled because the tracker made it ineligible or claimed by
        someone else may be picked again, as a new execution, once the tracker
        lists it as eligible again; but never while an earlier execution of it
        still needs attention, such as an unfinished claim release (ADR-0026 §5).
        """
        finished = {e.task for e in self._store.list_in_states({S.SUCCEEDED, S.FAILED})}
        terminal = self._store.list_in_states({s for s in ExecutionState if s.is_terminal})
        finished |= {e.task for e in terminal if needs_attention(self._store, e)}
        for execution in self._store.list_in_states({S.CANCELLED}):
            if not cancellation_accepted(self._store, execution.id):
                continue  # not claimed in the first place: eligible again
            reason = tracker.accepted_reason(self._store.events(execution_id=execution.id))
            if reason not in tracker.REPICKABLE:
                finished.add(execution.task)
        for task in sorted(self._c.task_provider.list_eligible(), key=lambda t: t.ref.key):
            if task.ref in not_now:
                continue  # its stray label went in this pass: never picked in the same one
            if task.ref in finished or self._store.active_for_task(task.ref) is not None:
                continue
            return task
        return None


def _none_after(work: Callable[[], object]) -> WorkerResult | None:
    work()
    return None
