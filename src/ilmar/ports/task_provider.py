# SPDX-License-Identifier: Apache-2.0
"""Port for issue trackers and other sources of work."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol

from ilmar.domain.tasks import ClaimOutcome, Task, TaskRef


class TaskProvider(Protocol):
    """Discovers tasks and manages their lifecycle on the tracker.

    Contract (see ``tests/contracts/task_provider.py``):

    * ``list_eligible`` returns only tasks this factory may start. A task that
      is claimed by someone else is not eligible.
    * ``claim`` is idempotent for the claimant: claiming a task already held by
      this factory returns ``CLAIMED`` again.
    * ``release`` is idempotent and makes a claimed task eligible again unless
      it became ineligible for another reason.
    * Every ``TaskRef`` this provider returns has ``provider == self.name``.
    """

    @property
    def name(self) -> str: ...

    def list_eligible(self) -> Sequence[Task]: ...

    def get(self, ref: TaskRef) -> Task | None:
        """Return the task, or ``None`` if it no longer exists."""
        ...

    def claim(self, ref: TaskRef) -> ClaimOutcome: ...

    def release(self, ref: TaskRef) -> None: ...

    def post_update(self, ref: TaskRef, message: str) -> None:
        """Leave a human-readable progress note on the task."""
        ...
