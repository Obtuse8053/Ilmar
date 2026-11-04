# SPDX-License-Identifier: Apache-2.0
"""Source-control concepts: repositories, change requests and check results.

"Change request" is the neutral term for a GitHub pull request or a GitLab
merge request.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass
from pathlib import PurePosixPath

from ilmar.domain.tasks import TaskRef


@dataclass(frozen=True, slots=True)
class RepositoryRef:
    """Identifies a repository. ``slug`` is opaque to the core."""

    provider: str
    slug: str


@dataclass(frozen=True, slots=True)
class CandidateRevision:
    """The agent's output frozen into an immutable revision (ADR-0010).

    ``revision`` is what gets verified and published; ``base_revision`` is what
    the agent started from. ``changed_paths`` lists every path added, modified
    or deleted relative to the base, as sorted, relative POSIX paths. It is the
    input for classifying protected, check-influencing and CI paths (#9).
    """

    revision: str
    base_revision: str
    changed_paths: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.revision or not self.base_revision:
            raise ValueError("revision and base_revision must be non-empty")
        if list(self.changed_paths) != sorted(set(self.changed_paths)):
            raise ValueError("changed_paths must be sorted and unique")
        for path in self.changed_paths:
            pure = PurePosixPath(path)
            if not path or pure.is_absolute() or ".." in pure.parts or str(pure) != path:
                raise ValueError(f"changed path must be relative and normalised: {path!r}")


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
