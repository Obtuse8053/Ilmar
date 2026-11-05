# SPDX-License-Identifier: Apache-2.0
"""Drives one execution from QUEUED to a terminal or blocked state (ADR-0021).

A worker holds the execution's lease for its whole run, re-reads the
execution after every step (so every intent is written against the version
it last saw), and changes the outside world only through :class:`Effects`.
Workers share nothing mutable: several can run at once on different
executions.

Failure classification (ADR-0021):

* **transient** (``TransientError``): the agent timed out; a checkout,
  sandbox start, agent run, verification run or push certainly did not
  complete. A new attempt follows while attempts remain (at most 3).
* **deterministic, security or policy**: never retried automatically. They
  end in FAILED and release the claim: the agent failed or changed nothing,
  checks failed, CI configuration changed, unsafe agent output, the branch
  moved, a foreign change request, a policy violation.
* **blocked**: an ambiguous effect, a quarantined resource or an
  inconsistent ledger. The worker stops at once and a human (or #11c)
  decides.
* **ownership lost** (``FencingError``): propagated; nothing more happens.
"""

from __future__ import annotations

import enum
import secrets
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime

from ilmar.core.components import Components
from ilmar.core.effects import Effects, TransientError, parse_identity
from ilmar.core.journal import check_candidate
from ilmar.core.naming import branch_name
from ilmar.core.ownership import ExecutionLease, ExecutionLeases
from ilmar.core.state_machine import ExecutionStateMachine
from ilmar.domain.agent import AgentOutcome, AgentRequest, AgentResult
from ilmar.domain.changes import CandidateRevision, ChangeRequestSpec
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
from ilmar.domain.execution import Execution, ExecutionState
from ilmar.domain.ledger import Commit, OperationKind, operation_key
from ilmar.domain.tasks import ClaimOutcome, Task
from ilmar.domain.verification import EvidenceRecord, Verdict
from ilmar.domain.workspace import ContainerHandle, ResourceOwner
from ilmar.ports.execution_store import ExecutionStore

S = ExecutionState
MAX_ATTEMPTS = 3
REVIEW_LABEL = "ilmar:needs-human-review"
_DETERMINISTIC = (
    CandidateRejectedError,
    LeaseConflictError,
    NonFastForwardError,
    OwnershipConflictError,
    PolicyViolationError,
)


class Outcome(enum.Enum):
    PUBLISHED = "published"
    """Execution complete: the verified candidate is published as a change request."""
    FAILED = "failed"
    CANCELLED = "cancelled"
    BLOCKED = "blocked"
    """Needs a human or reconciliation (#11c): an ambiguous effect or a quarantine."""


@dataclass(frozen=True, slots=True)
class WorkerResult:
    execution_id: str
    outcome: Outcome
    detail: str = ""


class _Deterministic(Exception):  # noqa: N818 - internal control flow, never escapes
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

    # -- the run ----------------------------------------------------------------------

    def run(self, execution_id: str, task: Task) -> WorkerResult:
        with self.leases.acquire(execution_id) as lease:
            return _Run(self, lease, execution_id, task).drive()


