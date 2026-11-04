# SPDX-License-Identifier: Apache-2.0
"""GitHub task state model details beyond the shared contract (ADR-0013)."""

from __future__ import annotations

import re
import threading

import pytest

from ilmar.adapters.github import GitHubClient, GitHubTaskProvider
from ilmar.adapters.github.client import HttpRequest
from ilmar.domain.errors import ProviderError
from ilmar.domain.tasks import ClaimOutcome, TaskRef

from fake_github import TOKEN, FakeGitHub, claim_comment

READY, CLAIMED = "ilmar:ready", "ilmar:claimed"


def provider(api: FakeGitHub, instance: str = "a", **kwargs: object) -> GitHubTaskProvider:
    return GitHubTaskProvider(
        GitHubClient(TOKEN, transport=api),
        repository="owner/repo",
        instance_id=instance,
        **kwargs,  # type: ignore[arg-type]
    )


def ref(number: int) -> TaskRef:
    return TaskRef("github", f"owner/repo#{number}")


def claims(api: FakeGitHub, number: int) -> list[str]:
    return [c["body"] for c in api.comments[number] if "ilmar:claim" in c["body"]]


@pytest.fixture
def api() -> FakeGitHub:
    api = FakeGitHub()
    api.add_issue(1, "Fix the bug", body="Steps...", labels={READY, "bug"})
    return api


def test_task_mapping(api: FakeGitHub) -> None:
    (task,) = provider(api).list_eligible()
    assert task.ref == ref(1)
    assert (task.title, task.description) == ("Fix the bug", "Steps...")
    assert task.labels == {READY, "bug"}
    assert task.url == "https://github.com/owner/repo/issues/1"


@pytest.mark.parametrize(
    "issue_kwargs",
    [
        {"labels": {READY}, "pull_request": True},
        {"labels": {READY}, "state": "closed"},
        {"labels": set()},
        {"labels": {READY, "needs:maintainer"}},
        {"labels": {READY}, "assignees": ["human"]},
        {"labels": {READY, CLAIMED}},
    ],
    ids=["pull-request", "closed", "unlabelled", "excluded", "assigned", "claimed"],
)
def test_ineligible_issues_are_not_listed(issue_kwargs: dict[str, object]) -> None:
    api = FakeGitHub()
    api.add_issue(2, **issue_kwargs)  # type: ignore[arg-type]
    assert provider(api, excluded_labels=("needs:maintainer",)).list_eligible() == ()


def test_assigned_issues_can_be_allowed() -> None:
    api = FakeGitHub()
    api.add_issue(2, labels={READY}, assignees=["human"])
    assert len(provider(api, skip_assigned=False).list_eligible()) == 1


def test_get_handles_foreign_refs_missing_issues_and_pull_requests(api: FakeGitHub) -> None:
    api.add_issue(3, labels={READY}, pull_request=True)
    p = provider(api)
    assert p.get(TaskRef("github", "other/repo#1")) is None
    assert p.get(TaskRef("gitlab", "owner/repo#1")) is None
    assert p.get(TaskRef("github", "owner/repo#x")) is None
    assert p.get(ref(99)) is None
    assert p.get(ref(3)) is None
    assert p.get(ref(1)) is not None


def test_claim_posts_one_marker_and_label_and_is_idempotent(api: FakeGitHub) -> None:
    p = provider(api)
    assert p.claim(ref(1)) is ClaimOutcome.CLAIMED
    assert p.claim(ref(1)) is ClaimOutcome.CLAIMED
    assert len(claims(api, 1)) == 1
    assert CLAIMED in api.issues[1].labels


def test_human_claim_via_label_is_respected(api: FakeGitHub) -> None:
    """A session following docs/workflow.md marks work in progress with a label only."""
    api.issues[1].labels.add("status:in-progress")
    p = provider(api, claimed_label="status:in-progress")
    assert p.list_eligible() == ()
    assert p.claim(ref(1)) is ClaimOutcome.CLAIMED_BY_OTHER
    p.release(ref(1))
    assert "status:in-progress" in api.issues[1].labels


@pytest.mark.parametrize(
    ("change", "expected"),
    [
        ({"state": "closed"}, ClaimOutcome.NOT_ELIGIBLE),
        ({"labels": {"bug"}}, ClaimOutcome.NOT_ELIGIBLE),
        ({"assignees": ["human"]}, ClaimOutcome.CLAIMED_BY_OTHER),
    ],
)
def test_claim_outcomes(api: FakeGitHub, change: dict[str, object], expected: ClaimOutcome) -> None:
    for key, value in change.items():
        setattr(api.issues[1], key, value)
    assert provider(api).claim(ref(1)) is expected
    assert claims(api, 1) == []


def test_claim_of_unknown_or_foreign_task_is_not_eligible(api: FakeGitHub) -> None:
    assert provider(api).claim(ref(42)) is ClaimOutcome.NOT_ELIGIBLE
    assert provider(api).claim(TaskRef("github", "other/repo#1")) is ClaimOutcome.NOT_ELIGIBLE


def test_claim_marker_carries_a_unique_token(api: FakeGitHub) -> None:
    api.add_issue(2, labels={READY})
    p = provider(api)
    p.claim(ref(1))
    p.claim(ref(2))
    tokens = [re.search(r"token=([0-9a-f]+)", body).group(1) for body in iter_claims(api)]  # type: ignore[union-attr]
    assert len(tokens) == 2
    assert len(set(tokens)) == 2
    assert all(len(token) == 32 for token in tokens)


