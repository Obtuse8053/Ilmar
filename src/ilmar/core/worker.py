# SPDX-License-Identifier: Apache-2.0
"""Drives one execution, from wherever it is, to a terminal or blocked state.

ADR-0021 defines the steps; ADR-0022 makes them recoverable. A worker holds
the execution's lease and decides **every** step from durable state only:
the execution, the journal of its operations, the resource ledger, its
evidence, and trusted observations (the runtime, the branch head, the
coordinator's own repository). Nothing a step needs is kept in memory
between steps. A fresh run and a run after any number of crashes therefore
follow the same code, and repeating a step is safe by construction:

* an operation that already succeeded is never performed again;
* one left unfinished by a crash is first settled from an observation
  (``Effects.settle_*``);
* the frozen candidate is always reloaded from the coordinator's repository
  and checked against the freeze's revision, base and changed-paths digest.

Failure classes (ADR-0021):

* **transient** (``TransientError``), *before a candidate exists*: a new
  attempt while attempts remain (at most 3). A crash before or during the
  agent run abandons the attempt the same way;
* **late-stage**, *after freezing*: the same frozen candidate is verified or
  pushed again (at most 3 tries), then blocked. **The agent never runs again
  once a candidate is frozen**, which is enforced here and in ``Effects``;
* **deterministic, security or policy**: FAILED, never retried;
* **blocked**: needs a human; ownership lost (``FencingError``): propagated.
"""

from __future__ import annotations

import enum
import logging
import secrets
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime

from ilmar.core.cancel_inbox import CancelRequest
from ilmar.core.cancel_watcher import WATCH_INTERVAL_SECONDS, Watcher
from ilmar.core.cancellation import Cancellations, cancellation_accepted
from ilmar.core.clock import not_before
from ilmar.core.components import Components
from ilmar.core.effects import (
    ABANDONED,
    NEVER_TRANSIENT,
    DeterministicError,
    Effects,
    TransientError,
    parse_identity,
)
from ilmar.core.event_context import failure_code
from ilmar.core.naming import branch_name, container_name
from ilmar.core.ownership import ExecutionLease, ExecutionLeases
from ilmar.core.safe_fs import directory_identity
from ilmar.core.state_machine import ExecutionStateMachine
from ilmar.domain.agent import AgentOutcome, AgentRequest, AgentResult
from ilmar.domain.changes import ChangeRequestSpec
from ilmar.domain.errors import (
    AmbiguousOperationError,
    CandidateRejectedError,
    ConcurrencyConflictError,
    ContainmentError,
    LeaseConflictError,
    LedgerIntegrityError,
    NonFastForwardError,
    OwnershipConflictError,
    PolicyViolationError,
    ProviderError,
)
from ilmar.domain.events import EventType, reason_for_claim, reason_for_error
from ilmar.domain.execution import Execution, ExecutionState
from ilmar.domain.ledger import (
    Commit,
    Operation,
    OperationKind,
    OperationStatus,
    Resource,
    ResourceKind,
    ResourceStatus,
    operation_key,
    resource_key,
)
from ilmar.domain.tasks import ClaimOutcome, Task
from ilmar.domain.verification import EvidenceRecord, Verdict
from ilmar.domain.workspace import ResourceOwner
from ilmar.ports.execution_store import ExecutionStore

_LOG = logging.getLogger(__name__)
S = ExecutionState
K = OperationKind
ST = OperationStatus
R = ResourceStatus
MAX_ATTEMPTS = 3
LATE_TRIES = 3
"""Verification runs, pushes or deletions of one frozen candidate within an attempt."""
REVIEW_LABEL = "ilmar:needs-human-review"
_NEVER_TRANSIENT_NAMES = frozenset(c.__name__ for c in NEVER_TRANSIENT)
_NEVER_TRANSIENT_CODES = {c.__name__: reason_for_error(c) for c in NEVER_TRANSIENT}
_DETERMINISTIC = (
    CandidateRejectedError,
    LeaseConflictError,
    NonFastForwardError,
    OwnershipConflictError,
    PolicyViolationError,
)
CANCEL_TRIES = 3
"""How often one pass tries a pending cancellation's ending before it reports BLOCKED."""
CANCEL_STEP = "cancelled"
"""The step of the note and the claim release that end a cancellation (ADR-0025 §3)."""
_UNFINISHED = (ST.INTENDED, ST.AMBIGUOUS)
_LIVE = (R.INTENDED, R.ACTIVE, R.RELEASING)


class Outcome(enum.Enum):
    PUBLISHED = "published"
    """Execution complete: the verified candidate is published as a change request."""
    FAILED = "failed"
    CANCELLED = "cancelled"
    BLOCKED = "blocked"
    """Needs a human: an unsettleable effect, a quarantine, an inconsistent ledger."""


@dataclass(frozen=True, slots=True)
class WorkerResult:
    execution_id: str
    outcome: Outcome
    detail: str = ""
    code: str | None = None
    """The stable reason code (ADR-0024 §4) of a failure, cancellation or block,
    when one is recorded; ``detail`` keeps the human-readable text."""

    def line(self) -> str:
        """``e1: failed [agent-failed] the agent failed: …``"""
        code = f"[{self.code}] " if self.code else ""
        return f"{self.execution_id}: {self.outcome.value} {code}{self.detail}".strip()


