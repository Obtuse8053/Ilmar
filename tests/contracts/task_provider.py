# SPDX-License-Identifier: Apache-2.0
"""Contract every ``TaskProvider`` adapter must satisfy.

Subclasses implement :meth:`make_provider`, which returns a provider with at
least two eligible tasks, and :meth:`claim_as_other`, which makes a task look
claimed by a different worker using provider-specific means.
"""

from __future__ import annotations

import pytest

from ilmar.domain.tasks import ClaimOutcome, Task
from ilmar.ports.task_provider import TaskProvider


class TaskProviderContract:
    def make_provider(self) -> TaskProvider:
        raise NotImplementedError

    def claim_as_other(self, provider: TaskProvider, task: Task) -> None:
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
        assert provider.claim(task.ref) is ClaimOutcome.CLAIMED
        assert provider.claim(task.ref) is ClaimOutcome.CLAIMED
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
        assert provider.claim(task.ref) is ClaimOutcome.CLAIMED_BY_OTHER

    def test_release_does_not_steal_another_workers_claim(self, provider: TaskProvider) -> None:
        task = self._first(provider)
        self.claim_as_other(provider, task)
        provider.release(task.ref)
        assert provider.claim(task.ref) is ClaimOutcome.CLAIMED_BY_OTHER

    def test_post_update_is_accepted(self, provider: TaskProvider) -> None:
        task = self._first(provider)
        provider.post_update(task.ref, "Ilmar started working on this task.")