class _Run:
    """The mutable state of one worker run. Never shared between workers."""

    def __init__(
        self, worker: ExecutionWorker, lease: ExecutionLease, execution_id: str, task: Task
    ) -> None:
        self.w = worker
        self.lease = lease
        self.id = execution_id
        self.task = task
        self.branch = branch_name(task.ref)
        self.owner: ResourceOwner | None = None
        self.agent_result: AgentResult | None = None
        self.candidate: CandidateRevision | None = None
        self.evidence: EvidenceRecord | None = None

    @property
    def store(self) -> ExecutionStore:
        return self.w.components.execution_store

    def current(self) -> Execution:
        found = self.store.get(self.id)
        if found is None:
            raise LedgerIntegrityError(f"execution {self.id} vanished")
        return found

    def move(self, target: ExecutionState, reason: str, **values: str) -> Execution:
        execution = self.current()
        updated, transition = self.w.machine.advance(
            execution, target, now=self.w.clock(), reason=reason, **values
        )
        self.lease.check(self.id)
        self.store.apply(updated, transition, fence=self.w.effects.fence)
        return updated

    def transition_to(self, target: ExecutionState, reason: str, **values: str) -> Commit:
        """A commit moving the execution from its current version to ``target``."""
        updated, transition = self.w.machine.advance(
            self.current(), target, now=self.w.clock(), reason=reason, **values
        )
        return Commit(applied=((updated, transition),))

    def drive(self) -> WorkerResult:
        while True:
            execution = self.current()
            if execution.state.is_terminal:
                return self.finish(execution)
            try:
                self.step(execution)
            except TransientError as error:
                self.retry_or_fail(str(error))
            except _Deterministic as failure:
                self.fail(failure.reason)
            except _DETERMINISTIC as error:
                self.fail(f"{type(error).__name__}: {error}")
            except ContainmentError as error:
                self.fail(f"containment: {error}")
                return WorkerResult(self.id, Outcome.BLOCKED, "a sandbox could not be removed")
            except (
                AmbiguousOperationError,
                LedgerIntegrityError,
                ConcurrencyConflictError,
                ProviderError,  # an effect's outcome is unclear: the journal says ambiguous
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

    # -- steps ------------------------------------------------------------------------

    def claim(self, execution: Execution) -> None:
        def then(result: Mapping[str, str]) -> Commit:
            outcome = ClaimOutcome(result["outcome"])
            if outcome is ClaimOutcome.CLAIMED:
                return self.transition_to(S.CLAIMED, "claimed")
            return self.transition_to(S.CANCELLED, f"not claimed: {outcome.value}")

        self.w.effects.claim(execution, self.lease, then=then)

    def provision(self, execution: Execution) -> None:
        self.owner = ResourceOwner(
            self.w.components.instance_id, self.id, execution.attempt, self.w.new_token()
        )
        self.w.effects.create_attempt_directory(execution, self.lease, owner_token=self.owner.token)
        self.w.effects.checkout(
            self.current(),
            self.lease,
            owner_token=self.owner.token,
            branch=self.branch,
            then=lambda r: self.transition_to(
                S.RUNNING_AGENT,
                "workspace ready",
                base_revision=r["base_revision"],
                branch=self.branch,
            ),
        )

    def run_agent(self, execution: Execution) -> None:
        owner = self.require_owner(execution)
        layout = self.w.components.layout
        workspace = layout.agent_workspace(self.id, execution.attempt)
        key = operation_key(OperationKind.CHECKOUT_BASE, self.id, execution.attempt)
        checkout = self.store.operation(key)
        if checkout is None:
            raise LedgerIntegrityError(f"{self.id} has no recorded checkout")
        identity = parse_identity(checkout.result["identity"])
        handle = self.w.effects.start_agent_sandbox(
            execution, self.lease, owner=owner, workspace=workspace, identity=identity
        )
        try:
            result = self.w.effects.run_agent(
                self.current(), self.lease, handle=handle, request=self.request()
            )
        finally:
            self.stop(handle, owner)
        self.agent_result = result
        if result.outcome is AgentOutcome.TIMED_OUT:
            raise TransientError("the agent timed out")
        if result.outcome is AgentOutcome.FAILED:
            raise _Deterministic(f"the agent failed: {result.summary[:200]}")

        def then(r: Mapping[str, str]) -> Commit:
            if r["changed_path_count"] == "0":
                return self.transition_to(
                    S.FAILED, "the agent changed nothing", error="the agent changed nothing"
                )
            return self.transition_to(
                S.VERIFYING, "candidate frozen", candidate_revision=r["revision"]
            )

        self.candidate = self.w.effects.freeze(
            self.current(),
            self.lease,
            message=f"Ilmar candidate for {self.task.ref.key} ({self.id}, attempt "
            f"{execution.attempt})",
            then=then,
        )

    def stop(self, handle: ContainerHandle, owner: ResourceOwner) -> None:
        self.w.effects.stop_agent_sandbox(self.current(), self.lease, handle=handle, owner=owner)

    def verify(self, execution: Execution) -> None:
        owner = self.require_owner(execution)
        candidate, agent_result = self.candidate, self.agent_result
        if candidate is None or agent_result is None:
            raise LedgerIntegrityError(f"{self.id}: verifying needs the candidate in memory (#11c)")
        self.check_frozen(execution, candidate)
        run = 1 + sum(1 for e in self.store.evidence(self.id) if e.attempt == execution.attempt)

        def then(r: Mapping[str, str]) -> Commit:
            verdict = Verdict(r["verdict"])
            if verdict.publishable:
                return self.transition_to(S.PUBLISHING, f"verified: {verdict.value}")
            reason = (
                "CI configuration changed: a human must take over"
                if verdict is Verdict.REJECTED_CI_CONFIG
                else "verification checks failed"
            )
            return self.transition_to(S.FAILED, reason, error=reason)

        self.evidence = self.w.effects.verify(
            execution,
            self.lease,
            candidate=candidate,
            agent_result=agent_result,
            owner=owner,
            run=run,
            then=then,
        )

    def publish(self, execution: Execution) -> None:
        candidate, evidence = self.candidate, self.evidence
        if candidate is None or evidence is None:
            raise LedgerIntegrityError(f"{self.id}: publishing needs the verified candidate (#11c)")
        self.check_frozen(execution, candidate)
        stored = self.store.evidence(self.id)
        if not stored or stored[-1] != evidence or not evidence.verdict.publishable:
            raise LedgerIntegrityError(f"{self.id}: the latest evidence does not allow publishing")
        if (evidence.candidate_revision, evidence.base_revision) != (
            execution.candidate_revision,
            execution.base_revision,
        ):
            raise LedgerIntegrityError(f"{self.id}: the evidence is for another candidate")
        checkout = self.store.operation(
            operation_key(OperationKind.CHECKOUT_BASE, self.id, execution.attempt)
        )
        if checkout is None:
            raise LedgerIntegrityError(f"{self.id} has no recorded checkout")
        expected_head = checkout.result["branch_head"] or None
        step = ""
        for tries in range(3):
            try:
                self.w.effects.publish(
                    self.current(),
                    self.lease,
                    branch=self.branch,
                    expected_head=expected_head,
                    step=step,
                )
                break
            except TransientError:
                if tries == 2:
                    raise
                step = f"retry-{tries + 1}"
        labels = (REVIEW_LABEL,) if evidence.verdict is Verdict.PUBLISH_FOR_REVIEW else ()
        spec = ChangeRequestSpec(
            repository=self.w.components.target.repository,
            source_branch=self.branch,
            target_branch=self.w.components.target.base_branch,
            title=self.task.title,
            description=describe(self.task, evidence),
            task=self.task.ref,
            labels=labels,
        )
        owner = self.require_owner(execution)
        self.w.effects.upsert_change_request(
            self.current(),
            self.lease,
            spec=spec,
            owner_token=owner.token,
            then=lambda r: self.transition_to(
                S.SUCCEEDED, "change request published", change_request_url=r["url"]
            ),
        )

    # -- endings ----------------------------------------------------------------------

    def retry_or_fail(self, reason: str) -> None:
        execution = self.current()
        if self.w.machine.retries_remaining(execution) > 0 and execution.state not in (S.QUEUED,):
            self.move(S.RETRY_WAIT, f"transient: {reason}", error=reason)
            self.candidate = self.agent_result = self.evidence = None
            self.owner = None
        else:
            self.fail(f"attempts exhausted: {reason}")

    def fail(self, reason: str) -> None:
        execution = self.current()
        if not execution.state.is_terminal:
            self.move(S.FAILED, reason, error=reason)

    def finish(self, execution: Execution) -> WorkerResult:
        try:
            return self._finish(execution)
        except (OwnershipConflictError, ProviderError, OSError, AmbiguousOperationError) as error:
            return WorkerResult(self.id, Outcome.BLOCKED, f"cleanup: {type(error).__name__}")

    def _finish(self, execution: Execution) -> WorkerResult:
        effects, lease = self.w.effects, self.lease
        if execution.state is S.SUCCEEDED:
            effects.post_update(
                execution,
                lease,
                step="published",
                message=f"Ilmar published a verified change: {execution.change_request_url}",
            )
            effects.delete_attempt(self.current(), lease)
            return WorkerResult(self.id, Outcome.PUBLISHED, execution.change_request_url or "")
        if execution.state is S.FAILED:
            reason = execution.last_error or "failed"
            effects.post_update(
                execution, lease, step="failed", message=f"Ilmar could not complete this: {reason}"
            )
            effects.release(self.current(), lease, step="failed")
            return WorkerResult(self.id, Outcome.FAILED, reason)
        return WorkerResult(self.id, Outcome.CANCELLED, execution.last_error or "")

    # -- checks -----------------------------------------------------------------------

    def require_owner(self, execution: Execution) -> ResourceOwner:
        if self.owner is None or self.owner.attempt != execution.attempt:
            raise LedgerIntegrityError(f"{self.id}: no owner token for attempt {execution.attempt}")
        return self.owner

    def check_frozen(self, execution: Execution, candidate: CandidateRevision) -> None:
        """The candidate in memory is exactly the one the journal froze (ADR-0020)."""
        freeze = self.store.operation(
            operation_key(OperationKind.FREEZE_CANDIDATE, self.id, execution.attempt)
        )
        if freeze is None:
            raise LedgerIntegrityError(f"{self.id} has no freeze for attempt {execution.attempt}")
        check_candidate(candidate, freeze)
        if (candidate.revision, candidate.base_revision) != (
            execution.candidate_revision,
            execution.base_revision,
        ):
            raise LedgerIntegrityError(f"{self.id}: the candidate is not the recorded one")

    def request(self) -> AgentRequest:
        return AgentRequest(
            task=self.task,
            instructions=instructions(self.task),
            timeout_seconds=self.w.agent_timeout,
        )


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
