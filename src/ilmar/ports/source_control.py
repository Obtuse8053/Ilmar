# SPDX-License-Identifier: Apache-2.0
"""Port for source-control platforms (repositories, branches, change requests)."""

from __future__ import annotations

from pathlib import Path
from typing import Protocol

from ilmar.domain.changes import ChangeRequest, ChangeRequestSpec, ChecksSummary, RepositoryRef


class SourceControlProvider(Protocol):
    """Moves code between the platform and a local working copy.

    Implementations run in the coordinator process and are the only component
    that holds source-control credentials. The agent sandbox never calls this
    port (ADR-0006).
    """

    @property
    def name(self) -> str: ...

    def checkout(
        self,
        repository: RepositoryRef,
        *,
        base_branch: str,
        branch: str,
        destination: Path,
    ) -> None:
        """Create a working copy at ``destination`` with ``branch`` checked out.

        If ``branch`` already exists on the platform it is checked out as-is so
        an interrupted execution can resume; otherwise it is created from
        ``base_branch``. ``destination`` must not exist yet.
        """
        ...

    def publish_branch(
        self,
        repository: RepositoryRef,
        *,
        working_copy: Path,
        branch: str,
        message: str,
    ) -> str:
        """Commit any pending changes, push ``branch`` and return the head revision."""
        ...

    def upsert_change_request(self, spec: ChangeRequestSpec) -> ChangeRequest:
        """Open a change request for ``spec.source_branch``, or update the open one.

        Idempotent: at most one open change request exists per source branch.
        """
        ...

    def checks(self, change_request: ChangeRequest) -> ChecksSummary: ...