def test_claim_appearing_mid_claim_makes_us_back_off(api: FakeGitHub) -> None:
    """Another instance's claim shows up between our check and our re-read."""
    a = provider(api, "a")

    def b_claims_during_a_post(request: HttpRequest) -> None:
        if request.method == "POST" and request.url.endswith("/comments"):
            api.before.remove(b_claims_during_a_post)
            api.comment(1, claim_comment("b"))

    api.before.append(b_claims_during_a_post)
    assert a.claim(ref(1)) is ClaimOutcome.CLAIMED_BY_OTHER
    assert claims(api, 1) == [claim_comment("b")]  # a withdrew; b's claim untouched
    assert CLAIMED in api.issues[1].labels  # still visibly taken (by b)


def test_overlapping_claims_on_a_controlled_schedule_never_both_succeed(api: FakeGitHub) -> None:
    """Both instances pass the initial check before either posts (ADR-0013).

    Request order is forced: a GET issue, a GET comments, b GET issue, b GET
    comments, a POST comment, b POST comment, a POST label, b POST label, then
    both re-read. Both see each other, both back off, and the label is cleaned up.
    """
    order = ["a", "a", "b", "b", "a", "b", "a", "b", "a", "b"]
    schedule = Schedule(order)
    api.before.append(schedule.gate)
    results: dict[str, ClaimOutcome] = {}

    def run(instance: str) -> None:
        results[instance] = provider(api, instance).claim(ref(1))

    threads = [threading.Thread(target=run, args=(i,), name=i) for i in ("a", "b")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)
    assert not any(t.is_alive() for t in threads), "schedule deadlocked"
    assert schedule.followed, schedule.log
    issue, comments = "GET owner/repo/issues/1", "GET owner/repo/issues/1/comments?per_page=100"
    post, label = "POST owner/repo/issues/1/comments", "POST owner/repo/issues/1/labels"
    assert schedule.log == [
        f"a {issue}",
        f"a {comments}",
        f"b {issue}",
        f"b {comments}",
        f"a {post}",
        f"b {post}",
        f"a {label}",
        f"b {label}",
        f"a {comments}",
        f"b {comments}",
    ]  # both passed the initial check before either posted
    assert ClaimOutcome.CLAIMED not in results.values()
    assert results == {"a": ClaimOutcome.CLAIMED_BY_OTHER, "b": ClaimOutcome.CLAIMED_BY_OTHER}
    assert claims(api, 1) == []
    assert CLAIMED not in api.issues[1].labels
    assert len(provider(api, "a").list_eligible()) == 1  # nothing is stuck


def test_sequential_second_instance_sees_the_first(api: FakeGitHub) -> None:
    a, b = provider(api, "a"), provider(api, "b")
    assert a.claim(ref(1)) is ClaimOutcome.CLAIMED
    assert b.claim(ref(1)) is ClaimOutcome.CLAIMED_BY_OTHER
    assert len(claims(api, 1)) == 1


def test_ownership_is_per_instance_across_restarts(api: FakeGitHub) -> None:
    """A restarted coordinator of the same instance recognises its claim; whether
    an execution still backs it is decided by the durable store (#12, #15)."""
    provider(api, "a").claim(ref(1))
    restarted = provider(api, "a")
    assert restarted.claim(ref(1)) is ClaimOutcome.CLAIMED
    assert len(claims(api, 1)) == 1
    restarted.release(ref(1))
    assert claims(api, 1) == []


def test_release_removes_only_our_claim(api: FakeGitHub) -> None:
    a = provider(api, "a")
    a.claim(ref(1))
    api.comment(1, claim_comment("b"))  # a stale claim from another instance
    a.release(ref(1))
    assert claims(api, 1) == [claim_comment("b")]
    assert CLAIMED in api.issues[1].labels  # still held by b
    a.release(ref(1))  # idempotent


def test_release_by_the_sole_holder_clears_the_label(api: FakeGitHub) -> None:
    a = provider(api, "a")
    a.claim(ref(1))
    a.release(ref(1))
    assert claims(api, 1) == []
    assert CLAIMED not in api.issues[1].labels
    assert len(a.list_eligible()) == 1


def test_post_update(api: FakeGitHub) -> None:
    p = provider(api)
    p.post_update(ref(1), "Started.")
    assert api.comments[1][-1]["body"] == "Started."
    with pytest.raises(ProviderError):
        p.post_update(ref(42), "nope")
    with pytest.raises(ProviderError):
        p.post_update(TaskRef("github", "other/repo#1"), "nope")


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"repository": "not-a-repo"}, "owner/name"),
        ({"instance_id": "../x"}, "instance id"),
        ({"claimed_label": READY}, "differ"),
    ],
)
def test_configuration_is_validated(kwargs: dict[str, str], message: str) -> None:
    settings = {"repository": "owner/repo", "instance_id": "a"} | kwargs
    with pytest.raises(ValueError, match=message):
        GitHubTaskProvider(GitHubClient(TOKEN, transport=FakeGitHub()), **settings)  # type: ignore[arg-type]


def iter_claims(api: FakeGitHub) -> list[str]:
    return [
        c["body"]
        for comments in api.comments.values()
        for c in comments
        if "ilmar:claim" in c["body"]
    ]


class Schedule:
    """Forces the order in which actor threads (named by instance) may send requests."""

    def __init__(self, order: list[str]) -> None:
        self._order = list(order)
        self._condition = threading.Condition()
        self.log: list[str] = []
        self.followed = True

    def gate(self, request: HttpRequest) -> None:
        actor = threading.current_thread().name
        with self._condition:
            if not self._order:
                return
            if not self._condition.wait_for(
                lambda: not self._order or self._order[0] == actor, timeout=5
            ):
                self.followed = False
                return
            if self._order:
                self._order.pop(0)
                self.log.append(f"{actor} {request.method} {request.url.split('/repos/')[1]}")
            self._condition.notify_all()
