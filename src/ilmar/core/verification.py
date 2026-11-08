# SPDX-License-Identifier: Apache-2.0
"""VERIFYING: independent checks on a frozen candidate (ADR-0010, ADR-0018).

The :class:`Verifier` decides only from evidence Ilmar produces itself:

1. **Independent checks** read the candidate's diff (``changed_paths``) as
   data against the policy's path classes. Nothing is executed.
2. If CI configuration changed, nothing of the candidate runs, and the
   verdict is ``REJECTED_CI_CONFIG``: a human must take over (ADR-0010 §5).
3. Otherwise each **job** of the policy gets its own fresh checkout of the
   candidate revision and its own fresh sandbox: network ``NONE``, no
   credential, the policy's image and limits. The job's commands run there in
   order and share that checkout and sandbox, like the steps of a CI job; no
   two jobs ever share files or processes (the isolation boundary, ADR-0018).
   Pass or fail comes only from the exit status and timeout the harness
   measures. Output is reduced to a digest; reports the candidate writes are
   never read.
4. The verdict follows the ADR-0010 §5 table. The agent's own report is kept
   in the record, labelled as reported, and never influences the verdict.

The record is appended to the execution store before it is returned.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from datetime import datetime

from ilmar.core.event_context import PassEvents
from ilmar.core.layout import WorkspaceLayout
from ilmar.core.naming import verification_container_name
from ilmar.core.safe_fs import directory_identity, ensure_directory
from ilmar.domain.agent import AgentResult
from ilmar.domain.changes import CandidateRevision, RepositoryRef
from ilmar.domain.errors import ContainmentError, ProviderError
from ilmar.domain.execution import Execution, ExecutionState
from ilmar.domain.ledger import Fence
from ilmar.domain.verification import (
    AgentReport,
    CheckClass,
    CheckEvidence,
    EvidenceRecord,
    Independence,
    Verdict,
    VerificationCommand,
    VerificationPolicy,
    matching,
)
from ilmar.domain.workspace import (
    CommandResult,
    ContainerHandle,
    ContainerSpec,
    NetworkPolicy,
    ResourceOwner,
)
from ilmar.ports.container_runtime import ContainerRuntime
from ilmar.ports.execution_store import ExecutionStore
from ilmar.ports.source_control import SourceControlProvider

REVIEW_LABEL = "ilmar:needs-human-review"
_SUMMARY_LIMIT = 2000
_DETAIL_LIMIT = 200


class Verifier:
    def __init__(
        self,
        *,
        source_control: SourceControlProvider,
        runtime: ContainerRuntime,
        store: ExecutionStore,
        layout: WorkspaceLayout,
        policy: VerificationPolicy,
        instance_id: str,
        clock: Callable[[], datetime],
        fence: Fence | None = None,
        events: PassEvents | None = None,
    ) -> None:
        self._source_control = source_control
        self._runtime = runtime
        self._store = store
        self._layout = layout
        self._policy = policy
        self._instance = instance_id
        self._clock = clock
        self._fence = fence  # evidence is a fenced write once a coordinator owns the store
        self._events = events

    def verify(
        self,
        *,
        execution: Execution,
        repository: RepositoryRef,
        candidate: CandidateRevision,
        agent_result: AgentResult,
        owner: ResourceOwner,
        run: int = 1,
    ) -> EvidenceRecord:
        """Verify ``candidate`` for ``execution``; append and return the evidence."""
        _check_inputs(execution, candidate, owner)
        policy = self._policy
        changed = candidate.changed_paths
        protected = matching(changed, policy.protected_paths)
        influencing = matching(changed, policy.check_influencing_paths)
        ci = matching(changed, policy.ci_config_paths)

        checks = [
            CheckEvidence(
                "ci-config-paths",
                CheckClass.INDEPENDENT,
                passed=not ci,
                detail=_paths_detail("changes CI configuration", ci),
            ),
            CheckEvidence(
                "protected-paths",
                CheckClass.INDEPENDENT,
                passed=True,
                detail=_paths_detail("flags protected paths", protected),
            ),
            CheckEvidence(
                "check-influencing-paths",
                CheckClass.INDEPENDENT,
                passed=True,
                detail=_paths_detail("flags check-influencing paths", influencing),
            ),
        ]
        reasons: list[str] = []
        if ci:
            reasons.append(f"ci-config-modified: {', '.join(ci)}")
        if influencing:
            reasons.append(f"modified-checks: {', '.join(influencing)}")
        if protected:
            reasons.append(f"protected-paths: {', '.join(protected)}")

        if ci:
            independence, verdict = Independence.CI_CONFIG_MODIFIED, Verdict.REJECTED_CI_CONFIG
        else:
            checks += self._run_candidate_checks(execution, repository, candidate, owner, run)
            independence = (
                Independence.MODIFIED_CHECKS if influencing else Independence.UNCHANGED_CHECKS
            )
            if any(c.required and not c.passed for c in checks):
                verdict = Verdict.REJECTED_CHECKS_FAILED
            elif reasons:
                verdict = Verdict.PUBLISH_FOR_REVIEW
            else:
                verdict = Verdict.PUBLISH

        record = EvidenceRecord(
            execution_id=execution.id,
            attempt=execution.attempt,
            recorded_at=self._clock(),
            base_revision=candidate.base_revision,
            candidate_revision=candidate.revision,
            policy_digest=policy.digest(),
            checks=tuple(checks),
            protected_paths=protected,
            check_influencing_paths=influencing,
            ci_config_paths=ci,
            independence=independence,
            verdict=verdict,
            requires_human_review=bool(reasons),
            review_reasons=tuple(reasons),
            agent_report=AgentReport(
                agent_result.outcome.value, agent_result.summary[:_SUMMARY_LIMIT]
            ),
            run=run,
        )
        context = (
            self._events.context(self._fence)
            if self._events is not None and self._fence is not None
            else None
        )
        self._store.append_evidence(record, fence=self._fence, context=context)
        return record

    # -- candidate-executed checks ---------------------------------------------------

    def _run_candidate_checks(
        self,
        execution: Execution,
        repository: RepositoryRef,
        candidate: CandidateRevision,
        owner: ResourceOwner,
        run: int,
    ) -> list[CheckEvidence]:
        layout = self._layout
        run_directory = layout.verification_checkout(execution.id, execution.attempt, run)
        if directory_identity(run_directory, root=layout.workspaces_root) is not None:
            # A run's directories are never reused, whatever is (or is not) inside.
            raise ProviderError(f"verification run {run} of {execution.id} already exists")
        ensure_directory(run_directory, root=layout.workspaces_root)
        results: list[CheckEvidence] = []
        for job, commands in self._policy.jobs():
            results += self._run_job(execution, repository, candidate, owner, run, job, commands)
        return results

    def _run_job(
        self,
        execution: Execution,
        repository: RepositoryRef,
        candidate: CandidateRevision,
        owner: ResourceOwner,
        run: int,
        job: str,
        commands: tuple[VerificationCommand, ...],
    ) -> list[CheckEvidence]:
        """One job: a fresh checkout and a fresh sandbox, shared by its commands only."""
        layout = self._layout
        checkout = layout.verification_checkout(execution.id, execution.attempt, run) / job
        self._source_control.checkout_revision(
            repository, revision=candidate.revision, destination=checkout
        )
        identity = directory_identity(checkout, root=layout.workspaces_root)
        if identity is None:
            raise ProviderError(f"verification checkout {checkout} vanished")
        handle = self._runtime.start(
            ContainerSpec(
                image=self._policy.image,
                host_workspace=checkout,
                name=verification_container_name(
                    self._instance, execution.id, execution.attempt, run, job
                ),
                owner=owner,
                workspace_identity=identity,
                network=NetworkPolicy.NONE,  # always, and no credential (ADR-0011 §7)
                limits=self._policy.limits,
            )
        )
        results: list[CheckEvidence] = []
        sandbox_gone = False  # the runtime removed it after a timeout (ADR-0014)
        ended: str | None = None
        try:
            for command in commands:
                if ended is not None:
                    results.append(_not_run(command, ended))
                    continue
                try:
                    result = self._runtime.run(
                        handle, list(command.argv), timeout_seconds=command.timeout_seconds
                    )
                except ContainmentError:
                    raise
                except ProviderError:
                    if self._runtime.is_running(handle):
                        raise  # an infrastructure failure, not the candidate's doing
                    # The candidate's code can end its own sandbox (for example by
                    # killing every process). That fails the check; it is not a
                    # reason to produce no evidence.
                    results.append(_sandbox_stopped(command))
                    ended = "the job's sandbox stopped unexpectedly"
                    continue
                results.append(_evidence(command, result))
                if result.timed_out:
                    sandbox_gone = True
                    ended = "the job's sandbox ended after an earlier timeout"
        finally:
            if not sandbox_gone:
                self._stop(handle, owner)
        return results

    def _stop(self, handle: ContainerHandle, owner: ResourceOwner) -> None:
        self._runtime.stop(handle, owner=owner)


def _check_inputs(execution: Execution, candidate: CandidateRevision, owner: ResourceOwner) -> None:
    if execution.state is not ExecutionState.VERIFYING:
        raise ValueError(f"execution {execution.id} is {execution.state.value}, not verifying")
    if (execution.base_revision, execution.candidate_revision) != (
        candidate.base_revision,
        candidate.revision,
    ):
        raise ValueError("the candidate is not the one recorded for this attempt")
    if (owner.execution_id, owner.attempt) != (execution.id, execution.attempt):
        raise ValueError("the resource owner belongs to another execution or attempt")


def _evidence(command: VerificationCommand, result: CommandResult) -> CheckEvidence:
    digest = hashlib.sha256(
        result.stdout.encode("utf-8", "replace") + b"\0" + result.stderr.encode("utf-8", "replace")
    ).hexdigest()
    if result.timed_out:
        detail = f"timed out after {command.timeout_seconds:g}s"
    else:
        detail = f"exit {result.exit_code}"
        tail = result.stderr.strip().splitlines()[-1:] if result.exit_code else []
        if tail:
            detail += f"; last stderr line (reported by the candidate): {_printable(tail[0])}"
    return CheckEvidence(
        name=command.name,
        check_class=CheckClass.CANDIDATE_EXECUTED,
        passed=result.exit_code == 0 and not result.timed_out,
        detail=detail,
        command=command.argv,
        exit_code=None if result.timed_out else result.exit_code,
        timed_out=result.timed_out,
        duration_seconds=round(result.duration_seconds, 3),
        output_digest=f"sha256:{digest}",
        required=command.required,
        job=command.job,
    )


def _printable(text: str) -> str:
    """Candidate text made safe to show: no control characters or escape sequences."""
    return "".join(c if c.isprintable() else "\ufffd" for c in text)[:_DETAIL_LIMIT]


def _sandbox_stopped(command: VerificationCommand) -> CheckEvidence:
    return CheckEvidence(
        name=command.name,
        check_class=CheckClass.CANDIDATE_EXECUTED,
        passed=False,
        detail="failed: the sandbox stopped while the command ran",
        command=command.argv,
        required=command.required,
        job=command.job,
    )


def _not_run(command: VerificationCommand, reason: str) -> CheckEvidence:
    return CheckEvidence(
        name=command.name,
        check_class=CheckClass.CANDIDATE_EXECUTED,
        passed=False,
        detail=f"not run: {reason}",
        command=command.argv,
        required=command.required,
        job=command.job,
    )


def _paths_detail(what: str, paths: tuple[str, ...]) -> str:
    if not paths:
        return "none"
    shown = ", ".join(paths[:20])
    more = f" and {len(paths) - 20} more" if len(paths) > 20 else ""
    return f"{what}: {shown}{more}"
