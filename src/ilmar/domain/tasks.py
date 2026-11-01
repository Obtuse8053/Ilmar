# SPDX-License-Identifier: Apache-2.0
"""Provider-neutral representation of units of work."""

from __future__ import annotations

import enum
from dataclasses import dataclass, field


def _require_text(value: str, name: str) -> None:
    if not value or value != value.strip():
        raise ValueError(f"{name} must be non-empty and have no surrounding whitespace")


@dataclass(frozen=True, slots=True)
class TaskRef:
    """Identifies a task without exposing provider-specific structure.

    ``provider`` names the configured task provider (for example ``"github"``).
    ``key`` is opaque to the core: only the owning adapter may interpret it.
    """

    provider: str
    key: str

    def __post_init__(self) -> None:
        _require_text(self.provider, "provider")
        _require_text(self.key, "key")

    def __str__(self) -> str:
        return f"{self.provider}:{self.key}"


@dataclass(frozen=True, slots=True)
class Task:
    """A unit of work discovered from a task provider."""

    ref: TaskRef
    title: str
    description: str
    labels: frozenset[str] = field(default_factory=frozenset)
    url: str | None = None


class ClaimOutcome(enum.Enum):
    """Result of asking a task provider to reserve a task for this factory."""

    CLAIMED = "claimed"
    """The task is now reserved for us. Also returned if we already held it."""

    CLAIMED_BY_OTHER = "claimed_by_other"
    """Another worker or person holds the task."""

    NOT_ELIGIBLE = "not_eligible"
    """The task exists but no longer qualifies (closed, label removed, ...)."""
