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

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from ilmar.core.components import Components
from ilmar.core.journal import Journal, freeze_result
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
    IlmarError,
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
from ilmar.domain.tasks import ClaimOutcome
from ilmar.domain.verification import EvidenceRecord
from ilmar.domain.workspace import (
    ContainerHandle,
    ContainerSpec,
    DirectoryIdentity,
    ResourceOwner,
)
from ilmar.ports.execution_store import ExecutionStore

K = OperationKind
R = ResourceStatus
Then = Callable[[Mapping[str, str]], Commit]
_SUMMARY_LIMIT = 2000


class TransientError(IlmarError):
    """The step failed for a reason a repetition may not meet (ADR-0021)."""


class DeterministicError(IlmarError):
    """The step failed in a way that repeating it would only reproduce (ADR-0021)."""


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
    def _store(self) -> ExecutionStore:
        return self._c.execution_store

    # -- the task --------------------------------------------------------------------

    def claim(self, execution: Execution, lease: ExecutionLease, *, then: Then) -> ClaimOutcome:
        def effect() -> Mapping[str, str]:
            return {"outcome": self._c.task_provider.claim(execution.task).value}

        result = self._journal.perform(K.CLAIM_TASK, execution, effect, lease=lease, after=then)
        return ClaimOutcome(result["outcome"])

    def release(self, execution: Execution, lease: ExecutionLease, *, step: str) -> None:
        def effect() -> Mapping[str, str]:
            self._c.task_provider.release(execution.task)
            return {}

        self._journal.perform(K.RELEASE_TASK, execution, effect, lease=lease, step=step)

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
                key, OperationStatus.FAILED, reason="the note may or may not exist; not repeated"
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
            raise DeterministicError(f"checkout failed: {type(error).__name__}") from error
        return Checkout(
            result["base_revision"],
            result["branch_head"] or None,
            parse_identity(result["identity"]),
        )

    def delete_attempt(self, execution: Execution, lease: ExecutionLease) -> bool:
        """Delete a finished attempt's directory and record every resource inside as released."""
        layout = self._c.layout
        path = layout.attempt_dir(execution.id, execution.attempt)
        top = self._store.resource(
            resource_key(ResourceKind.WORKSPACE, execution.id, execution.attempt, str(path))
        )
        if top is None or top.status is not R.ACTIVE or top.identity is None:
            return False
        inside = [
            r
            for r in self._store.resources(execution_id=execution.id, statuses={R.ACTIVE})
            if r.attempt == execution.attempt
            and r.kind in (ResourceKind.WORKSPACE, ResourceKind.VERIFICATION_CHECKOUT)
            and r.key != top.key
            and Path(r.name).is_relative_to(path)
        ]
        now = self._clock()
        releasing = [r.moved(R.RELEASING, now=now) for r in (top, *inside)]
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
                intent={"path": str(path)},
                before=Commit(resources=tuple(releasing)),
                after=lambda _: Commit(
                    resources=tuple(
                        r.moved(R.RELEASED, now=self._clock(), note="deleted after success")
                        for r in releasing
                    )
                ),
                definitive=(OwnershipConflictError,),
            )
        except OwnershipConflictError:
            self._commit(
                lease,
                execution,
                Commit(
                    resources=tuple(
                        r.moved(R.QUARANTINED, now=self._clock(), note="replaced on disk")
                        for r in releasing
                    )
                ),
            )
            raise
        return True

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
            self._quarantine(execution, lease, planned, "start could not be cleaned up")
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
                    changes=Commit(
                        resources=(
                            planned.moved(R.RELEASED, now=self._clock(), note="never created"),
                        )
                    ),
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
    ) -> AgentResult:
        def effect() -> Mapping[str, str]:
            outcome = self._c.agent_runner.run(self._c.container_runtime, handle, request)
            return {
                "outcome": outcome.outcome.value,
                "exit_code": "" if outcome.exit_code is None else str(outcome.exit_code),
                "summary": outcome.summary[:_SUMMARY_LIMIT],
            }

        key = operation_key(K.RUN_AGENT, execution.id, execution.attempt)
        try:
            result = self._journal.perform(K.RUN_AGENT, execution, effect, lease=lease)
        except NEVER_TRANSIENT:
            raise
        except ProviderError as error:
            # ADR-0021: an agent run with an unknown outcome abandons the attempt.
            self._journal.reconcile(
                key, OperationStatus.FAILED, reason="outcome unknown; the attempt is abandoned"
            )
            raise TransientError(f"the agent run was interrupted: {error}") from error
        exit_code = result["exit_code"]
        return AgentResult(
            AgentOutcome(result["outcome"]),
            result["summary"],
            int(exit_code) if exit_code else None,
        )

    def stop_agent_sandbox(
        self,
        execution: Execution,
        lease: ExecutionLease,
        *,
        handle: ContainerHandle,
        owner: ResourceOwner,
    ) -> None:
        key = resource_key(ResourceKind.CONTAINER, execution.id, execution.attempt, handle.name)
        active = self._store.resource(key)
        if active is None or active.status is not R.ACTIVE:
            raise LedgerIntegrityError(f"{key} is not an active resource")
        releasing = active.moved(R.RELEASING, now=self._clock())

        def effect() -> Mapping[str, str]:
            self._c.container_runtime.stop(handle, owner=owner)
            return {}

        try:
            self._journal.perform(
                K.STOP_CONTAINER,
                execution,
                effect,
                lease=lease,
                step=handle.name,
                before=Commit(resources=(releasing,)),
                after=lambda _: Commit(resources=(releasing.moved(R.RELEASED, now=self._clock()),)),
            )
        except ContainmentError:
            self._quarantine(execution, lease, releasing, "removal could not be confirmed")
            raise

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
                    self._quarantine(execution, lease, planned, "removal could not be confirmed")
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
                    result={"head": revision},
                )
                return
            if head == expected_head:
                self._journal.reconcile(
                    key, OperationStatus.FAILED, reason="the branch did not move"
                )
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
        planned = self._planned(
            ResourceKind.CHANGE_REQUEST, execution, spec.source_branch, owner_token
        )
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
            before=Commit(resources=(planned,)),
            after=lambda r: _merge(Commit(resources=(self._activate(planned, r["key"]),)), then(r)),
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
    ) -> None:
        """The task is complete (merged) or abandoned (closed): release Ilmar's claim.

        Resumes a resource left ``RELEASING`` by an earlier, interrupted pass.
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
        self, execution: Execution, lease: ExecutionLease, resource: Resource, note: str
    ) -> None:
        current = self._store.resource(resource.key) or resource
        if current.status in (R.RELEASED, R.QUARANTINED):
            return
        self._commit(
            lease,
            execution,
            Commit(resources=(current.moved(R.QUARANTINED, now=self._clock(), note=note),)),
        )

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

    def _commit(self, lease: ExecutionLease, execution: Execution, changes: Commit) -> None:
        lease.check(execution.id)
        self._store.commit(self._journal.fence, changes)


def _merge(first: Commit, second: Commit) -> Commit:
    return Commit(
        created=first.created + second.created,
        applied=first.applied + second.applied,
        operations=first.operations + second.operations,
        resources=first.resources + second.resources,
        expected=first.expected + second.expected,
    )
