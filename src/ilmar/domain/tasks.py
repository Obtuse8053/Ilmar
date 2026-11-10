# SPDX-License-Identifier: Apache-2.0
"""Provider-neutral representation of units of work."""

from __future__ import annotations

import enum
import re
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


# -- tracker reconciliation (ADR-0026) ----------------------------------------------------

_OPAQUE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")


def _require_opaque(value: str, name: str) -> None:
    if not isinstance(value, str) or not _OPAQUE.match(value):
        raise ValueError(f"{name} must be a short opaque identifier")


@dataclass(frozen=True, slots=True)
class Claim:
    """What ``TaskProvider.claim`` did: the outcome and, when ``CLAIMED``, the id
    of the claim this instance holds (ADR-0026 §6)."""

    outcome: ClaimOutcome
    claim_id: str | None = None

    def __post_init__(self) -> None:
        if self.outcome is ClaimOutcome.CLAIMED:
            _require_opaque(self.claim_id or "", "claim_id")
        elif self.claim_id is not None:
            raise ValueError("only a CLAIMED outcome carries a claim id")


@dataclass(frozen=True, slots=True)
class HeldClaim:
    """One authentic claim of this instance (ADR-0026 §5): the task, the claim
    token and the tracker's immutable id of the object that holds the claim.
    Both values are opaque to the core."""

    ref: TaskRef
    claim_id: str
    object_id: str

    def __post_init__(self) -> None:
        _require_opaque(self.claim_id, "claim_id")
        _require_opaque(self.object_id, "object_id")


class Standing(enum.Enum):
    """Whether a task is still wanted and held by this instance (ADR-0026 §1)."""

    HELD = "held"
    CLAIM_LOST = "claim_lost"
    CLAIMED_BY_OTHER = "claimed_by_other"
    INELIGIBLE = "ineligible"
    GONE = "gone"


STANDING_REASONS: dict[Standing, frozenset[str]] = {
    Standing.HELD: frozenset(),
    Standing.CLAIM_LOST: frozenset({"task-claim-lost"}),
    Standing.CLAIMED_BY_OTHER: frozenset(
        {"task-claimed-by-human", "task-claimed-by-other-instance"}
    ),
    Standing.INELIGIBLE: frozenset({"task-closed", "task-unlabelled", "task-excluded"}),
    Standing.GONE: frozenset({"task-gone"}),
}
"""The reason codes each standing may carry; registered in ADR-0024's registry."""

REASON_PRECEDENCE: tuple[str, ...] = (
    "task-gone",
    "task-closed",
    "task-claimed-by-human",
    "task-claimed-by-other-instance",
    "task-unlabelled",
    "task-excluded",
    "task-claim-lost",
)
"""The most decisive reason first, when several apply (ADR-0026 §1)."""


@dataclass(frozen=True, slots=True)
class TaskStanding:
    """A fresh read of a task's standing (ADR-0026 §1)."""

    standing: Standing
    reason: str | None = None
    claim: HeldClaim | None = None
    label_missing: bool = False

    def __post_init__(self) -> None:
        if self.standing is Standing.HELD:
            if self.claim is None or self.reason is not None:
                raise ValueError("HELD carries the held claim and no reason")
            return
        if self.reason not in STANDING_REASONS[self.standing]:
            raise ValueError(f"{self.reason!r} is not a reason for {self.standing.value}")
        if self.claim is not None or self.label_missing:
            raise ValueError("only HELD carries a claim or label_missing")

    @staticmethod
    def decisive(reasons: set[str]) -> TaskStanding:
        """The standing for the most decisive of ``reasons`` (ADR-0026 §1)."""
        for reason in REASON_PRECEDENCE:
            if reason in reasons:
                standing = next(s for s, codes in STANDING_REASONS.items() if reason in codes)
                return TaskStanding(standing, reason)
        raise ValueError("no reason given")


class ReleaseResult(enum.Enum):
    """What a targeted release of one claim object did (ADR-0026 §5)."""

    RELEASED = "released"
    ABSENT = "absent"
    CHANGED = "changed"


class RestoreResult(enum.Enum):
    """What a targeted label restore beside one claim object did (ADR-0026 §5)."""

    RESTORED = "restored"
    ABSENT = "absent"
    CHANGED = "changed"
