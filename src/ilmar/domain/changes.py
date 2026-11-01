# SPDX-License-Identifier: Apache-2.0
"""Source-control concepts: repositories, change requests and check results.

"Change request" is the neutral term for a GitHub pull request or a GitLab
merge request.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass

from ilmar.domain.tasks import TaskRef


@dataclass(frozen=True, slots=True)
class RepositoryRef:
    """Identifies a repository. ``slug`` is opaque to the core."""

    provider: str
    slug: str


@dataclass(frozen=True, slots=True)
class ChangeRequestSpec:
    """What the factory wants a change request to look like."""

    repository: RepositoryRef
    source_branch: str
    target_branch: str
    title: str
    description: str
    task: TaskRef


@dataclass(frozen=True, slots=True)
class ChangeRequest:
    """A change request as it exists on the source-control platform."""

    repository: RepositoryRef
    key: str
    url: str
    source_branch: str
    target_branch: str


class CheckState(enum.Enum):
    """Aggregated state of the automated checks on a change request."""

    PENDING = "pending"
    PASSED = "passed"
    FAILED = "failed"
    NONE = "none"
    """No checks are configured or reported."""


@dataclass(frozen=True, slots=True)
class CheckResult:
    name: str
    state: CheckState
    url: str | None = None


@dataclass(frozen=True, slots=True)
class ChecksSummary:
    state: CheckState
    checks: tuple[CheckResult, ...] = ()
