# SPDX-License-Identifier: Apache-2.0
"""The only place in the core that changes the outside world (ADR-0020, ADR-0021).

Every public method of :class:`Effects` is exactly one ``Journal.perform``:

* its idempotency key comes from the execution, attempt and step;
* resources are recorded as ``INTENDED`` with the intent, and become
  ``ACTIVE`` (with their identity) or ``RELEASED`` with the outcome;
* exceptions are declared definitive only where the effect certainly did
  not happen;
* an unclear outcome is reconciled at once where an observation decides it
  (the container does not exist, the branch head did not move). Otherwise
  it is left ambiguous for a human or #11c.

An architecture test forbids every other core module, except the Verifier
(which runs inside :meth:`Effects.verify`), from calling a port method that
changes anything.

Methods return the in-memory objects later steps need (the frozen
``CandidateRevision``, container handles, evidence). Those objects are never
rebuilt from journal text in #11b. A replay that would need one raises
``LedgerIntegrityError``; #11c rebuilds a candidate with ``load_candidate``.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from ilmar.core.components import Components
from ilmar.core.event_context import CodedError, PassEvents, failure_code
from ilmar.core.journal import Journal, check_candidate, freeze_result
from ilmar.core.mounts import authorize_mount
from ilmar.core.naming import container_name, verification_container_name
from ilmar.core.ownership import ExecutionLease
from ilmar.core.safe_fs import directory_identity, ensure_directory, remove_directory
from ilmar.core.verification import Verifier
from ilmar.domain.agent import AgentOutcome, AgentRequest, AgentResult
from ilmar.domain.changes import CandidateRevision, ChangeRequest, ChangeRequestSpec
from ilmar.domain.errors import (
    AmbiguousOperationError,
    CandidateRejectedError,
    ContainmentError,
    FencingError,
    LeaseConflictError,
    LedgerIntegrityError,
    NonFastForwardError,
    OwnershipConflictError,
    PolicyViolationError,
    ProviderError,
)
from ilmar.domain.execution import Execution
from ilmar.domain.ledger import (
    Commit,
    Fence,
    OperationKind,
    OperationStatus,
    Resource,
    ResourceKind,
    ResourceStatus,
    operation_key,
    resource_key,
)
from ilmar.domain.tasks import (
    ClaimOutcome,
    ClearResult,
    HeldClaim,
    ReleaseResult,
    RestoreResult,
    TaskRef,
)
from ilmar.domain.verification import EvidenceRecord
from ilmar.domain.workspace import (
    ContainerHandle,
    ContainerSpec,
    DirectoryIdentity,
    ResourceOwner,
    StopSignal,
)
from ilmar.ports.execution_store import ExecutionStore

K = OperationKind
R = ResourceStatus
Then = Callable[[Mapping[str, str]], Commit]
_SUMMARY_LIMIT = 2000


class TransientError(CodedError):
    """The step failed for a reason a repetition may not meet (ADR-0021)."""

    default_code = "transient-provider-error"


ABANDONED = "interrupted; the attempt is abandoned"
"""The note of a step that recovery settled as failed because a crash interrupted it.

Such a failure is transient: it says nothing about the step itself (ADR-0022)."""


class DeterministicError(CodedError):
    """The step failed in a way that repeating it would only reproduce (ADR-0021)."""


class ClaimChangedError(DeterministicError):
    """A targeted claim write found its object no longer this instance's authentic
    claim with the recorded token: nothing of ours to act on (ADR-0026 §5)."""

    default_code = "claim-changed"


def claim_intent(claim: HeldClaim) -> dict[str, str]:
    """What a targeted claim write records in its intent: the exact object."""
    return {"task": str(claim.ref), "claim": claim.claim_id, "object": claim.object_id}


def claim_from_intent(intent: Mapping[str, str]) -> HeldClaim:
    """The claim a recorded intent names: an interrupted write repeats exactly it."""
    provider, _, key = intent["task"].partition(":")
    return HeldClaim(TaskRef(provider, key), intent["claim"], intent["object"])


def orphan_step(claim: HeldClaim) -> str:
    digest = hashlib.sha256(
        "\0".join((claim.ref.key, claim.object_id, claim.claim_id)).encode()
    ).hexdigest()
    return f"{claim.ref.provider}.{digest[:32]}"


def stray_step(ref: TaskRef, n: int) -> str:
    """The step of the ``n``-th stray-label operation for a task (ADR-0027 §1)."""
    digest = hashlib.sha256(ref.key.encode()).hexdigest()
    return f"{ref.provider}.{digest[:32]}.{n}"


def stray_number(key: str) -> int:
    """The ``n`` of a stray-label operation key."""
    return int(key.rsplit(".", 1)[1])


def _absent(result: Mapping[str, str]) -> str | None:
    return "claim-absent" if result.get("result") == "absent" else None


# Never converted into a transient failure, whatever port raised them: a
# security, policy, ownership or integrity failure ends retries at once
# (review of 46c1662). ``ContainmentError`` is a ``ProviderError``, so every
# ``except ProviderError`` below lets these through first.
NEVER_TRANSIENT: tuple[type[BaseException], ...] = (
    ContainmentError,
    PolicyViolationError,
    OwnershipConflictError,
    CandidateRejectedError,
    LedgerIntegrityError,
    FencingError,
)


def identity_text(identity: DirectoryIdentity) -> str:
    return f"{identity.device}:{identity.inode}"


def parse_identity(text: str) -> DirectoryIdentity:
    device, _, inode = text.partition(":")
    return DirectoryIdentity(int(device), int(inode))


@dataclass(frozen=True, slots=True)
class Checkout:
    base_revision: str
    expected_head: str | None
    """The task branch's head before the agent ran: the lease for publishing."""
    identity: DirectoryIdentity