class _Deterministic(Exception):  # noqa: N818 - internal control flow, never escapes
    def __init__(self, reason: str, *, code: str) -> None:
        super().__init__(reason)
        self.reason = reason
        self.code = code


class _Cancelled(Exception):  # noqa: N818 - internal control flow, never escapes
    """A cancellation is accepted for this execution: end it at this safe point."""


class _Blocked(Exception):  # noqa: N818 - internal control flow, never escapes
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class ExecutionWorker:
    def __init__(
        self,
        *,
        components: Components,
        effects: Effects,
        leases: ExecutionLeases,
        machine: ExecutionStateMachine,
        clock: Callable[[], datetime],
        agent_timeout_seconds: float,
        new_token: Callable[[], str] = lambda: secrets.token_hex(16),
        watch_interval_seconds: float = WATCH_INTERVAL_SECONDS,
    ) -> None:
        if machine.max_attempts > MAX_ATTEMPTS:
            raise ValueError(f"at most {MAX_ATTEMPTS} attempts per execution (ADR-0021)")
        self.components = components
        self.effects = effects
        self.leases = leases
        self.machine = machine
        self.clock = clock
        self.agent_timeout = agent_timeout_seconds
        self.new_token = new_token
        self.watch_interval = watch_interval_seconds
        self.cancellations = Cancellations(
            store=components.execution_store, journal=effects.journal, layout=components.layout
        )

    def run(self, execution_id: str, task: Task | None) -> WorkerResult:
        """Drive ``execution_id``; ``task`` is the tracker's view, ``None`` if it vanished."""
        with self.leases.acquire(execution_id) as lease:
            return _Run(self, lease, execution_id, task).drive()

    def stranded_requests(self) -> tuple[str, ...]:
        """Execution ids with an entry left in the request inbox."""
        return self.cancellations.pending()

    def apply_stranded(self, execution_id: str) -> tuple[str, ...]:
        """Record and clean up a request left for a **terminal** execution (ADR-0025
        §3: ``already-terminal``), under its lease and the instance fence. Nothing
        else is done to the execution. Returns what was moved aside."""
        with self.leases.acquire(execution_id) as lease:
            execution = self.components.execution_store.get(execution_id)
            if execution is None or not execution.state.is_terminal:
                return ()  # unknown, or a live execution: its own worker applies it
            applied = self.cancellations.apply(execution, lease)
            for report in applied.reports:
                _LOG.warning("%s", report)
            return applied.reports


def needs_attention(store: ExecutionStore, execution: Execution) -> bool:
    """Whether a terminal execution still has work: unsettled operations or live sandboxes."""
    if any(o.status in _UNFINISHED for o in store.operations(execution.id)):
        return True
    rows = store.resources(execution_id=execution.id, statuses=set(_LIVE))
    if any(r.kind is ResourceKind.CONTAINER for r in rows):
        return True
    if execution.state is S.SUCCEEDED:  # its final workspace must be gone
        return any(
            r.attempt == execution.attempt and r.kind is ResourceKind.WORKSPACE for r in rows
        )
    if execution.state is S.FAILED:  # its claim must be released
        return (
            store.operation(
                operation_key(K.RELEASE_TASK, execution.id, execution.attempt, "failed")
            )
            is None
        )
    if execution.state is S.CANCELLED and cancellation_accepted(store, execution.id):
        # Cancelled on request: a claim it held must be released (ADR-0025 §5).
        return (
            held_claim(store, execution.id)
            and store.operation(
                operation_key(K.RELEASE_TASK, execution.id, execution.attempt, CANCEL_STEP)
            )
            is None
        )
    return False


def held_claim(store: ExecutionStore, execution_id: str) -> bool:
    """Whether this execution claimed its task on the tracker."""
    return any(
        o.kind is K.CLAIM_TASK
        and o.status is ST.SUCCEEDED
        and o.result.get("outcome") == ClaimOutcome.CLAIMED.value
        for o in store.operations(execution_id)
    )


def recorded_never_transient(operation: Operation | None) -> str | None:
    """The never-transient error class an operation recorded, if any.

    The journal records such failures as ``FAILED`` with the class as the
    note (older records: ``AMBIGUOUS``). Recovery must end the execution
    for these, exactly as the uninterrupted path does, and never abandon
    and retry them (ADR-0021, ADR-0022).
    """
    if operation is None or operation.status not in (ST.FAILED, ST.AMBIGUOUS):
        return None
    return operation.note if operation.note in _NEVER_TRANSIENT_NAMES else None


