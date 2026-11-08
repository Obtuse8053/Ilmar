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
import secrets
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime

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
from ilmar.domain.events import reason_for_claim, reason_for_error
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


class _Deterministic(Exception):  # noqa: N818 - internal control flow, never escapes
    def __init__(self, reason: str, *, code: str) -> None:
        super().__init__(reason)
        self.reason = reason
        self.code = code


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

    def run(self, execution_id: str, task: Task | None) -> WorkerResult:
        """Drive ``execution_id``; ``task`` is the tracker's view, ``None`` if it vanished."""
        with self.leases.acquire(execution_id) as lease:
            return _Run(self, lease, execution_id, task).drive()


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
    return False


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
        while True:
            execution = self.current()
            try:
                if execution.state.is_terminal:
                    return self.finish(execution)
                self.step(execution)
            except TransientError as error:
                blocked = self.retry_or_fail(str(error), code=error.code)
                if blocked is not None:
                    return blocked
            except (_Deterministic, DeterministicError) as failure:
                self.fail(str(failure), code=failure.code)
            except _Blocked as blocked:
                return WorkerResult(self.id, Outcome.BLOCKED, blocked.reason)
            except _DETERMINISTIC as error:
                self.fail(f"{type(error).__name__}: {error}", code=reason_for_error(error))
            except ContainmentError as error:
                if not self.current().state.is_terminal:
                    self.fail(f"containment: {error}", code="containment-unconfirmed")
                return WorkerResult(self.id, Outcome.BLOCKED, "a sandbox could not be removed")
            except (
                AmbiguousOperationError,
                LedgerIntegrityError,
                ConcurrencyConflictError,
                ProviderError,
                OSError,
            ) as error:
                return WorkerResult(self.id, Outcome.BLOCKED, f"{type(error).__name__}: {error}")

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
        try:
            effects.run_agent(self.current(), self.lease, handle=handle, request=self.request())
        finally:
            sandbox = self.row(execution, ResourceKind.CONTAINER, name)
            if sandbox is not None:
                effects.remove_sandbox(self.current(), self.lease, resource=sandbox)

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
        if self.op(execution, K.FREEZE_CANDIDATE) is not None:
            # Never a new agent run once this attempt froze a candidate.
            return WorkerResult(
                self.id, Outcome.BLOCKED, f"a transient failure after freezing: {reason}"
            )
        if self.w.machine.retries_remaining(execution) > 0 and execution.state is not S.QUEUED:
            self.move(S.RETRY_WAIT, f"transient: {reason}", code=code, error=reason)
        else:
            self.fail(f"attempts exhausted: {reason}", code="attempts-exhausted")
        return None

    def fail(self, reason: str, *, code: str) -> None:
        if not self.current().state.is_terminal:
            self.move(S.FAILED, reason, code=code, error=reason)

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
                return WorkerResult(self.id, Outcome.BLOCKED, "quarantined resources remain")
            return WorkerResult(self.id, Outcome.PUBLISHED, execution.change_request_url or "")
        if execution.state is S.FAILED:
            if quarantined:  # a human must look: the claim stays
                return WorkerResult(self.id, Outcome.BLOCKED, "quarantined resources remain")
            reason = execution.last_error or "failed"
            self.note(execution, "failed", f"Ilmar could not complete this: {reason}")
            effects.release(self.current(), self.lease, step="failed")  # repeatable
            return WorkerResult(self.id, Outcome.FAILED, reason)
        return WorkerResult(self.id, Outcome.CANCELLED, execution.last_error or "")

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