class Effects:
    def __init__(
        self,
        *,
        components: Components,
        journal: Journal,
        verifier: Verifier,
        clock: Callable[[], datetime],
    ) -> None:
        self._c = components
        self._journal = journal
        self._verifier = verifier
        self._clock = clock

    @property
    def fence(self) -> Fence:
        return self._journal.fence

    @property
    def events(self) -> PassEvents:
        return self._journal.events

    @property
    def journal(self) -> Journal:
        return self._journal

    @property
    def _store(self) -> ExecutionStore:
        return self._c.execution_store

    # -- the task --------------------------------------------------------------------

    def claim(self, execution: Execution, lease: ExecutionLease, *, then: Then) -> ClaimOutcome:
        def effect() -> Mapping[str, str]:
            claim = self._c.task_provider.claim(execution.task)
            result = {"outcome": claim.outcome.value}
            if claim.claim_id is not None:  # the claim this execution holds (ADR-0026 §3a)
                result["claim_id"] = claim.claim_id
            return result

        result = self._journal.perform(K.CLAIM_TASK, execution, effect, lease=lease, after=then)
        return ClaimOutcome(result["outcome"])

    def release(self, execution: Execution, lease: ExecutionLease, *, step: str) -> None:
        def effect() -> Mapping[str, str]:
            self._c.task_provider.release(execution.task)
            return {}

        self._journal.perform(K.RELEASE_TASK, execution, effect, lease=lease, step=step)

    def restore_label(
        self, execution: Execution, lease: ExecutionLease, *, claim: HeldClaim, step: str
    ) -> RestoreResult:
        """Add only the claim label beside exactly ``claim``'s object (ADR-0026 §5).

        Never a claim, never a decision: ``RESTORED`` and ``ABSENT`` succeed,
        ``CHANGED`` fails (``claim-changed``). An interrupted restore is performed
        again with the claim its intent recorded, never with a newer read.
        """

        def effect() -> Mapping[str, str]:
            result = self._c.task_provider.restore_label(claim)
            if result is RestoreResult.CHANGED:
                raise ClaimChangedError("the claim is no longer this instance's authentic claim")
            return {"result": result.value}

        result = self._journal.perform(
            K.RESTORE_CLAIM_LABEL, execution, effect, lease=lease, step=step,
            intent=claim_intent(claim), definitive=(ClaimChangedError,),
            observed="claim-label-missing", outcome_observed=_absent,
        )  # fmt: skip
        return RestoreResult(result["result"])

    def release_orphan_claim(
        self, claim: HeldClaim, *, revalidate: Callable[[], None]
    ) -> ReleaseResult:
        """Release exactly one orphaned claim object (ADR-0026 §5, rule 4).

        Instance-scoped and fenced by the instance lock; ``revalidate`` runs
        after the intent and the fence check, right before the effect. The key
        is the provider plus a digest of the task, the object and the claim id,
        so a newer claim is always a different operation.
        """

        def effect() -> Mapping[str, str]:
            result = self._c.task_provider.release_claim(claim)
            if result is ReleaseResult.CHANGED:
                raise ClaimChangedError("the claim is no longer this instance's authentic claim")
            return {"result": result.value}

        result = self._journal.perform_instance(
            K.RELEASE_ORPHAN_CLAIM, effect, step=orphan_step(claim), intent=claim_intent(claim),
            reason="orphaned-claim", revalidate=revalidate, definitive=(ClaimChangedError,),
            outcome_observed=_absent,
        )  # fmt: skip
        return ReleaseResult(result["result"])

    def intend_stray_label(self, ref: TaskRef, *, n: int) -> str:
        """Record the intent to remove a stray claim label; nothing is written to the
        tracker in this pass (ADR-0027 §1). Returns the operation's key."""
        operation = self._journal.intend_instance(
            K.CLEAR_STRAY_LABEL, step=stray_step(ref, n), intent={"task": str(ref)},
            reason="stray-label",
        )  # fmt: skip
        return operation.key

    def clear_stray_label(
        self, ref: TaskRef, *, n: int, revalidate: Callable[[], None]
    ) -> ClearResult:
        """Resume a recorded stray-label intent in a later pass: revalidate against
        the store, then let ``clear_label`` re-read the issue and decide."""

        def effect() -> Mapping[str, str]:
            return {"result": self._c.task_provider.clear_label(ref).value}

        def found(result: Mapping[str, str]) -> str | None:
            return {"absent": "claim-label-missing", "kept": "claim-present"}.get(
                result.get("result", "")
            )

        result = self._journal.perform_instance(
            K.CLEAR_STRAY_LABEL, effect, step=stray_step(ref, n), intent={"task": str(ref)},
            reason="stray-label", revalidate=revalidate, outcome_observed=found,
        )  # fmt: skip
        return ClearResult(result["result"])

    def post_update(
        self, execution: Execution, lease: ExecutionLease, *, step: str, message: str
    ) -> bool:
        """Informational. An unclear outcome is never repeated (no duplicate notes)."""

        def effect() -> Mapping[str, str]:
            self._c.task_provider.post_update(execution.task, message)
            return {}

        key = operation_key(K.POST_UPDATE, execution.id, execution.attempt, step)
        try:
            self._journal.perform(K.POST_UPDATE, execution, effect, lease=lease, step=step)
        except AmbiguousOperationError:
            return False
        except ProviderError:
            self._journal.reconcile(
                key,
                OperationStatus.FAILED,
                reason="the note may or may not exist; not repeated",
                observed="outcome-unknown",
                code="transient-provider-error",
                lease=lease,
            )
            return False
        return True

    # -- the workspace ----------------------------------------------------------------

    def create_attempt_directory(
        self, execution: Execution, lease: ExecutionLease, *, owner_token: str
    ) -> DirectoryIdentity:
        layout = self._c.layout
        path = layout.attempt_dir(execution.id, execution.attempt)
        planned = self._planned(ResourceKind.WORKSPACE, execution, str(path), owner_token)

        def effect() -> Mapping[str, str]:
            if directory_identity(path, root=layout.workspaces_root) is not None:
                raise OwnershipConflictError(f"{path} already exists; it is never reused")
            return {"identity": identity_text(ensure_directory(path, root=layout.workspaces_root))}

        result = self._journal.perform(
            K.CREATE_WORKSPACE,
            execution,
            effect,
            lease=lease,
            intent={"path": str(path)},
            before=Commit(resources=(planned,)),
            after=lambda r: Commit(resources=(self._activate(planned, r["identity"]),)),
            definitive=(OwnershipConflictError,),
        )
        return parse_identity(result["identity"])

    def checkout(
        self,
        execution: Execution,
        lease: ExecutionLease,
        *,
        owner_token: str,
        branch: str,
        then: Then,
    ) -> Checkout:
        layout, target = self._c.layout, self._c.target
        path = layout.agent_workspace(execution.id, execution.attempt)
        planned = self._planned(ResourceKind.WORKSPACE, execution, str(path), owner_token)
        scm = self._c.source_control

        def effect() -> Mapping[str, str]:
            head = scm.branch_head(target.repository, branch)
            base = scm.checkout(
                target.repository,
                base_branch=target.base_branch,
                branch=branch,
                destination=path,
            )
            identity = directory_identity(path, root=layout.workspaces_root)
            if identity is None:
                raise ProviderError("the checkout left no workspace")
            return {
                "base_revision": base,
                "branch_head": head or "",
                "identity": identity_text(identity),
            }

        try:
            result = self._journal.perform(
                K.CHECKOUT_BASE,
                execution,
                effect,
                lease=lease,
                intent={"branch": branch, "path": str(path)},
                before=Commit(resources=(planned,)),
                after=lambda r: _merge(
                    Commit(resources=(self._activate(planned, r["identity"]),)), then(r)
                ),
                definitive=(ProviderError,),  # attempt-scoped: nothing else was changed
            )
        except NEVER_TRANSIENT:
            raise
        except ProviderError as error:
            # Not retried: a checkout fails the same way again (unknown base
            # branch, an unsafe tree refused by the adapter).
            self._settle_partial(execution, lease, planned, path)
            raise DeterministicError(
                f"checkout failed: {type(error).__name__}", code="checkout-failed"
            ) from error
        return Checkout(
            result["base_revision"],
            result["branch_head"] or None,
            parse_identity(result["identity"]),
        )

    def delete_attempt(
        self, execution: Execution, lease: ExecutionLease, *, step: str = ""
    ) -> bool:
        """Delete a finished attempt's directory and record every resource inside as released."""
        layout = self._c.layout
        path = layout.attempt_dir(execution.id, execution.attempt)
        top = self._store.resource(
            resource_key(ResourceKind.WORKSPACE, execution.id, execution.attempt, str(path))
        )
        if top is None or top.status is R.RELEASED:
            return False
        if top.status is R.QUARANTINED or top.identity is None:
            raise LedgerIntegrityError(f"{path} is not a recorded, deletable directory")
        group = self._attempt_tree(execution, path, statuses={R.ACTIVE, R.RELEASING})
        now = self._clock()
        releasing = [r if r.status is R.RELEASING else r.moved(R.RELEASING, now=now) for r in group]
        before = Commit(
            resources=tuple(r for r, g in zip(releasing, group, strict=True) if g is not r)
        )
        expected = parse_identity(top.identity)

        def effect() -> Mapping[str, str]:
            removed = remove_directory(path, root=layout.workspaces_root, expected=expected)
            return {"removed": str(removed).lower()}

        try:
            self._journal.perform(
                K.DELETE_WORKSPACE,
                execution,
                effect,
                lease=lease,
                step=step,
                intent={"path": str(path)},
                before=before,
                after=lambda _: Commit(
                    resources=tuple(
                        r.moved(R.RELEASED, now=self._clock(), note="deleted after success")
                        for r in releasing
                    )
                ),
                definitive=(OwnershipConflictError,),
            )
        except OwnershipConflictError:
            latest = [self._store.resource(r.key) or r for r in releasing]
            replaced = tuple(
                r.moved(R.QUARANTINED, now=self._clock(), note="replaced on disk")
                for r in latest
                if r.status not in (R.RELEASED, R.QUARANTINED)
            )
            self._commit(lease, execution, _quarantined(replaced, "resource-ownership-conflict"))
            raise
        return True

    def _attempt_tree(
        self, execution: Execution, path: Path, *, statuses: set[ResourceStatus]
    ) -> list[Resource]:
        return [
            r
            for r in self._store.resources(execution_id=execution.id, statuses=statuses)
            if r.attempt == execution.attempt
            and r.kind in (ResourceKind.WORKSPACE, ResourceKind.VERIFICATION_CHECKOUT)
            and (r.name == str(path) or Path(r.name).is_relative_to(path))
        ]

    # -- the agent sandbox ------------------------------------------------------------

    def start_agent_sandbox(
        self,
        execution: Execution,
        lease: ExecutionLease,
        *,
        owner: ResourceOwner,
        workspace: Path,
        identity: DirectoryIdentity,
    ) -> ContainerHandle:
        name = container_name(self._c.instance_id, execution.id, execution.attempt)
        planned = self._planned(ResourceKind.CONTAINER, execution, name, owner.token)
        sandbox = self._c.agent_sandbox
        runtime = self._c.container_runtime

        def effect() -> Mapping[str, str]:
            spec = ContainerSpec(
                image=sandbox.image,
                host_workspace=authorize_mount(self._c.layout, workspace, identity),
                name=name,
                owner=owner,
                workspace_identity=identity,
                network=sandbox.network,
                allowed_hosts=sandbox.allowed_hosts,
                limits=sandbox.limits,
            )
            handle = runtime.start(spec)
            return {"id": handle.id, "name": handle.name, "runtime": handle.runtime}

        key = operation_key(K.START_CONTAINER, execution.id, execution.attempt, name)
        try:
            result = self._journal.perform(
                K.START_CONTAINER,
                execution,
                effect,
                lease=lease,
                step=name,
                intent={"name": name, "image": sandbox.image},
                before=Commit(resources=(planned,)),
                after=lambda r: Commit(resources=(self._activate(planned, r["id"]),)),
                definitive=(PolicyViolationError, OwnershipConflictError),
            )
        except (PolicyViolationError, OwnershipConflictError):
            self._commit(
                lease,
                execution,
                Commit(resources=(planned.moved(R.RELEASED, now=self._clock(), note="refused"),)),
            )
            raise
        except ContainmentError:
            self._quarantine(
                execution, lease, planned, "start could not be cleaned up",
                code="containment-unconfirmed",
            )  # fmt: skip
            raise
        except NEVER_TRANSIENT:
            raise
        except ProviderError as error:
            # Observe: if no container exists under the name, nothing happened.
            if runtime.find(name) is None:
                self._journal.reconcile(
                    key,
                    OperationStatus.FAILED,
                    reason="no container exists under the name",
                    observed="sandbox-absent",
                    code="transient-provider-error",
                    changes=Commit(
                        resources=(
                            planned.moved(R.RELEASED, now=self._clock(), note="never created"),
                        )
                    ),
                    lease=lease,
                )
                raise TransientError(f"the agent sandbox did not start: {error}") from error
            raise  # it exists: left ambiguous for reconciliation (#11c)
        return ContainerHandle(result["runtime"], result["id"], result["name"])

    def run_agent(
        self,
        execution: Execution,
        lease: ExecutionLease,
        *,
        handle: ContainerHandle,
        request: AgentRequest,
        stop: StopSignal | None = None,
    ) -> AgentResult:
        """``stop`` (ADR-0025 §2) ends the run inside this journaled effect exactly as
        a timeout does; the operation then succeeds with the outcome ``cancelled``."""

        def effect() -> Mapping[str, str]:
            outcome = self._c.agent_runner.run(
                self._c.container_runtime, handle, request, stop=stop
            )
            return {
                "outcome": outcome.outcome.value,
                "exit_code": "" if outcome.exit_code is None else str(outcome.exit_code),
                "summary": outcome.summary[:_SUMMARY_LIMIT],
            }

        key = operation_key(K.RUN_AGENT, execution.id, execution.attempt)
        if (
            self._store.operation(
                operation_key(K.FREEZE_CANDIDATE, execution.id, execution.attempt)
            )
            is not None
        ):
            raise LedgerIntegrityError(f"{execution.id}: the agent never runs after a freeze")
        try:
            result = self._journal.perform(K.RUN_AGENT, execution, effect, lease=lease)
        except NEVER_TRANSIENT:
            raise
        except ProviderError as error:
            # ADR-0021: an agent run with an unknown outcome abandons the attempt.
            self._journal.reconcile(
                key,
                OperationStatus.FAILED,
                reason="outcome unknown; the attempt is abandoned",
                observed="outcome-unknown",
                code="transient-provider-error",
                lease=lease,
            )
            raise TransientError(f"the agent run was interrupted: {error}") from error
        exit_code = result["exit_code"]
        return AgentResult(
            AgentOutcome(result["outcome"]),
            result["summary"],
            int(exit_code) if exit_code else None,
        )

    def remove_sandbox(
        self, execution: Execution, lease: ExecutionLease, *, resource: Resource
    ) -> None:
        """Remove a recorded sandbox (container, egress sidecar, network) from durable data.

        Journaled as ``stop-container:<execution>:<current attempt>:<name>``.
        The owner comes from the ledger row, so this works after a restart and
        for earlier attempts. A foreign or ambiguous owner, or an unconfirmed
        removal, quarantines the row and raises.
        """
        if resource.kind is not ResourceKind.CONTAINER:
            raise LedgerIntegrityError(f"{resource.key} is not a sandbox")
        current = self._store.resource(resource.key) or resource
        if current.status is R.RELEASED:
            return
        if current.status is R.QUARANTINED:
            raise LedgerIntegrityError(f"{current.key} is quarantined; a human must resolve it")
        owner = ResourceOwner(
            self._c.instance_id, current.execution_id, current.attempt, current.owner_token
        )
        key = operation_key(K.STOP_CONTAINER, execution.id, execution.attempt, current.name)
        if current.status is R.ACTIVE:
            target = current.moved(R.RELEASING, now=self._clock())
            before = Commit(resources=(target,))
        else:  # INTENDED (maybe never created) or RELEASING (removal under way)
            target, before = current, Commit()

        def effect() -> Mapping[str, str]:
            removed = self._c.container_runtime.remove_owned(current.name, owner=owner)
            return {"removed": str(removed).lower()}

        try:
            self._journal.perform(
                K.STOP_CONTAINER,
                execution,
                effect,
                lease=lease,
                step=current.name,
                before=before,
                after=lambda r: Commit(
                    resources=(
                        target.moved(
                            R.RELEASED,
                            now=self._clock(),
                            note="removed" if r["removed"] == "true" else "already gone",
                        ),
                    )
                ),
            )
        except (OwnershipConflictError, ContainmentError) as error:
            latest = self._store.resource(current.key) or current
            code = failure_code(error)
            self._quarantine(
                execution, lease, latest, f"not removed: {type(error).__name__}", code=code
            )
            if (op := self._store.operation(key)) is not None and not op.status.is_final:
                self._journal.reconcile(
                    key,
                    OperationStatus.FAILED,
                    reason=f"quarantined: {type(error).__name__}",
                    observed="resource-quarantined",
                    code=code,
                    lease=lease,
                )
            raise

    # -- settling what a crash left unclear (ADR-0022) ----------------------------------

    def settle_directory_step(
        self, execution: Execution, lease: ExecutionLease, *, kind: OperationKind, path: Path
    ) -> None:
        """An interrupted attempt-directory creation or checkout: observe the directory."""
        key = operation_key(kind, execution.id, execution.attempt)
        planned = self._store.resource(
            resource_key(ResourceKind.WORKSPACE, execution.id, execution.attempt, str(path))
        )
        if planned is not None and planned.status is R.INTENDED:
            self._settle_partial(execution, lease, planned, path)
        self._journal.reconcile(
            key, OperationStatus.FAILED, reason=ABANDONED, observed="outcome-unknown",
            code="interrupted", lease=lease,
        )  # fmt: skip

    def abandon_agent_run(self, execution: Execution, lease: ExecutionLease) -> None:
        self._journal.reconcile(
            operation_key(K.RUN_AGENT, execution.id, execution.attempt),
            OperationStatus.FAILED,
            reason="outcome unknown after a restart; the attempt is abandoned",
            observed="outcome-unknown",
            code="interrupted",
            lease=lease,
        )

    def settle_start(self, execution: Execution, lease: ExecutionLease, *, name: str) -> None:
        self._journal.reconcile(
            operation_key(K.START_CONTAINER, execution.id, execution.attempt, name),
            OperationStatus.FAILED,
            reason="interrupted before the agent ran; the sandbox was removed",
            observed="sandbox-removed",
            code="interrupted",
            lease=lease,
        )

    def settle_post(self, execution: Execution, lease: ExecutionLease, *, step: str) -> None:
        self._journal.reconcile(
            operation_key(K.POST_UPDATE, execution.id, execution.attempt, step),
            OperationStatus.FAILED,
            reason="the note may or may not exist; not repeated",
            observed="outcome-unknown",
            code="interrupted",
            lease=lease,
        )

    def settle_verify(
        self,
        execution: Execution,
        lease: ExecutionLease,
        *,
        run: int,
        candidate: CandidateRevision,
        then: Then,
    ) -> bool:
        """An interrupted verification run, decided by its own evidence (matched by run).

        Its sandboxes are removed first, from the ledger. Returns ``True`` if
        the run had finished and its outcome is now recorded.
        """
        attempt = execution.attempt
        names = {
            verification_container_name(self._c.instance_id, execution.id, attempt, run, job)
            for job, _ in self._c.verification.jobs()
        }
        for row in self._store.resources(execution_id=execution.id):
            if row.kind is ResourceKind.CONTAINER and row.attempt == attempt and row.name in names:
                self.remove_sandbox(self._current(execution), lease, resource=row)
        execution = self._current(execution)
        run_dir = self._c.layout.verification_checkout(execution.id, attempt, run)
        checkout = self._store.resource(
            resource_key(ResourceKind.VERIFICATION_CHECKOUT, execution.id, attempt, str(run_dir))
        )
        settled_checkout = (
            (self._observed_checkout(checkout, run_dir),)
            if checkout is not None and checkout.status is R.INTENDED
            else ()
        )
        key = operation_key(K.VERIFY, execution.id, attempt, f"run-{run}")
        evidence = [
            e for e in self._store.evidence(execution.id) if e.attempt == attempt and e.run == run
        ]
        if len(evidence) == 1 and (evidence[0].candidate_revision, evidence[0].base_revision) == (
            candidate.revision,
            candidate.base_revision,
        ):
            record = evidence[0]
            found = directory_identity(run_dir, root=self._c.layout.workspaces_root)
            result = {
                "verdict": record.verdict.value,
                "evidence_digest": record.digest(),
                "candidate_revision": record.candidate_revision,
                "checkout_identity": "" if found is None else identity_text(found),
            }
            self._journal.reconcile(
                key,
                OperationStatus.SUCCEEDED,
                reason=f"evidence of run {run} exists",
                observed="evidence-present",
                result=result,
                changes=_merge(Commit(resources=settled_checkout), then(result)),
                lease=lease,
            )
            return True
        if evidence:
            raise LedgerIntegrityError(f"{key}: its evidence is for another candidate")
        self._journal.reconcile(
            key,
            OperationStatus.FAILED,
            reason=f"run {run} left no evidence",
            observed="evidence-absent",
            code="interrupted",
            changes=Commit(resources=settled_checkout),
            lease=lease,
        )
        return False

    def settle_publish(
        self,
        execution: Execution,
        lease: ExecutionLease,
        *,
        step: str,
        branch: str,
        expected_head: str | None,
    ) -> bool:
        """An interrupted push, decided by the branch head. Never overwrites anything.

        Returns ``True`` if the branch is at the verified candidate. A head that
        is neither the candidate nor the expected head blocks for a human.
        """
        revision = execution.candidate_revision
        key = operation_key(K.PUBLISH_REVISION, execution.id, execution.attempt, step)
        head = self._c.source_control.branch_head(self._c.target.repository, branch)
        if head is not None and head == revision:
            self._journal.reconcile(
                key,
                OperationStatus.SUCCEEDED,
                reason="the branch is at the candidate",
                observed="branch-at-candidate",
                result={"head": head},
                lease=lease,
            )
            return True
        if head == expected_head:
            self._journal.reconcile(
                key, OperationStatus.FAILED, reason="the branch did not move",
                observed="branch-unmoved", code="interrupted", lease=lease,
            )  # fmt: skip
            return False
        raise LedgerIntegrityError(
            f"{branch} moved to an unverified head; a human must decide (it is never "
            "force-pushed, reset or overwritten)"
        )

    def settle_delete(self, execution: Execution, lease: ExecutionLease, *, step: str) -> bool:
        """An interrupted workspace deletion, decided by the directory. ``True`` if gone."""
        path = self._c.layout.attempt_dir(execution.id, execution.attempt)
        key = operation_key(K.DELETE_WORKSPACE, execution.id, execution.attempt, step)
        group = self._attempt_tree(execution, path, statuses={R.RELEASING})
        top = self._store.resource(
            resource_key(ResourceKind.WORKSPACE, execution.id, execution.attempt, str(path))
        )
        if top is None or top.identity is None:
            raise LedgerIntegrityError(f"{key}: no recorded attempt directory")
        found = directory_identity(path, root=self._c.layout.workspaces_root)
        now = self._clock()
        if found is None:
            self._journal.reconcile(
                key,
                OperationStatus.SUCCEEDED,
                reason="the directory is gone",
                observed="directory-absent",
                result={"removed": "true"},
                changes=Commit(
                    resources=tuple(r.moved(R.RELEASED, now=now, note="deleted") for r in group)
                ),
                lease=lease,
            )
            return True
        if identity_text(found) == top.identity:
            self._journal.reconcile(
                key, OperationStatus.FAILED, reason="the directory is still there",
                observed="directory-unchanged", code="interrupted", lease=lease,
            )  # fmt: skip
            return False
        self._journal.reconcile(
            key,
            OperationStatus.FAILED,
            reason="the directory was replaced",
            observed="directory-replaced",
            code="resource-ownership-conflict",
            changes=_quarantined(
                tuple(r.moved(R.QUARANTINED, now=now, note="replaced") for r in group),
                "resource-ownership-conflict",
            ),
            lease=lease,
        )
        raise LedgerIntegrityError(f"{path} was replaced; it is quarantined, never deleted")

    def load_candidate(self, execution: Execution) -> CandidateRevision:
        """The frozen candidate, rebuilt and proven equal to what the freeze recorded."""
        freeze = self._store.operation(
            operation_key(K.FREEZE_CANDIDATE, execution.id, execution.attempt)
        )
        if (
            freeze is None
            or execution.candidate_revision is None
            or execution.base_revision is None
        ):
            raise LedgerIntegrityError(f"{execution.id} has no frozen candidate to load")
        try:
            candidate = self._c.source_control.load_candidate(
                self._c.target.repository,
                base_revision=execution.base_revision,
                revision=execution.candidate_revision,
            )
        except CandidateRejectedError as error:
            # Freezing succeeded, so this is the coordinator's own repository
            # losing or changing a recorded candidate: an integrity problem
            # for a human, not a failure of the agent's work.
            raise LedgerIntegrityError(
                f"{execution.id}: the frozen candidate cannot be reloaded: {error}"
            ) from error
        check_candidate(candidate, freeze)
        return candidate

    # -- candidate, verification and publication ---------------------------------------

    def freeze(
        self, execution: Execution, lease: ExecutionLease, *, message: str, then: Then
    ) -> CandidateRevision:
        if execution.base_revision is None:
            raise LedgerIntegrityError(f"{execution.id} has no base revision to freeze on")
        frozen: list[CandidateRevision] = []
        base = execution.base_revision
        workspace = self._c.layout.agent_workspace(execution.id, execution.attempt)

        def effect() -> Mapping[str, str]:
            candidate = self._c.source_control.freeze_candidate(
                self._c.target.repository,
                base_revision=base,
                workspace=workspace,
                message=message,
            )
            if candidate.base_revision != base:
                raise LedgerIntegrityError("the freeze answered for another base revision")
            frozen.append(candidate)
            return freeze_result(candidate)

        self._journal.perform(
            K.FREEZE_CANDIDATE,
            execution,
            effect,
            lease=lease,
            intent={"base_revision": base},
            after=then,
            definitive=(CandidateRejectedError,),
        )
        if not frozen:  # replayed from the journal: the object itself is gone
            raise LedgerIntegrityError(
                f"{execution.id}: the frozen candidate must be rebuilt from the coordinator's "
                "repository (#11c)"
            )
        return frozen[0]

    def verify(
        self,
        execution: Execution,
        lease: ExecutionLease,
        *,
        candidate: CandidateRevision,
        agent_result: AgentResult,
        owner: ResourceOwner,
        run: int,
        then: Then,
    ) -> EvidenceRecord:
        layout, policy = self._c.layout, self._c.verification
        run_dir = layout.verification_checkout(execution.id, execution.attempt, run)
        containers = [
            self._planned(
                ResourceKind.CONTAINER,
                execution,
                verification_container_name(
                    self._c.instance_id, execution.id, execution.attempt, run, job
                ),
                owner.token,
            )
            for job, _ in policy.jobs()
        ]
        checkout = self._planned(
            ResourceKind.VERIFICATION_CHECKOUT, execution, str(run_dir), owner.token
        )
        records: list[EvidenceRecord] = []

        def effect() -> Mapping[str, str]:
            record = self._verifier.verify(
                execution=execution,
                repository=self._c.target.repository,
                candidate=candidate,
                agent_result=agent_result,
                owner=owner,
                run=run,
            )
            records.append(record)
            found = directory_identity(run_dir, root=layout.workspaces_root)
            return {
                "verdict": record.verdict.value,
                "evidence_digest": record.digest(),
                "candidate_revision": record.candidate_revision,
                "checkout_identity": "" if found is None else identity_text(found),
            }

        def after(result: Mapping[str, str]) -> Commit:
            now = self._clock()
            done = tuple(c.moved(R.RELEASED, now=now) for c in containers)  # stopped by the run
            if result["checkout_identity"]:
                kept = self._activate(checkout, result["checkout_identity"])
            else:
                kept = checkout.moved(R.RELEASED, now=now, note="nothing was checked out")
            return _merge(Commit(resources=(*done, kept)), then(result))

        try:
            self._journal.perform(
                K.VERIFY,
                execution,
                effect,
                lease=lease,
                step=f"run-{run}",
                intent={"candidate_revision": candidate.revision},
                before=Commit(resources=(*containers, checkout)),
                after=after,
            )
        except ContainmentError as error:
            for planned in containers:
                if planned.name == error.name:
                    self._quarantine(
                        execution, lease, planned, "removal could not be confirmed",
                        code="containment-unconfirmed",
                    )  # fmt: skip
            raise
        except NEVER_TRANSIENT:
            raise
        except ProviderError as error:
            key = operation_key(K.VERIFY, execution.id, execution.attempt, f"run-{run}")
            left = [c for c in containers if self._c.container_runtime.find(c.name) is not None]
            if not left:
                self._journal.reconcile(
                    key,
                    OperationStatus.FAILED,
                    reason="verification did not finish and left no sandbox",
                    observed="sandbox-absent",
                    code="transient-provider-error",
                    changes=Commit(
                        resources=(
                            *(
                                c.moved(
                                    R.RELEASED, now=self._clock(), note="removed or never created"
                                )
                                for c in containers
                            ),
                            self._observed_checkout(checkout, run_dir),
                        )
                    ),
                    lease=lease,
                )
                raise TransientError(f"verification did not finish: {error}") from error
            raise
        if not records:
            raise LedgerIntegrityError(f"{execution.id}: verification was replayed, not run")
        return records[0]

    def publish(
        self,
        execution: Execution,
        lease: ExecutionLease,
        *,
        branch: str,
        expected_head: str | None,
        step: str = "",
    ) -> None:
        revision = execution.candidate_revision
        if revision is None:
            raise LedgerIntegrityError(f"{execution.id} has no candidate to publish")
        scm, repository = self._c.source_control, self._c.target.repository

        def effect() -> Mapping[str, str]:
            scm.publish_revision(
                repository, revision=revision, branch=branch, expected_head=expected_head
            )
            head = scm.branch_head(repository, branch)
            if head != revision:
                raise LedgerIntegrityError(f"{branch} is at {head}, not the verified candidate")
            return {"head": head}

        key = operation_key(K.PUBLISH_REVISION, execution.id, execution.attempt, step)
        try:
            self._journal.perform(
                K.PUBLISH_REVISION,
                execution,
                effect,
                lease=lease,
                step=step,
                intent={"revision": revision, "branch": branch, "expected": expected_head or ""},
                definitive=(LeaseConflictError, NonFastForwardError),
            )
        except (LeaseConflictError, NonFastForwardError):
            raise
        except NEVER_TRANSIENT:
            raise
        except ProviderError as error:
            head = scm.branch_head(repository, branch)  # observe
            if head == revision:
                self._journal.reconcile(
                    key,
                    OperationStatus.SUCCEEDED,
                    reason="the branch is at the candidate",
                    observed="branch-at-candidate",
                    result={"head": revision},
                    lease=lease,
                )
                return
            if head == expected_head:
                self._journal.reconcile(
                    key, OperationStatus.FAILED, reason="the branch did not move",
                    observed="branch-unmoved", code="transient-provider-error", lease=lease,
                )  # fmt: skip
                raise TransientError(f"the push did not happen: {error}") from error
            raise  # the branch moved elsewhere: left ambiguous for a human

    def upsert_change_request(
        self,
        execution: Execution,
        lease: ExecutionLease,
        *,
        spec: ChangeRequestSpec,
        owner_token: str,
        verified: str,
        then: Then,
    ) -> ChangeRequest:
        """Open or update the change request for ``verified``.

        The result records the branch head right after: ``then`` decides,
        and must refuse to call the execution complete if it is no longer
        ``verified`` (someone pushed in between).
        """
        planned = self._store.resource(
            resource_key(
                ResourceKind.CHANGE_REQUEST, execution.id, execution.attempt, spec.source_branch
            )
        ) or self._planned(ResourceKind.CHANGE_REQUEST, execution, spec.source_branch, owner_token)
        first = self._store.resource(planned.key) is None
        made: list[ChangeRequest] = []
        scm = self._c.source_control

        def effect() -> Mapping[str, str]:
            request = scm.upsert_change_request(spec)
            made.append(request)
            head = scm.branch_head(spec.repository, spec.source_branch)
            return {
                "key": request.key,
                "url": request.url,
                "labels": ",".join(request.labels),
                "head_after": head or "",
                "verified": verified,
            }

        result = self._journal.perform(
            K.UPSERT_CHANGE_REQUEST,
            execution,
            effect,
            lease=lease,
            intent={"source_branch": spec.source_branch, "target_branch": spec.target_branch},
            before=Commit(resources=(planned,)) if first else Commit(),
            after=lambda r: _merge(
                Commit(resources=(self._activate(planned, r["key"]),))
                if planned.status is R.INTENDED
                else Commit(),
                then(r),
            ),
            definitive=(OwnershipConflictError,),
        )
        if made:
            return made[0]
        return ChangeRequest(
            repository=spec.repository,
            key=result["key"],
            url=result["url"],
            source_branch=spec.source_branch,
            target_branch=spec.target_branch,
            labels=tuple(filter(None, result["labels"].split(","))),
        )

    def stop_tracking_change_request(
        self,
        execution: Execution,
        lease: ExecutionLease,
        *,
        resource: Resource,
        note: str,
        step: str,
        observed: str,
    ) -> None:
        """The task is complete (merged) or abandoned (closed): release Ilmar's claim.

        Resumes a resource left ``RELEASING`` by an earlier, interrupted pass.
        ``observed`` is the change-request state that decided it
        (``change-request-merged`` or ``-closed``): it is recorded on the
        release's events, in the commits that move the resource (ADR-0024 §1).
        """
        if resource.status is R.RELEASING:
            releasing, before = resource, Commit()
        else:
            releasing = resource.moved(R.RELEASING, now=self._clock())
            before = Commit(resources=(releasing,))

        def effect() -> Mapping[str, str]:
            self._c.task_provider.release(execution.task)
            return {}

        self._journal.perform(
            K.RELEASE_TASK,
            execution,
            effect,
            lease=lease,
            step=step,
            before=before,
            after=lambda _: Commit(
                resources=(releasing.moved(R.RELEASED, now=self._clock(), note=note),)
            ),
            observed=observed,
        )

    # -- ledger helpers (no external effect) --------------------------------------------

    def _planned(
        self, kind: ResourceKind, execution: Execution, name: str, owner_token: str
    ) -> Resource:
        return Resource.intended(
            kind,
            execution.id,
            execution.attempt,
            name,
            owner_token=owner_token,
            now=self._clock(),
        )

    def _activate(self, planned: Resource, identity: str) -> Resource:
        return planned.moved(R.ACTIVE, now=self._clock(), identity=identity)

    def _quarantine(
        self,
        execution: Execution,
        lease: ExecutionLease,
        resource: Resource,
        note: str,
        *,
        code: str,
    ) -> None:
        current = self._store.resource(resource.key) or resource
        if current.status in (R.RELEASED, R.QUARANTINED):
            return
        moved = current.moved(R.QUARANTINED, now=self._clock(), note=note)
        self._commit(lease, execution, _quarantined((moved,), code))

    def _observed_checkout(self, checkout: Resource, run_dir: Path) -> Resource:
        """What a verification run left on disk, as the ledger should record it."""
        found = directory_identity(run_dir, root=self._c.layout.workspaces_root)
        if found is None:
            return checkout.moved(R.RELEASED, now=self._clock(), note="never created")
        return checkout.moved(
            R.ACTIVE, now=self._clock(), identity=identity_text(found), note="interrupted run"
        )

    def _settle_partial(
        self, execution: Execution, lease: ExecutionLease, planned: Resource, path: Path
    ) -> None:
        """After a failed checkout, record what is actually on disk."""
        current = self._store.resource(planned.key)
        if current is None or current.status is not R.INTENDED:
            return
        found = directory_identity(path, root=self._c.layout.workspaces_root)
        if found is None:
            moved = current.moved(R.RELEASED, now=self._clock(), note="never created")
        else:
            moved = current.moved(
                R.ACTIVE, now=self._clock(), identity=identity_text(found), note="partial checkout"
            )
        self._commit(lease, execution, Commit(resources=(moved,)))

    def _current(self, execution: Execution) -> Execution:
        found = self._store.get(execution.id)
        if found is None:
            raise LedgerIntegrityError(f"execution {execution.id} vanished")
        return found

    def _commit(self, lease: ExecutionLease, execution: Execution, changes: Commit) -> None:
        self._journal.record(changes, lease=lease, execution_id=execution.id)


def _quarantined(resources: tuple[Resource, ...], code: str) -> Commit:
    return Commit(resources=resources, reasons={r.key: code for r in resources})


def _merge(first: Commit, second: Commit) -> Commit:
    return Commit(
        created=first.created + second.created,
        applied=first.applied + second.applied,
        operations=first.operations + second.operations,
        resources=first.resources + second.resources,
        expected=first.expected + second.expected,
        reasons={**first.reasons, **second.reasons},
        extra_events=first.extra_events + second.extra_events,
    )