class _Run:
    """One worker run. Holds no state a step depends on: everything is re-read."""

    def __init__(
        self, worker: ExecutionWorker, lease: ExecutionLease, execution_id: str, task: Task | None
    ) -> None:
        self.w = worker
        self.lease = lease
        self.id = execution_id
        self.task = task
        self.cancel_failures = 0
        self.notices: list[str] = []
        """Inbox entries moved aside during this run, for the operator (ADR-0025 §1a)."""

    # -- durable state --------------------------------------------------------------

    @property
    def store(self) -> ExecutionStore:
        return self.w.components.execution_store

    def current(self) -> Execution:
        found = self.store.get(self.id)
        if found is None:
            raise LedgerIntegrityError(f"execution {self.id} vanished")
        return found

    def op(self, execution: Execution, kind: OperationKind, step: str = "") -> Operation | None:
        return self.store.operation(operation_key(kind, self.id, execution.attempt, step))

    def ops(self, execution: Execution, kind: OperationKind) -> list[Operation]:
        return [
            o
            for o in self.store.operations(self.id)
            if o.kind is kind and o.attempt == execution.attempt
        ]

    def row(self, execution: Execution, kind: ResourceKind, name: str) -> Resource | None:
        return self.store.resource(resource_key(kind, self.id, execution.attempt, name))

    def owner(self, execution: Execution) -> ResourceOwner:
        """The attempt's owner, from the ledger: its token is on the attempt directory row."""
        top = self.row(
            execution,
            ResourceKind.WORKSPACE,
            str(self.w.components.layout.attempt_dir(self.id, execution.attempt)),
        )
        if top is None:
            raise LedgerIntegrityError(f"{self.id}: attempt {execution.attempt} has no owner token")
        return ResourceOwner(
            self.w.components.instance_id, self.id, execution.attempt, top.owner_token
        )

    def require_task(self) -> Task:
        if self.task is None:
            raise _Blocked("the task no longer exists on the tracker")
        return self.task

    def move(
        self, target: ExecutionState, reason: str, *, code: str | None = None, **values: str
    ) -> Execution:
        changes = self.transition_to(target, reason, code=code, **values)
        self.w.effects.journal.record(changes, lease=self.lease, execution_id=self.id)
        ((updated, _),) = changes.applied
        return updated

    def transition_to(
        self, target: ExecutionState, reason: str, *, code: str | None = None, **values: str
    ) -> Commit:
        """``code`` is the stable reason code of a failure, cancellation or retry."""
        current = self.current()
        updated, transition = self.w.machine.advance(
            current, target, now=not_before(self.w.clock(), current.updated_at), reason=reason,
            **values,
        )  # fmt: skip
        return Commit(
            applied=((updated, transition),), reasons={self.id: code} if code is not None else {}
        )

    # -- the loop -------------------------------------------------------------------

    def drive(self) -> WorkerResult:
        result = self._drive()
        if not self.notices:
            return result
        notices = "; ".join(self.notices)
        detail = f"{result.detail} (inbox: {notices})" if result.detail else f"inbox: {notices}"
        return WorkerResult(result.execution_id, result.outcome, detail, code=result.code)

    def _drive(self) -> WorkerResult:
        while True:
            execution = self.current()
            try:
                finished = self.advance(execution)
                if finished is not None:
                    return finished
            except TransientError as error:
                ended = self.retry_or_fail(str(error), code=error.code)
                if ended is not None:
                    return ended
            except (_Deterministic, DeterministicError) as failure:
                ended = self.fail(str(failure), code=failure.code)
                if ended is not None:
                    return ended
            except _Blocked as blocked:
                return WorkerResult(self.id, Outcome.BLOCKED, blocked.reason)
            except _DETERMINISTIC as error:
                ended = self.fail(f"{type(error).__name__}: {error}", code=reason_for_error(error))
                if ended is not None:
                    return ended
            except ContainmentError as error:
                if self.cancel_pending():
                    # The sandbox is quarantined; the ending settles the rest, then
                    # CANCELLED, and finish keeps the claim (ADR-0025 §3).
                    ended = self.cancel_instead(f"containment: {error}",
                                                  code="containment-unconfirmed")  # fmt: skip
                    if ended is not None:
                        return ended
                    continue
                if not self.current().state.is_terminal:
                    self.fail(f"containment: {error}", code="containment-unconfirmed")
                return WorkerResult(self.id, Outcome.BLOCKED, "a sandbox could not be removed",
                                    code="containment-unconfirmed")  # fmt: skip
            except (
                AmbiguousOperationError,
                LedgerIntegrityError,
                ConcurrencyConflictError,
                ProviderError,
                OSError,
            ) as error:
                return WorkerResult(self.id, Outcome.BLOCKED, f"{type(error).__name__}: {error}",
                                    code=failure_code(error))  # fmt: skip

    def advance(self, execution: Execution) -> WorkerResult | None:
        """One loop iteration: the safe point, then the step (or the ending)."""
        if execution.state.is_terminal:
            self.apply_inbox()  # a request that arrived too late: already-terminal
            return self.finish(self.current())
        try:
            self.cancel_point()
            self.step(execution)
        except _Cancelled:
            self.end_cancelled()
        return None

    def step(self, execution: Execution) -> None:
        state = execution.state
        if state is S.QUEUED:
            self.claim(execution)
        elif state in (S.CLAIMED, S.RETRY_WAIT):
            self.move(S.PROVISIONING, "provisioning a new attempt")
        elif state is S.PROVISIONING:
            self.provision(execution)
        elif state is S.RUNNING_AGENT:
            self.run_agent(execution)
        elif state is S.VERIFYING:
            self.verify(execution)
        elif state is S.PUBLISHING:
            self.publish(execution)
        else:  # pragma: no cover - every non-terminal state is handled above
            raise LedgerIntegrityError(f"unexpected state {state.value}")

    # -- QUEUED ---------------------------------------------------------------------

    def claim(self, execution: Execution) -> None:
        def then(result: Mapping[str, str]) -> Commit:
            outcome = ClaimOutcome(result["outcome"])
            if outcome is ClaimOutcome.CLAIMED:
                return self.transition_to(S.CLAIMED, "claimed")
            return self.transition_to(
                S.CANCELLED, f"not claimed: {outcome.value}", code=reason_for_claim(outcome)
            )

        self.w.effects.claim(execution, self.lease, then=then)  # repeatable

    # -- PROVISIONING ---------------------------------------------------------------

    def provision(self, execution: Execution) -> None:
        layout, effects = self.w.components.layout, self.w.effects
        create, checkout = (
            self.op(execution, K.CREATE_WORKSPACE),
            self.op(execution, K.CHECKOUT_BASE),
        )
        for operation, kind, path in (
            (create, K.CREATE_WORKSPACE, layout.attempt_dir(self.id, execution.attempt)),
            (checkout, K.CHECKOUT_BASE, layout.agent_workspace(self.id, execution.attempt)),
        ):
            never = recorded_never_transient(operation)
            if operation is not None and operation.status in _UNFINISHED:
                effects.settle_directory_step(execution, self.lease, kind=kind, path=path)
                if never:
                    raise _never(f"{kind.value} failed", never)
                raise TransientError("provisioning was interrupted", code="interrupted")
            if operation is not None and operation.status is ST.FAILED:
                if never:
                    raise _never(f"{kind.value} failed", never)
                if operation.note == ABANDONED:  # a crash, then another before retrying
                    raise TransientError("provisioning was interrupted", code="interrupted")
                raise _Deterministic(
                    f"{kind.value} failed",
                    code="checkout-failed" if kind is K.CHECKOUT_BASE else "unexpected-error",
                )
        if create is None:
            effects.create_attempt_directory(execution, self.lease, owner_token=self.w.new_token())
            self.cancel_point()
        branch = branch_name(execution.task)
        effects.checkout(
            self.current(),
            self.lease,
            owner_token=self.owner(execution).token,
            branch=branch,
            then=lambda r: self.transition_to(
                S.RUNNING_AGENT, "workspace ready", base_revision=r["base_revision"], branch=branch
            ),
        )

    # -- RUNNING_AGENT --------------------------------------------------------------

    def run_agent(self, execution: Execution) -> None:
        c, effects = self.w.components, self.w.effects
        name = container_name(c.instance_id, self.id, execution.attempt)
        start = self.op(execution, K.START_CONTAINER, name)
        run = self.op(execution, K.RUN_AGENT)
        if self.op(execution, K.FREEZE_CANDIDATE) is None and start is None:
            self.fresh_agent_run(execution, name)
            return
        sandbox = self.row(execution, ResourceKind.CONTAINER, name)
        never = recorded_never_transient(start) or recorded_never_transient(run)
        if never:  # recorded before the crash: fail, as the uninterrupted path does
            if sandbox is not None:
                effects.remove_sandbox(execution, self.lease, resource=sandbox)
            raise _never("the agent step failed", never)
        if run is None:
            # A crash after the sandbox was created and before the agent ran: the
            # sandbox is never reused (ADR-0022). Remove it and start over.
            if sandbox is not None:
                effects.remove_sandbox(execution, self.lease, resource=sandbox)
            if start is not None and start.status in _UNFINISHED:
                effects.settle_start(self.current(), self.lease, name=name)
            raise TransientError("interrupted before the agent ran", code="interrupted")
        if run.status in _UNFINISHED:
            effects.abandon_agent_run(execution, self.lease)
            if sandbox is not None:
                effects.remove_sandbox(self.current(), self.lease, resource=sandbox)
            raise TransientError("interrupted while the agent ran", code="interrupted")
        if sandbox is not None:  # the sandbox must be confirmed gone before freezing
            effects.remove_sandbox(execution, self.lease, resource=sandbox)
        if run.status is ST.FAILED:
            raise TransientError("the agent run was interrupted", code="interrupted")
        result = AgentResult(
            AgentOutcome(run.result["outcome"]),
            run.result.get("summary", ""),
            int(run.result["exit_code"]) if run.result.get("exit_code") else None,
        )
        if result.outcome is AgentOutcome.TIMED_OUT:
            raise TransientError("the agent timed out", code="agent-timed-out")
        if result.outcome is AgentOutcome.FAILED:
            raise _Deterministic(f"the agent failed: {result.summary[:200]}", code="agent-failed")
        self.cancel_point()  # never frozen once a cancellation is accepted
        if result.outcome is not AgentOutcome.COMPLETED:
            # A cancelled run's output never becomes a candidate (ADR-0025 §2). A
            # stopped run whose request was not recorded fails closed.
            raise _Deterministic(
                f"the agent run ended {result.outcome.value}; nothing is frozen",
                code="cancel-requested",
            )
        self.freeze(self.current(), name)

    def fresh_agent_run(self, execution: Execution, name: str) -> None:
        """Start the sandbox and run the agent in one step: a crash in between is abandoned."""
        c, effects = self.w.components, self.w.effects
        owner = self.owner(execution)
        workspace = c.layout.agent_workspace(self.id, execution.attempt)
        checkout = self.op(execution, K.CHECKOUT_BASE)
        if checkout is None or checkout.status is not ST.SUCCEEDED:
            raise LedgerIntegrityError(f"{self.id} has no completed checkout")
        identity = parse_identity(checkout.result["identity"])
        handle = effects.start_agent_sandbox(
            execution, self.lease, owner=owner, workspace=workspace, identity=identity
        )
        watcher = Watcher(self.w.cancellations.read, self.id, interval=self.w.watch_interval)
        try:
            self.cancel_point()  # a started sandbox is removed; the agent never runs
            with watcher:  # writes nothing; sets the stop signal for a valid request only
                effects.run_agent(self.current(), self.lease, handle=handle,
                                  request=self.request(), stop=watcher.signal)  # fmt: skip
        finally:
            sandbox = self.row(execution, ResourceKind.CONTAINER, name)
            if sandbox is not None:
                effects.remove_sandbox(self.current(), self.lease, resource=sandbox)
            if watcher.request is not None:  # recorded as validated, right after the run
                self.apply_inbox(validated=watcher.request)

    def freeze(self, execution: Execution, name: str) -> None:
        sandbox = self.row(execution, ResourceKind.CONTAINER, name)
        if sandbox is None or sandbox.status is not R.RELEASED:
            raise LedgerIntegrityError(
                f"{self.id}: never freeze before the sandbox is confirmed gone"
            )
        workspace = self.w.components.layout.agent_workspace(self.id, execution.attempt)
        recorded = self.row(execution, ResourceKind.WORKSPACE, str(workspace))
        try:
            found = directory_identity(workspace, root=self.w.components.layout.workspaces_root)
        except OwnershipConflictError as error:  # planted in our own state: a human decides
            raise LedgerIntegrityError(
                f"{self.id}: the agent workspace is not the recorded one"
            ) from error
        if (
            recorded is None
            or found is None
            or recorded.identity != f"{found.device}:{found.inode}"
        ):
            raise LedgerIntegrityError(f"{self.id}: the agent workspace is not the recorded one")
        prior = self.op(execution, K.FREEZE_CANDIDATE)
        if prior is not None and prior.status is ST.FAILED:
            raise _Deterministic(
                "the agent's output could not be frozen safely", code="candidate-rejected"
            )

        def then(r: Mapping[str, str]) -> Commit:
            if r["changed_path_count"] == "0":
                return self.transition_to(
                    S.FAILED, "the agent changed nothing", code="no-changes",
                    error="the agent changed nothing",
                )  # fmt: skip
            return self.transition_to(
                S.VERIFYING, "candidate frozen", candidate_revision=r["revision"]
            )

        task = self.require_task()
        self.w.effects.freeze(  # repeatable and deterministic
            execution,
            self.lease,
            message=f"Ilmar candidate for {task.ref.key} ({self.id}, attempt {execution.attempt})",
            then=then,
        )

    # -- VERIFYING ------------------------------------------------------------------

    def verify(self, execution: Execution) -> None:
        effects = self.w.effects
        candidate = effects.load_candidate(execution)  # rebuilt and checked, never remembered
        run_op = self.op(execution, K.RUN_AGENT)
        if run_op is None or run_op.status is not ST.SUCCEEDED:
            raise LedgerIntegrityError(f"{self.id}: verifying without a recorded agent run")
        agent_result = AgentResult(
            AgentOutcome(run_op.result["outcome"]), run_op.result.get("summary", ""), None
        )

        def then(r: Mapping[str, str]) -> Commit:
            verdict = Verdict(r["verdict"])
            if verdict.publishable:
                return self.transition_to(S.PUBLISHING, f"verified: {verdict.value}")
            ci = verdict is Verdict.REJECTED_CI_CONFIG
            reason = (
                "CI configuration changed: a human must take over"
                if ci
                else "verification checks failed"
            )
            code = "ci-config-changed" if ci else "verification-failed"
            return self.transition_to(S.FAILED, reason, code=code, error=reason)

        runs = sorted(self.ops(execution, K.VERIFY), key=lambda o: int(o.key.rsplit("run-", 1)[1]))
        never = next((n for o in runs if (n := recorded_never_transient(o))), None)
        for operation in runs:
            if operation.status in _UNFINISHED:
                number = int(operation.key.rsplit("run-", 1)[1])
                if never is None and effects.settle_verify(
                    self.current(), self.lease, run=number, candidate=candidate, then=then
                ):
                    return
                if never is not None:  # its sandboxes go; its outcome does not count
                    effects.settle_verify(
                        self.current(), self.lease, run=number, candidate=candidate,
                        then=lambda _r: Commit(),
                    )  # fmt: skip
        if never is not None:
            raise _never("verification failed", never)
        tried = len(runs)
        if tried >= LATE_TRIES:
            raise _Blocked(
                f"verification infrastructure failed {LATE_TRIES} times; the frozen candidate "
                "is kept"
            )
        self.cancel_point()  # no verification run starts once a cancellation is accepted
        try:
            effects.verify(
                self.current(),
                self.lease,
                candidate=candidate,
                agent_result=agent_result,
                owner=self.owner(execution),
                run=tried + 1,
                then=then,
            )
        except TransientError:
            return  # the same frozen candidate is verified again, as the next run

    # -- PUBLISHING -----------------------------------------------------------------

    def publish(self, execution: Execution) -> None:
        c, effects = self.w.components, self.w.effects
        candidate = effects.load_candidate(execution)
        evidence = self.latest_evidence(execution)
        if (evidence.candidate_revision, evidence.base_revision) != (
            candidate.revision,
            candidate.base_revision,
        ) or not evidence.verdict.publishable:
            raise LedgerIntegrityError(f"{self.id}: the latest evidence does not allow publishing")
        if evidence.policy_digest != c.verification.digest():
            # The policy changed since this run (for example while Ilmar was down).
            # Evidence only proves what it checked: a human decides (ADR-0022).
            raise _Blocked(
                "the verification policy changed since the candidate was verified; "
                "it is not published on the old evidence"
            )
        branch = execution.branch or branch_name(execution.task)
        checkout = self.op(execution, K.CHECKOUT_BASE)
        if checkout is None or checkout.status is not ST.SUCCEEDED:
            raise LedgerIntegrityError(f"{self.id} has no recorded checkout")
        expected_head = checkout.result["branch_head"] or None
        pushes = self.ops(execution, K.PUBLISH_REVISION)
        pushed = any(o.status is ST.SUCCEEDED for o in pushes)
        for operation in pushes:
            if operation.status in _UNFINISHED:
                step = operation.key.split(":", 3)[3] if operation.key.count(":") == 3 else ""
                pushed = (
                    effects.settle_publish(
                        execution, self.lease, step=step, branch=branch, expected_head=expected_head
                    )
                    or pushed
                )
        if not pushes:
            self.cancel_point()  # before the first push; after it, a request is too late
        if not pushed:
            failed = sum(
                1 for o in self.ops(self.current(), K.PUBLISH_REVISION) if o.status is ST.FAILED
            )
            if failed >= LATE_TRIES:
                raise _Blocked(
                    f"the push failed {LATE_TRIES} times; the verified candidate is kept"
                )
            try:
                effects.publish(
                    self.current(),
                    self.lease,
                    branch=branch,
                    expected_head=expected_head,
                    step=f"retry-{failed}" if failed else "",
                )
            except TransientError:
                return  # pushed again next loop, under a new key
        self.apply_inbox()  # a publish intent exists: a request now is recorded too-late
        head = c.source_control.branch_head(c.target.repository, branch)  # read only
        if head != candidate.revision:
            raise _Blocked(
                f"{branch} moved after the push; a human must decide (never overwritten)"
            )
        upsert = self.op(self.current(), K.UPSERT_CHANGE_REQUEST)
        if upsert is not None and upsert.status is ST.SUCCEEDED:
            raise _Blocked(
                f"{branch} moved while the change request was opened; it does not show the "
                "verified candidate, so the execution is not complete"
            )
        task = self.require_task()
        labels = (REVIEW_LABEL,) if evidence.verdict is Verdict.PUBLISH_FOR_REVIEW else ()
        spec = ChangeRequestSpec(
            repository=c.target.repository,
            source_branch=branch,
            target_branch=c.target.base_branch,
            title=task.title,
            description=describe(task, evidence),
            task=task.ref,
            labels=labels,
        )

        def then(r: Mapping[str, str]) -> Commit:
            if r["head_after"] != r["verified"]:
                return Commit()  # the change request exists but no longer shows it
            return self.transition_to(
                S.SUCCEEDED, "change request published", change_request_url=r["url"]
            )

        effects.upsert_change_request(  # repeatable, keyed by branch
            self.current(),
            self.lease,
            spec=spec,
            owner_token=self.owner(execution).token,
            verified=candidate.revision,
            then=then,
        )

    def latest_evidence(self, execution: Execution) -> EvidenceRecord:
        verified = [o for o in self.ops(execution, K.VERIFY) if o.status is ST.SUCCEEDED]
        if len(verified) != 1:
            raise LedgerIntegrityError(f"{self.id}: expected exactly one successful verification")
        run = int(verified[0].key.rsplit("run-", 1)[1])
        records = [
            e
            for e in self.store.evidence(self.id)
            if e.attempt == execution.attempt and e.run == run
        ]
        if len(records) != 1 or records[0].digest() != verified[0].result.get("evidence_digest"):
            raise LedgerIntegrityError(f"{self.id}: the verified run's evidence is not intact")
        return records[0]

    # -- endings --------------------------------------------------------------------

    def retry_or_fail(self, reason: str, *, code: str) -> WorkerResult | None:
        execution = self.current()
        if self.cancel_pending():  # never a new attempt once a cancellation is accepted
            return self.cancel_instead(reason, code=code)
        if self.op(execution, K.FREEZE_CANDIDATE) is not None:
            # Never a new agent run once this attempt froze a candidate.
            return WorkerResult(
                self.id, Outcome.BLOCKED, f"a transient failure after freezing: {reason}", code=code
            )
        if self.w.machine.retries_remaining(execution) > 0 and execution.state is not S.QUEUED:
            self.move(S.RETRY_WAIT, f"transient: {reason}", code=code, error=reason)
        else:
            return self.fail(f"attempts exhausted: {reason}", code="attempts-exhausted")
        return None

    def fail(self, reason: str, *, code: str) -> WorkerResult | None:
        if self.cancel_pending():  # a cancellation never turns into a failure (ADR-0025 §3)
            return self.cancel_instead(reason, code=code)
        if not self.current().state.is_terminal:
            self.move(S.FAILED, reason, code=code, error=reason)
        return None

    def cancel_instead(self, reason: str, *, code: str) -> WorkerResult | None:
        """A failure while a cancellation is pending. It never becomes FAILED, a retry
        or a bare CANCELLED transition: the loop runs the whole ending again
        (reconciliation, ownership-verified sandbox removal), at most
        ``CANCEL_TRIES`` times in this pass, then reports BLOCKED and leaves the
        execution as it is for the next pass."""
        self.cancel_failures += 1
        _LOG.warning("%s: cancelling failed (%s): %s", self.id, code, reason)
        if self.cancel_failures < CANCEL_TRIES:
            return None
        detail = f"the cancellation could not complete yet: {reason}"
        return WorkerResult(self.id, Outcome.BLOCKED, detail, code=code)

    # -- cancellation (ADR-0025) ----------------------------------------------------

    def apply_inbox(self, *, validated: CancelRequest | None = None) -> None:
        """Record this execution's request, if any, and clean up the inbox entry."""
        applied = self.w.cancellations.apply(self.current(), self.lease, validated=validated)
        for report in applied.reports:
            _LOG.warning("%s", report)
            self.notices.append(report)

    def cancel_pending(self) -> bool:
        return not self.current().state.is_terminal and cancellation_accepted(self.store, self.id)

    def cancel_point(self) -> None:
        """A safe point: apply a request from the inbox, then stop if one is accepted."""
        self.apply_inbox()
        if self.cancel_pending():
            raise _Cancelled

    def end_cancelled(self) -> None:
        """Undo what the current state started (ADR-0025 §3), then CANCELLED."""
        execution = self.current()
        if execution.state.is_terminal:
            return
        c, effects, state = self.w.components, self.w.effects, execution.state
        if state is S.QUEUED:
            claim = self.op(execution, K.CLAIM_TASK)
            if claim is not None and claim.status in _UNFINISHED:  # repeatable: settled first
                effects.claim(execution, self.lease, then=lambda _r: Commit())
        elif state is S.PROVISIONING:
            for kind, path in (
                (K.CREATE_WORKSPACE, c.layout.attempt_dir(self.id, execution.attempt)),
                (K.CHECKOUT_BASE, c.layout.agent_workspace(self.id, execution.attempt)),
            ):
                operation = self.op(execution, kind)
                if operation is not None and operation.status in _UNFINISHED:
                    effects.settle_directory_step(self.current(), self.lease, kind=kind, path=path)
        elif state is S.VERIFYING:
            unfinished = [o for o in self.ops(execution, K.VERIFY) if o.status in _UNFINISHED]
            if unfinished:
                candidate = effects.load_candidate(execution)
                for operation in unfinished:  # its sandboxes go; its outcome does not count
                    effects.settle_verify(
                        self.current(), self.lease, run=int(operation.key.rsplit("run-", 1)[1]),
                        candidate=candidate, then=lambda _r: Commit(),
                    )  # fmt: skip
        self.sweep_sandboxes()  # journaled, ownership-verified removal of every live sandbox
        quarantined = any(
            r.kind is ResourceKind.CONTAINER
            for r in self.store.resources(execution_id=self.id, statuses={R.QUARANTINED})
        )
        if state is S.RUNNING_AGENT and not quarantined:
            # Settling these says the sandbox is gone; with a quarantined one it is
            # not known, so they stay for the human who resolves the quarantine.
            name = container_name(c.instance_id, self.id, execution.attempt)
            start = self.op(execution, K.START_CONTAINER, name)
            if start is not None and start.status in _UNFINISHED:
                effects.settle_start(self.current(), self.lease, name=name)
            run = self.op(execution, K.RUN_AGENT)
            if run is not None and run.status in _UNFINISHED:
                effects.abandon_agent_run(self.current(), self.lease)
        self.end_cancelled_now("cancelled on request")

    def end_cancelled_now(self, reason: str) -> None:
        """The transition itself, only ever after the ending above; ``finish`` then
        posts the note and releases the claim, or keeps it (quarantine)."""
        if not self.current().state.is_terminal:
            self.move(S.CANCELLED, reason, code="cancel-requested")

    def finish(self, execution: Execution) -> WorkerResult:
        effects = self.w.effects
        for operation in self.store.operations(self.id):  # informational notes are not repeated
            if operation.kind is K.POST_UPDATE and operation.status in _UNFINISHED:
                step = operation.key.split(":", 3)[3]
                effects.settle_post(execution, self.lease, step=step)
        self.sweep_sandboxes()
        quarantined = self.store.resources(execution_id=self.id, statuses={R.QUARANTINED})
        if execution.state is S.SUCCEEDED:
            self.note(
                execution,
                "published",
                f"Ilmar published a verified change: {execution.change_request_url}",
            )
            self.delete_final_workspace(self.current())
            if quarantined:
                return self.quarantine_remains()
            return WorkerResult(self.id, Outcome.PUBLISHED, execution.change_request_url or "")
        if execution.state is S.FAILED:
            if quarantined:  # a human must look: the claim stays
                return self.quarantine_remains()
            reason = execution.last_error or "failed"
            self.note(execution, "failed", f"Ilmar could not complete this: {reason}")
            effects.release(self.current(), self.lease, step="failed")  # repeatable
            code = self.recorded_code(EventType.EXECUTION_TRANSITIONED, S.FAILED)
            return WorkerResult(self.id, Outcome.FAILED, reason, code=code)
        if cancellation_accepted(self.store, self.id):  # cancelled on request (ADR-0025 §3)
            if quarantined:  # a human must look: the claim stays
                return self.quarantine_remains()
            if held_claim(self.store, self.id):  # never claimed: nothing to tell or release
                message = "Ilmar stopped working on this task: its cancellation was requested."
                self.note(execution, CANCEL_STEP, message)
                effects.release(self.current(), self.lease, step=CANCEL_STEP)  # repeatable
        code = self.recorded_code(EventType.EXECUTION_TRANSITIONED, S.CANCELLED)
        detail = self.recorded_reason(S.CANCELLED) or execution.last_error or ""
        return WorkerResult(self.id, Outcome.CANCELLED, detail, code=code)

    def quarantine_remains(self) -> WorkerResult:
        code = self.recorded_code(EventType.RESOURCE_QUARANTINED)
        return WorkerResult(self.id, Outcome.BLOCKED, "quarantined resources remain", code=code)

    def recorded_code(self, kind: EventType, state: ExecutionState | None = None) -> str | None:
        """The reason code of this execution's latest ``kind`` event (into ``state``):
        read from the durable events, so a later pass reports the code of a failure
        recorded before a crash."""
        for event in reversed(self.store.events(execution_id=self.id)):
            if event.type is kind and (state is None or event.data.get("to") == state.value):
                return event.reason
        return None

    def recorded_reason(self, state: ExecutionState) -> str | None:
        """The reason of this execution's latest transition into ``state``."""
        for transition in reversed(self.store.history(self.id)):
            if transition.to_state is state:
                return transition.reason
        return None

    def note(self, execution: Execution, step: str, message: str) -> None:
        if self.op(execution, K.POST_UPDATE, step) is None:
            self.w.effects.post_update(self.current(), self.lease, step=step, message=message)

    def sweep_sandboxes(self) -> None:
        """Remove every sandbox of this execution the ledger does not show as gone."""
        for row in self.store.resources(execution_id=self.id, statuses=set(_LIVE)):
            if row.kind is ResourceKind.CONTAINER:
                self.w.effects.remove_sandbox(self.current(), self.lease, resource=row)

    def delete_final_workspace(self, execution: Execution) -> None:
        deletions = self.ops(execution, K.DELETE_WORKSPACE)
        for operation in deletions:
            if operation.status in _UNFINISHED:
                step = operation.key.split(":", 3)[3] if operation.key.count(":") == 3 else ""
                if self.w.effects.settle_delete(execution, self.lease, step=step):
                    return
        if any(o.status is ST.SUCCEEDED for o in self.ops(execution, K.DELETE_WORKSPACE)):
            return
        failed = sum(1 for o in self.ops(execution, K.DELETE_WORKSPACE) if o.status is ST.FAILED)
        if failed >= LATE_TRIES:
            raise _Blocked("the workspace could not be deleted")
        self.w.effects.delete_attempt(
            self.current(), self.lease, step=f"retry-{failed}" if failed else ""
        )

    def request(self) -> AgentRequest:
        task = self.require_task()
        return AgentRequest(
            task=task, instructions=instructions(task), timeout_seconds=self.w.agent_timeout
        )


