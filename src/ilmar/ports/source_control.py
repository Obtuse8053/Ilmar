# SPDX-License-Identifier: Apache-2.0
"""Port for source-control platforms (repositories, branches, change requests)."""

from __future__ import annotations

from pathlib import Path
from typing import Protocol

from ilmar.domain.changes import (
    CandidateRevision,
    ChangeRequest,
    ChangeRequestSpec,
    ChangeRequestState,
    ChecksSummary,
    RepositoryRef,
)


class SourceControlProvider(Protocol):
    """Moves code between the platform, the agent workspace and verification.

    Implementations run in the coordinator process and are the only component
    that holds source-control credentials (ADR-0006). Agent output is never
    trusted as a repository: it is frozen as plain file content into a
    coordinator-owned repository, and exactly that revision is verified and
    published (ADR-0010). Contract: ``tests/contracts/source_control.py``.
    """

    @property
    def name(self) -> str: ...

    def branch_head(self, repository: RepositoryRef, branch: str) -> str | None:
        """The revision ``branch`` points at on the platform, or ``None`` if it does not exist."""
        ...

    def checkout(
        self,
        repository: RepositoryRef,
        *,
        base_branch: str,
        branch: str,
        destination: Path,
    ) -> str:
        """Create the agent's workspace at ``destination`` and return its base revision.

        The workspace receives the content of ``branch`` if it exists on the
        platform (so an interrupted task resumes), otherwise of ``base_branch``.
        It holds no credentials. ``destination`` must not exist
        (``ProviderError``).
        """
        ...

    def freeze_candidate(
        self,
        repository: RepositoryRef,
        *,
        base_revision: str,
        workspace: Path,
        message: str,
    ) -> CandidateRevision:
        """Record the workspace content as a new revision on top of ``base_revision``.

        The workspace is plain data: a top-level ``.git`` is ignored and nothing
        in the workspace is ever executed or read as configuration. Symlinks
        are stored as links and never followed. Nested repositories and special
        files raise ``CandidateRejectedError``. Freezing the same content on the
        same base with the same message returns the same revision.
        """
        ...

    def load_candidate(
        self,
        repository: RepositoryRef,
        *,
        base_revision: str,
        revision: str,
    ) -> CandidateRevision:
        """Rebuild a candidate this coordinator froze earlier (ADR-0022), read only.

        ``changed_paths`` are recomputed from the stored revisions exactly as
        ``freeze_candidate`` computed them. Fails closed with
        ``CandidateRejectedError`` if ``revision`` was not frozen by this
        coordinator or its parent is not ``base_revision``. The caller still
        checks the result against the journal's changed-paths digest.
        """
        ...

    def checkout_revision(
        self,
        repository: RepositoryRef,
        *,
        revision: str,
        destination: Path,
    ) -> None:
        """Write exactly the tree of ``revision`` into ``destination``, which must not exist."""
        ...

    def publish_revision(
        self,
        repository: RepositoryRef,
        *,
        revision: str,
        branch: str,
        expected_head: str | None,
    ) -> None:
        """Make ``branch`` on the platform point exactly at ``revision``. Never commits.

        No-op if the branch already points at ``revision``. Otherwise:

        * the branch head must equal ``expected_head`` (``None``: the branch must
          not exist), or ``LeaseConflictError`` is raised;
        * if the branch exists, ``revision`` must descend from its head
          (fast-forward only), or ``NonFastForwardError`` is raised. Published
          history is never rewritten.

        In both error cases nothing changes. An unknown revision raises
        ``ProviderError``.
        """
        ...

    def upsert_change_request(self, spec: ChangeRequestSpec) -> ChangeRequest:
        """Open a change request for ``spec.source_branch``, or update the one Ilmar opened.

        Idempotent: at most one Ilmar change request exists per source branch.
        An open change request on that branch that Ilmar did not open (no
        marker) raises ``OwnershipConflictError`` and is left unchanged.
        """
        ...

    def checks(self, change_request: ChangeRequest) -> ChecksSummary: ...

    def change_request_state(self, change_request: ChangeRequest) -> ChangeRequestState:
        """Whether ``change_request`` is open, merged or closed without merging. Read only."""
        ...
