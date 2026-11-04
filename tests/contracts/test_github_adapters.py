# SPDX-License-Identifier: Apache-2.0
"""Run the TaskProvider contract against the GitHub adapter over a fake GitHub API."""

from __future__ import annotations

from ilmar.adapters.github import GitHubClient, GitHubTaskProvider
from ilmar.domain.tasks import Task
from ilmar.ports.task_provider import TaskProvider

from contracts.task_provider import TaskProviderContract
from fake_github import TOKEN, FakeGitHub, claim_comment

READY = "ilmar:ready"


def make_api() -> FakeGitHub:
    api = FakeGitHub("owner/repo", page_size=2)  # force pagination
    for number in (1, 2, 3):
        api.add_issue(number, labels={READY})
    # Noise that must never be listed or claimable:
    api.add_issue(4, labels={READY}, pull_request=True)
    api.add_issue(5, labels={READY}, state="closed")
    api.add_issue(6)
    api.add_issue(7, labels={READY, "status:blocked"})
    api.add_issue(8, labels={READY}, assignees=["human"])
    return api


def make_provider(api: FakeGitHub, instance: str = "inst-a") -> GitHubTaskProvider:
    return GitHubTaskProvider(
        GitHubClient(TOKEN, transport=api),
        repository="owner/repo",
        instance_id=instance,
        excluded_labels=("status:blocked",),
    )


_conformance: TaskProvider = make_provider(make_api())


class TestGitHubTaskProvider(TaskProviderContract):
    def make_provider(self) -> TaskProvider:
        return make_provider(make_api())

    def claim_as_other(self, provider: TaskProvider, task: Task) -> None:
        assert isinstance(provider, GitHubTaskProvider)
        api = provider._client._transport  # the fake API behind this provider
        assert isinstance(api, FakeGitHub)
        number = int(task.ref.key.rpartition("#")[2])
        api.comment(number, claim_comment("other-instance"))
        api.issues[number].labels.add("ilmar:claimed")