def _never(what: str, name: str) -> _Deterministic:
    """A recorded never-transient failure: the execution ends with that class's code."""
    return _Deterministic(f"{what}: {name}", code=_NEVER_TRANSIENT_CODES[name])


def instructions(task: Task) -> str:
    return (
        "Make the change the following task asks for, in this repository. "
        "Do not change CI configuration. Your work will be verified independently.\n\n"
        f"Task: {task.title}\n\n{task.description}"
    )


def describe(task: Task, evidence: EvidenceRecord) -> str:
    """The change request's text: what Ilmar verified, and what the agent merely reported."""
    passed = [c.name for c in evidence.checks if c.passed]
    lines = [
        f"Ilmar change for: {task.title}",
        "",
        f"- Verdict: `{evidence.verdict.value}` ({evidence.independence.value})",
        f"- Candidate: `{evidence.candidate_revision}` on `{evidence.base_revision}`",
        f"- Checks passed: {', '.join(passed) or 'none'}",
    ]
    if evidence.requires_human_review:
        lines.append(f"- Needs human review: {'; '.join(evidence.review_reasons)}")
    lines += ["", "Reported by the agent (not verified):", "", evidence.agent_report.summary[:1000]]
    if task.url:
        lines += ["", f"Task: {task.url}"]
    return "\n".join(lines)
