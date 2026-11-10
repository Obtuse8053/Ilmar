# SPDX-License-Identifier: Apache-2.0
"""Contract every ``TaskProvider`` adapter must satisfy.

Subclasses implement :meth:`make_provider`, which returns a provider with at
least three eligible tasks, :meth:`claim_as_other`, which makes a task look
claimed by a different worker using provider-specific means, and, for
ADR-0026's standing, :meth:`change` (a human or tracker act on the task),
:meth:`make_unreadable` and :meth:`snapshot` (the tracker's whole state, to
prove that reads write nothing).
"""

from __future__ import annotations

from collections.abc import Hashable

import pytest

from ilmar.domain.errors import ProviderError
from ilmar.domain.tasks import (
    ClaimOutcome,
    ReleaseResult,
    RestoreResult,
    Standing,
    Task,
    TaskStanding,
)
from ilmar.ports.task_provider import TaskProvider

CHANGES = {
    # what happens to the task -> the standing it must read as (ADR-0026 §1)
    "close": (Standing.INELIGIBLE, "task-closed"),
    "unlabel": (Standing.INELIGIBLE, "task-unlabelled"),
    "exclude": (Standing.INELIGIBLE, "task-excluded"),
    "assign": (Standing.CLAIMED_BY_OTHER, "task-claimed-by-human"),
    "human-label": (Standing.CLAIMED_BY_OTHER, "task-claimed-by-human"),
    "other-instance": (Standing.CLAIMED_BY_OTHER, "task-claimed-by-other-instance"),
    "forge-own-marker": (Standing.CLAIMED_BY_OTHER, "task-claimed-by-other-instance"),
    "gone": (Standing.GONE, "task-gone"),
    "drop-own-claim": (Standing.CLAIM_LOST, "task-claim-lost"),
}
PRECEDENCE = [
    # several at once -> the most decisive reason (gone is applied last)
    (("close", "gone"), "task-gone"),
    (("other-instance", "close"), "task-closed"),
    (("unlabel", "other-instance"), "task-claimed-by-other-instance"),
    (("other-instance", "assign"), "task-claimed-by-human"),
    (("drop-own-claim", "exclude"), "task-excluded"),
    (("drop-own-claim", "unlabel", "exclude"), "task-unlabelled"),
]


