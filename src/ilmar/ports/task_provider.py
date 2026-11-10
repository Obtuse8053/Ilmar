# SPDX-License-Identifier: Apache-2.0
"""Port for issue trackers and other sources of work."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol

from ilmar.domain.tasks import (
    Claim,
    HeldClaim,
    ReleaseResult,
    RestoreResult,
    Task,
    TaskRef,
    TaskStanding,
)


class TaskProvider(Protocol):
    """Discovers tasks and manages their lifecycle on the tracker.

    Contract (see ``tests/contracts/task_provider.py``):

    * ``list_eligible`` returns only tasks this factory may start. A task that
      is claimed by someone else is not eligible.
    * ``claim`` is idempotent for the claimant: claiming a task already held by
      this factory returns ``CLAIMED`` again, with the same claim id.
    * ``release`` is idempotent and makes a claimed task eligible again unless
      it became ineligible for another reason.
    * A claim counts as this factory's only when it is authentic: written by
      the provider's own identity and naming this instance (ADR-0026 §6).
    * ``standing`` and ``held_claims`` never write, and every call reads the
      tracker afresh: no response is reused across calls (ADR-0026 §1).
      Uncertainty raises ``ProviderError``; it is never an outcome.
    * ``release_claim`` and ``restore_label`` act on exactly the one claim
      object they are given, re-read by its id first (ADR-0026 §5).
    * Every ``TaskRef`` this provider returns has ``provider == self.name``.
    """

    @property
    def name(self) -> str: ...

    def list_eligible(self) -> Sequence[Task]: ...

    def get(self, ref: TaskRef) -> Task | None:
        """Return the task, or ``None`` if it no longer exists."""
        ...

    def claim(self, ref: TaskRef) -> Claim: ...

    def release(self, ref: TaskRef) -> None: ...

    def post_update(self, ref: TaskRef, message: str) -> None:
        """Leave a human-readable progress note on the task."""
        ...

    def standing(self, ref: TaskRef) -> TaskStanding:
        """Whether the task is still wanted and held by this instance; read-only."""
        ...

    def held_claims(self) -> Sequence[HeldClaim]:
        """Every task on which this instance holds an authentic claim; read-only."""
        ...

    def release_claim(self, claim: HeldClaim) -> ReleaseResult:
        """Delete exactly ``claim``'s object if it is still this instance's
        authentic claim with that token; then drop the claim label only if no
        claim of anyone remains."""
        ...

    def restore_label(self, claim: HeldClaim) -> RestoreResult:
        """Add only the claim label beside exactly ``claim``'s object, if it is
        still this instance's authentic claim with that token."""
        ...
