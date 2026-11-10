# SPDX-License-Identifier: Apache-2.0
"""Run the TaskProvider contract against the GitHub adapter over a fake GitHub API."""

from __future__ import annotations

import json
from collections.abc import Hashable

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
    api.add_issue(9, labels={READY})
    return api


def make_provider(api: FakeGitHub, instance: str = "inst-a") -> GitHubTaskProvider:
    return GitHubTaskProvider(
        GitHubClient(TOKEN, transport=api),
        repository="owner/repo",
        instance_id=instance,
        excluded_labels=("status:blocked",),
        human_claim_labels=("status:in-progress",),
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

    def change(self, provider: TaskProvider, task: Task, what: str) -> None:
        api, number = self._api(provider), int(task.ref.key.rpartition("#")[2])
        issue = api.issues[number]
        if what == "close":
            issue.state = "closed"
        elif what == "unlabel":
            issue.labels.discard(READY)
        elif what == "exclude":
            issue.labels.add("status:blocked")
        elif what == "assign":
            issue.assignees.append("human")
        elif what == "human-label":
            issue.labels.add("status:in-progress")
        elif what == "other-instance":
            api.comment(number, claim_comment("other-instance"), user="ilmar-bot")
        elif what == "forge-own-marker":
            api.comment(number, claim_comment("inst-a", "f" * 32), user="mallory")
        elif what == "gone":
            api.deleted.add(number)
        elif what == "drop-own-claim":
            api.comments[number] = [
                c for c in api.comments[number]
                if not ("instance=inst-a " in c["body"] and c["user"]["login"] == "ilmar-bot")
            ]  # fmt: skip
        elif what == "drop-label":
            issue.labels.discard("ilmar:claimed")
        elif what == "add-label":
            issue.labels.add("ilmar:claimed")
        else:
            raise AssertionError(what)

    def make_unreadable(self, provider: TaskProvider, task: Task) -> None:
        number = int(task.ref.key.rpartition("#")[2])
        self._api(provider).failures[("GET", rf"/repos/owner/repo/issues/{number}")] = 403

    def make_listing_unreadable(self, provider: TaskProvider) -> None:
        self._api(provider).failures[("GET", r"/repos/owner/repo/issues")] = 503

    def snapshot(self, provider: TaskProvider) -> Hashable:
        api = self._api(provider)
        issues = {n: i.as_json(api.repository) for n, i in api.issues.items()}
        return json.dumps([issues, api.comments], sort_keys=True)

    @staticmethod
    def _api(provider: TaskProvider) -> FakeGitHub:
        assert isinstance(provider, GitHubTaskProvider)
        api = provider._client._transport
        assert isinstance(api, FakeGitHub)
        return api