class TaskProviderContract:
    def make_provider(self) -> TaskProvider:
        raise NotImplementedError

    def claim_as_other(self, provider: TaskProvider, task: Task) -> None:
        raise NotImplementedError

    def change(self, provider: TaskProvider, task: Task, what: str) -> None:
        """Apply one of :data:`CHANGES` to ``task``, as a human or the tracker
        would; or ``drop-label`` / ``add-label``: only the claim label changes."""
        raise NotImplementedError

    def make_unreadable(self, provider: TaskProvider, task: Task) -> None:
        """Make reading ``task`` fail as a 403, a 5xx or a rate limit would."""
        raise NotImplementedError

    def snapshot(self, provider: TaskProvider) -> Hashable:
        raise NotImplementedError

    @pytest.fixture
    def provider(self) -> TaskProvider:
        return self.make_provider()

    def _first(self, provider: TaskProvider) -> Task:
        eligible = provider.list_eligible()
        assert len(eligible) >= 2, "make_provider must seed at least two eligible tasks"
        return eligible[0]

    def test_refs_belong_to_this_provider(self, provider: TaskProvider) -> None:
        assert all(t.ref.provider == provider.name for t in provider.list_eligible())

    def test_get_returns_listed_task(self, provider: TaskProvider) -> None:
        task = self._first(provider)
        assert provider.get(task.ref) == task

    def test_claim_is_idempotent_and_removes_task_from_eligible(
        self, provider: TaskProvider
    ) -> None:
        task = self._first(provider)
        assert provider.claim(task.ref).outcome is ClaimOutcome.CLAIMED
        assert provider.claim(task.ref).outcome is ClaimOutcome.CLAIMED
        assert task.ref not in {t.ref for t in provider.list_eligible()}

    def test_release_makes_task_eligible_again(self, provider: TaskProvider) -> None:
        task = self._first(provider)
        provider.claim(task.ref)
        provider.release(task.ref)
        provider.release(task.ref)
        assert task.ref in {t.ref for t in provider.list_eligible()}

    def test_task_claimed_by_other_cannot_be_claimed(self, provider: TaskProvider) -> None:
        task = self._first(provider)
        self.claim_as_other(provider, task)
        assert task.ref not in {t.ref for t in provider.list_eligible()}
        assert provider.claim(task.ref).outcome is ClaimOutcome.CLAIMED_BY_OTHER

    def test_release_does_not_steal_another_workers_claim(self, provider: TaskProvider) -> None:
        task = self._first(provider)
        self.claim_as_other(provider, task)
        provider.release(task.ref)
        assert provider.claim(task.ref).outcome is ClaimOutcome.CLAIMED_BY_OTHER

    def test_post_update_is_accepted(self, provider: TaskProvider) -> None:
        task = self._first(provider)
        provider.post_update(task.ref, "Ilmar started working on this task.")

    # -- standing and claims (ADR-0026) ---------------------------------------------

    def _claimed(self, provider: TaskProvider) -> tuple[Task, str]:
        task = self._first(provider)
        claim = provider.claim(task.ref)
        assert claim.outcome is ClaimOutcome.CLAIMED
        assert claim.claim_id is not None
        return task, claim.claim_id

    def test_claim_returns_the_same_id_while_held(self, provider: TaskProvider) -> None:
        task, claim_id = self._claimed(provider)
        assert provider.claim(task.ref).claim_id == claim_id

    def test_a_claimed_task_is_held_with_its_claim(self, provider: TaskProvider) -> None:
        task, claim_id = self._claimed(provider)
        standing = provider.standing(task.ref)
        assert standing.standing is Standing.HELD
        assert standing.claim is not None
        assert (standing.claim.ref, standing.claim.claim_id) == (task.ref, claim_id)
        assert standing.label_missing is False

    @pytest.mark.parametrize("what", sorted(CHANGES))
    def test_each_change_reads_as_its_standing(self, provider: TaskProvider, what: str) -> None:
        task, _ = self._claimed(provider)
        self.change(provider, task, what)
        assert provider.standing(task.ref) == TaskStanding(*CHANGES[what])

    @pytest.mark.parametrize(("changes", "reason"), PRECEDENCE, ids=lambda v: str(v))
    def test_the_most_decisive_reason_is_reported(
        self, provider: TaskProvider, changes: tuple[str, ...], reason: str
    ) -> None:
        task, _ = self._claimed(provider)
        for what in changes:
            self.change(provider, task, what)
        assert provider.standing(task.ref).reason == reason

    def test_every_standing_read_is_fresh(self, provider: TaskProvider) -> None:
        task, _ = self._claimed(provider)
        assert provider.standing(task.ref).standing is Standing.HELD
        self.change(provider, task, "drop-own-claim")
        assert provider.standing(task.ref).standing is Standing.CLAIM_LOST
        self.change(provider, task, "close")
        assert provider.standing(task.ref).reason == "task-closed"

    def test_an_uncertain_read_raises_and_is_never_an_outcome(self, provider: TaskProvider) -> None:
        task, _ = self._claimed(provider)
        self.make_unreadable(provider, task)
        with pytest.raises(ProviderError):
            provider.standing(task.ref)

    def test_held_claims_lists_exactly_the_authentic_own_claims(
        self, provider: TaskProvider
    ) -> None:
        mine, other, forged = provider.list_eligible()[:3]
        claim = provider.claim(mine.ref)
        self.claim_as_other(provider, other)
        self.change(provider, forged, "forge-own-marker")
        self.change(provider, forged, "add-label")
        held = provider.held_claims()
        assert [(h.ref, h.claim_id) for h in held] == [(mine.ref, claim.claim_id)]

    def test_standing_and_held_claims_write_nothing(self, provider: TaskProvider) -> None:
        task, _ = self._claimed(provider)
        before = self.snapshot(provider)
        provider.standing(task.ref)
        provider.held_claims()
        assert self.snapshot(provider) == before

    def test_a_targeted_release_removes_exactly_that_claim(self, provider: TaskProvider) -> None:
        task, _ = self._claimed(provider)
        (held,) = provider.held_claims()
        assert provider.release_claim(held) is ReleaseResult.RELEASED
        assert provider.standing(task.ref).standing is Standing.CLAIM_LOST
        assert task.ref in {t.ref for t in provider.list_eligible()}  # label dropped too
        assert provider.release_claim(held) is ReleaseResult.ABSENT

    def test_a_targeted_release_never_touches_a_newer_claim(self, provider: TaskProvider) -> None:
        task, _ = self._claimed(provider)
        (old,) = provider.held_claims()
        provider.release(task.ref)
        newer = provider.claim(task.ref)
        assert provider.release_claim(old) is ReleaseResult.ABSENT
        standing = provider.standing(task.ref)
        assert standing.standing is Standing.HELD
        assert standing.claim is not None
        assert standing.claim.claim_id == newer.claim_id
        assert standing.label_missing is False

    def test_a_targeted_release_never_touches_another_partys_claim(
        self, provider: TaskProvider
    ) -> None:
        task, _ = self._claimed(provider)
        (held,) = provider.held_claims()
        self.claim_as_other(provider, task)
        assert provider.release_claim(held) is ReleaseResult.RELEASED
        assert provider.standing(task.ref).reason == "task-claimed-by-other-instance"
        assert task.ref not in {t.ref for t in provider.list_eligible()}  # label stays

    def test_a_missing_label_is_restored_beside_the_claim(self, provider: TaskProvider) -> None:
        task, _ = self._claimed(provider)
        self.change(provider, task, "drop-label")
        standing = provider.standing(task.ref)
        assert standing.standing is Standing.HELD
        assert standing.label_missing
        assert standing.claim is not None
        assert provider.restore_label(standing.claim) is RestoreResult.RESTORED
        assert provider.standing(task.ref).label_missing is False

    def test_restoring_beside_a_vanished_claim_never_claims(self, provider: TaskProvider) -> None:
        task, _ = self._claimed(provider)
        self.change(provider, task, "drop-label")
        held = provider.standing(task.ref).claim
        assert held is not None
        self.change(provider, task, "drop-own-claim")
        assert provider.restore_label(held) is RestoreResult.ABSENT
        assert provider.standing(task.ref).standing is Standing.CLAIM_LOST
        assert provider.held_claims() == ()

    @pytest.mark.parametrize("remaining", [None, "other-instance", "human-label"])
    def test_a_release_after_a_lost_claim_removes_only_ilmars_leftover_label(
        self, provider: TaskProvider, remaining: str | None
    ) -> None:
        """Review of #83: a person deleted Ilmar's claim; the ending's release still
        removes Ilmar's own label, unless a claim of anyone remains (ADR-0026 §4, §5)."""
        task, _ = self._claimed(provider)
        self.change(provider, task, "drop-own-claim")
        if remaining is not None:
            self.change(provider, task, remaining)
        before = provider.standing(task.ref)
        provider.release(task.ref)
        eligible = task.ref in {t.ref for t in provider.list_eligible()}
        assert eligible is (remaining is None)
        assert provider.standing(task.ref) == before  # nobody else's claim was touched
        provider.release(task.ref)  # idempotent
