# SPDX-License-Identifier: Apache-2.0
"""The GitHub adapter's side of tracker reconciliation (ADR-0026 §1, §5, §6).

Run against the in-memory REST fake: comment authors and forged markers,
identity from ``GET /user`` or ``bot_login``, deleted and transferred issues,
403, rate limits and 5xx, pagination, and an adversary acting between every
request of a targeted write.
"""

from __future__ import annotations

import re
import urllib.parse
from collections.abc import Callable

import pytest

from ilmar.adapters.github import GitHubClient, GitHubTaskProvider
from ilmar.adapters.github.client import HttpRequest
from ilmar.domain.errors import ProviderError
from ilmar.domain.tasks import (
    ClaimOutcome,
    ClearResult,
    HeldClaim,
    ReleaseResult,
    RestoreResult,
    Standing,
    TaskRef,
    TaskStanding,
)

from fake_github import TOKEN, FakeGitHub, claim_comment

READY, CLAIMED, HUMAN = "ilmar:ready", "ilmar:claimed", "status:in-progress"
ME = "ilmar-bot"


def provider(api: FakeGitHub, instance: str = "a", **kwargs: object) -> GitHubTaskProvider:
    return GitHubTaskProvider(
        GitHubClient(TOKEN, transport=api),
        repository="owner/repo",
        instance_id=instance,
        human_claim_labels=(HUMAN,),
        **kwargs,  # type: ignore[arg-type]
    )


def ref(number: int) -> TaskRef:
    return TaskRef("github", f"owner/repo#{number}")


@pytest.fixture
def api() -> FakeGitHub:
    api = FakeGitHub()
    api.add_issue(1, labels={READY})
    return api


def held(api: FakeGitHub, number: int = 1) -> HeldClaim:
    """Claim issue ``number`` as instance ``a`` and return its held claim."""
    p = provider(api)
    assert p.claim(ref(number)).outcome is ClaimOutcome.CLAIMED
    standing = p.standing(ref(number))
    assert standing.claim is not None
    return standing.claim


def bodies(api: FakeGitHub, number: int = 1) -> list[tuple[int, str, str]]:
    return [(c["id"], c["user"]["login"], c["body"]) for c in api.comments[number]]


def writes(api: FakeGitHub) -> list[str]:
    return [
        f"{r.method} {urllib.parse.urlsplit(r.url).path}" for r in api.requests if r.method != "GET"
    ]


# -- identity (ADR-0026 §6) ---------------------------------------------------------------


def test_identity_comes_from_the_token(api: FakeGitHub) -> None:
    claim = held(api)
    assert claim.claim_id
    assert sum(urllib.parse.urlsplit(r.url).path == "/user" for r in api.requests) == 1


def test_identity_falls_back_to_bot_login_for_app_tokens(api: FakeGitHub) -> None:
    api.user_login = None  # GET /user: 403 for an installation token
    api.poster = "my-app[bot]"
    p = provider(api, bot_login="my-app[bot]")
    assert p.claim(ref(1)).outcome is ClaimOutcome.CLAIMED
    assert p.standing(ref(1)).standing is Standing.HELD


def test_an_unknown_identity_fails_closed(api: FakeGitHub) -> None:
    held(api)
    api.user_login = None
    unknown = provider(api)
    before = bodies(api)
    for read in (lambda: unknown.standing(ref(1)), unknown.held_claims,
                 lambda: unknown.claim(ref(1)), lambda: unknown.release(ref(1))):  # fmt: skip
        with pytest.raises(ProviderError, match="bot_login"):
            read()
    assert bodies(api) == before


def test_a_configured_login_that_is_not_the_tokens_fails_closed(api: FakeGitHub) -> None:
    with pytest.raises(ProviderError, match="does not match"):
        provider(api, bot_login="someone-else").standing(ref(1))


def test_bot_login_must_look_like_a_login() -> None:
    with pytest.raises(ValueError, match="bot_login"):
        provider(FakeGitHub(), bot_login="not a login")


# -- forged markers -----------------------------------------------------------------------


def test_a_forged_own_marker_is_another_partys_claim(api: FakeGitHub) -> None:
    forged = api.comment(1, claim_comment("a", "f" * 32), user="mallory")
    api.issues[1].labels.add(CLAIMED)
    p = provider(api)
    assert p.claim(ref(1)).outcome is ClaimOutcome.CLAIMED_BY_OTHER
    assert p.standing(ref(1)) == TaskStanding(
        Standing.CLAIMED_BY_OTHER, "task-claimed-by-other-instance"
    )
    assert p.held_claims() == ()
    p.release(ref(1))
    assert [c[0] for c in bodies(api)] == [forged]  # never deleted
    assert CLAIMED in api.issues[1].labels
    forged_claim = HeldClaim(ref(1), "f" * 32, str(forged))
    assert p.release_claim(forged_claim) is ReleaseResult.CHANGED
    assert p.restore_label(forged_claim) is RestoreResult.CHANGED
    assert [c[0] for c in bodies(api)] == [forged]


def test_a_forged_copy_beside_our_claim_is_left_alone(api: FakeGitHub) -> None:
    claim = held(api)
    forged = api.comment(1, claim_comment("a", claim.claim_id), user="mallory")
    p = provider(api)
    assert p.standing(ref(1)).reason == "task-claimed-by-other-instance"
    assert p.held_claims() == (claim,)
    assert p.release_claim(claim) is ReleaseResult.RELEASED
    assert [c[0] for c in bodies(api)] == [forged]
    assert CLAIMED in api.issues[1].labels  # a claim of someone remains


# -- GONE needs a readable repository; a transfer is never followed ---------------------------


@pytest.mark.parametrize("how", ["deleted", "missing", "transferred"])
def test_gone_is_confirmed_by_a_readable_repository(api: FakeGitHub, how: str) -> None:
    held(api)
    if how == "deleted":
        api.deleted.add(1)
    elif how == "missing":
        del api.issues[1]
    else:
        api.transferred[1] = "elsewhere/repo"  # the fake fails if it is followed
    p = provider(api)
    assert p.standing(ref(1)) == TaskStanding(Standing.GONE, "task-gone")
    assert p.get(ref(1)) is None
    assert p.claim(ref(1)).outcome is ClaimOutcome.NOT_ELIGIBLE


@pytest.mark.parametrize("how", ["deleted", "transferred"])
def test_gone_is_never_reported_without_the_repository(api: FakeGitHub, how: str) -> None:
    held(api)
    api.repository_readable = False
    if how == "deleted":
        api.deleted.add(1)
    else:
        api.transferred[1] = "elsewhere/repo"
    with pytest.raises(ProviderError, match="repository"):
        provider(api).standing(ref(1))


def test_an_issue_answered_from_another_repository_is_gone(api: FakeGitHub) -> None:
    held(api)
    original = api.issues[1].as_json

    def moved(repository: str) -> dict[str, object]:
        return original("elsewhere/repo")

    api.issues[1].as_json = moved  # type: ignore[method-assign]
    assert provider(api).standing(ref(1)).standing is Standing.GONE


@pytest.mark.parametrize("status", [403, 429, 500, 502])
@pytest.mark.parametrize("path", [r"/repos/owner/repo/issues/1",
                                  r"/repos/owner/repo/issues/1/comments"])  # fmt: skip
def test_uncertainty_is_an_error_never_a_standing(api: FakeGitHub, status: int, path: str) -> None:
    held(api)
    api.failures[("GET", path)] = status
    with pytest.raises(ProviderError):
        provider(api).standing(ref(1))


def test_a_403_on_the_repository_is_never_gone(api: FakeGitHub) -> None:
    held(api)
    api.deleted.add(1)
    api.failures[("GET", r"/repos/owner/repo")] = 403
    with pytest.raises(ProviderError):
        provider(api).standing(ref(1))


# -- freshness and pagination -------------------------------------------------------------


def test_each_standing_call_reads_the_tracker_itself(api: FakeGitHub) -> None:
    held(api)
    p = provider(api)
    p.standing(ref(1))  # learns the identity
    for _ in range(3):
        before = len(api.requests)
        p.standing(ref(1))
        paths = [urllib.parse.urlsplit(r.url).path for r in api.requests[before:]]
        assert paths == ["/repos/owner/repo/issues/1", "/repos/owner/repo/issues/1/comments"]
    api.issues[1].state = "closed"
    assert p.standing(ref(1)).reason == "task-closed"


def test_standing_and_held_claims_follow_every_page() -> None:
    api = FakeGitHub(page_size=2)
    for number in range(1, 6):
        api.add_issue(number, labels={READY})
    p = provider(api)
    for number in range(1, 6):
        for noise in range(3):
            api.comment(number, f"noise {noise}")
        p.claim(ref(number))
    assert {h.ref for h in p.held_claims()} == {ref(n) for n in range(1, 6)}
    assert all(p.standing(ref(n)).standing is Standing.HELD for n in range(1, 6))


def test_held_claims_includes_closed_issues_and_skips_pull_requests(api: FakeGitHub) -> None:
    claim = held(api)
    api.issues[1].state = "closed"
    api.add_issue(2, labels={READY, CLAIMED}, pull_request=True)
    api.comment(2, claim_comment("a"), user=ME)
    assert provider(api).held_claims() == (claim,)


# -- the targeted release: exactly the observed object ----------------------------------------


@pytest.mark.parametrize(
    "edit",
    [
        lambda body: body.replace("token=", "token=1"),
        lambda body: body.replace("ilmar:claim", "ilmar:released"),
        lambda body: body.replace("instance=a", "instance=b"),
    ],
    ids=["token", "marker", "instance"],
)
def test_an_edited_claim_is_changed_and_never_deleted(
    api: FakeGitHub, edit: Callable[[str], str]
) -> None:
    claim = held(api)
    api.edit_comment(int(claim.object_id), edit(api.comments[1][0]["body"]))
    before = bodies(api)
    p = provider(api)
    assert p.release_claim(claim) is ReleaseResult.CHANGED
    assert p.restore_label(claim) is RestoreResult.CHANGED
    assert bodies(api) == before
    assert CLAIMED in api.issues[1].labels


def test_a_claim_object_on_another_issue_is_changed(api: FakeGitHub) -> None:
    claim = held(api)
    api.add_issue(2, labels={READY})
    elsewhere = HeldClaim(ref(2), claim.claim_id, claim.object_id)
    assert provider(api).release_claim(elsewhere) is ReleaseResult.CHANGED
    assert len(api.comments[1]) == 1


def test_restore_label_writes_only_the_label(api: FakeGitHub) -> None:
    claim = held(api)
    api.issues[1].labels.discard(CLAIMED)
    before = len(api.requests)
    assert provider(api).restore_label(claim) is RestoreResult.RESTORED
    assert writes(api)[-1:] == ["POST /repos/owner/repo/issues/1/labels"]
    assert sum(r.method != "GET" for r in api.requests[before:]) == 1
    assert CLAIMED in api.issues[1].labels


def test_restore_label_beside_a_deleted_claim_posts_nothing(api: FakeGitHub) -> None:
    claim = held(api)
    api.issues[1].labels.discard(CLAIMED)
    api.comments[1].clear()  # a human deleted Ilmar's claim
    before = len(api.requests)
    assert provider(api).restore_label(claim) is RestoreResult.ABSENT
    assert all(r.method == "GET" for r in api.requests[before:])
    assert api.comments[1] == []
    assert CLAIMED not in api.issues[1].labels


# Every request the targeted release makes, in order, and an adversary that acts
# just before one of them.
WINDOWS = [
    ("GET", r"/repos/owner/repo/issues/comments/\d+"),  # the re-read of the object
    ("DELETE", r"/repos/owner/repo/issues/comments/\d+"),  # the delete
    ("GET", r"/repos/owner/repo/issues/1"),  # the label step: the issue
    ("GET", r"/repos/owner/repo/issues/1/comments"),  # the label step: its claims
    ("DELETE", r"/repos/owner/repo/issues/1/labels/.+"),  # the label removal
]


def _edit(api: FakeGitHub, object_id: int, body: Callable[[str], str]) -> None:
    for comment in api.comments[1]:
        if comment["id"] == object_id:  # nothing to edit once it is gone
            comment["body"] = body(str(comment["body"]))


def _delete(api: FakeGitHub, object_id: int) -> None:
    api.comments[1] = [c for c in api.comments[1] if c["id"] != object_id]


def _forge(api: FakeGitHub, object_id: int) -> None:
    api.comment(1, claim_comment("a", "0" * 32), user="mallory")


Adversary = Callable[[FakeGitHub, int], object]
ADVERSARIES: dict[str, Adversary] = {
    "newer-own-claim": lambda api, o: api.comment(1, claim_comment("a", "e" * 32), user=ME),
    "other-instance": lambda api, o: api.comment(1, claim_comment("b"), user=ME),
    "human-label": lambda api, o: api.issues[1].labels.add(HUMAN),
    "assignee": lambda api, o: api.issues[1].assignees.append("human"),
    "edit-token": lambda api, o: _edit(api, o, lambda b: b.replace("token=", "token=d")),
    "edit-marker": lambda api, o: _edit(api, o, lambda b: b.replace("ilmar:claim", "done")),
    "edit-instance": lambda api, o: _edit(api, o, lambda b: b.replace("=a ", "=b ")),
    "delete": _delete,
    "forged-copy": _forge,
    "close": lambda api, o: setattr(api.issues[1], "state", "closed"),
    "transfer": lambda api, o: api.transferred.__setitem__(1, "elsewhere/repo"),
}


EDITS = {"edit-token", "edit-marker", "edit-instance"}


@pytest.mark.parametrize("window", range(len(WINDOWS)))
@pytest.mark.parametrize("adversary", sorted(ADVERSARIES))
def test_an_orphan_release_deletes_only_the_observed_object(
    api: FakeGitHub, window: int, adversary: str
) -> None:
    claim = held(api)
    p = provider(api)
    p.standing(ref(1))  # identity learned before the adversary is armed
    method, pattern = WINDOWS[window]
    observed = dict(api.comments[1][0])
    snapshots: dict[int, dict[str, object]] = {}

    def act(request: HttpRequest) -> None:
        path = urllib.parse.urlsplit(request.url).path
        if not snapshots and request.method == method and re.fullmatch(pattern, path):
            ADVERSARIES[adversary](api, int(observed["id"]))
            snapshots.update({c["id"]: dict(c) for c in api.comments[1]})
            snapshots.setdefault(-1, {})  # fired, even if nothing is left

    api.before.append(act)
    try:
        result: ReleaseResult | None = p.release_claim(claim)
    except ProviderError:
        result = None  # the issue was transferred mid-step: nothing more is done
    assert snapshots, "the adversary never got its window"
    after = {c["id"]: c for c in api.comments[1]}
    others = {i: c for i, c in snapshots.items() if i not in (-1, observed["id"])}
    # Only the observed object is ever deleted; every other comment, including
    # those the adversary added, survives byte for byte.
    assert others == {i: c for i, c in after.items() if i != observed["id"]}
    if observed["id"] not in after:
        assert result in (ReleaseResult.RELEASED, ReleaseResult.ABSENT, None)
        # It was still ours when it was re-read: its author can never change,
        # and an edit of its text before the re-read made it CHANGED instead.
        assert not (window == 0 and adversary in EDITS)
    # An edit or a forged copy seen by the re-read: never deleted, never touched.
    if window == 0 and adversary in EDITS:
        assert result is ReleaseResult.CHANGED
        assert after[observed["id"]] == snapshots[observed["id"]]
    if window == 0 and adversary == "delete":
        assert result is ReleaseResult.ABSENT
    # Human labels and assignees are never touched.
    assert (HUMAN in api.issues[1].labels) is (adversary == "human-label")
    assert api.issues[1].assignees == (["human"] if adversary == "assignee" else [])
    # The label goes only if no claim of anyone remained at the final re-check.
    # It reads the issue, then its claims: a human label or assignee added after
    # the first read, or anything after the second, races the removal (ADR-0026
    # §5, "the remaining race is on the label alone"). The human's own label or
    # assignee still keeps the task out of the queue; a newer claim's owner
    # restores the label (next test).
    human = adversary in ("human-label", "assignee")
    raced = window == 4 or (window == 3 and human)
    if CLAIMED not in api.issues[1].labels and not raced:
        assert not [c for c in api.comments[1] if "ilmar:claim" in str(c["body"])]
        assert not human
    if human:
        assert ref(1) not in {t.ref for t in p.list_eligible()}
    if CLAIMED in api.issues[1].labels and result is not ReleaseResult.CHANGED:
        claims_left = [c for c in api.comments[1] if "ilmar:claim" in str(c["body"])]
        # A transferred issue is no longer this repository's: nothing more is done.
        assert claims_left or human or adversary == "transfer"


@pytest.mark.parametrize("how", ["deleted", "transferred"])
def test_releasing_a_vanished_task_is_done(api: FakeGitHub, how: str) -> None:
    held(api)
    if how == "deleted":
        api.deleted.add(1)
    else:
        api.transferred[1] = "elsewhere/repo"
    p = provider(api)
    p.release(ref(1))  # "not found" counts as done (ADR-0026 §3)
    p.release(ref(1))


@pytest.mark.parametrize("status", [500, 502, 429, 403])
def test_a_transient_identity_failure_never_falls_back_to_bot_login(status: int) -> None:
    """Review of #83: only a definitive 403 (not a rate limit) or 404 from
    ``GET /user`` lets ``bot_login`` stand in; anything else fails closed and is
    not cached, so the mismatch check still runs once ``/user`` answers."""
    api = FakeGitHub()
    api.add_issue(1, labels={READY})
    api.failures[("GET", "/user")] = status  # 403 here carries the rate-limit header
    p = provider(api, bot_login="someone-else")
    with pytest.raises(ProviderError):
        p.standing(ref(1))
    with pytest.raises(ProviderError):
        p.claim(ref(1))
    assert api.comments[1] == []  # nothing written under a guessed identity
    del api.failures[("GET", "/user")]
    with pytest.raises(ProviderError, match="does not match"):
        p.standing(ref(1))  # the real login is read now, and the mismatch is caught


def test_a_rate_limited_403_is_not_a_missing_endpoint() -> None:
    api = FakeGitHub()
    api.add_issue(1, labels={READY})
    api.user_login = None  # an installation token: a plain 403
    p = provider(api, bot_login=ME)
    assert p.claim(ref(1)).outcome is ClaimOutcome.CLAIMED  # bot_login stands in


# -- stray claim labels (ADR-0027 §1) ----------------------------------------------------


def stray(api: FakeGitHub, number: int = 1) -> None:
    """Issue ``number`` carries Ilmar's label and nothing behind it."""
    api.issues[number].labels.add(CLAIMED)


@pytest.mark.parametrize("skip_assigned", [True, False])
def test_an_assignee_keeps_the_label_only_where_it_is_a_claim(
    api: FakeGitHub, skip_assigned: bool
) -> None:
    stray(api)
    api.issues[1].assignees.append("human")
    p = provider(api, skip_assigned=skip_assigned)
    assert (ref(1) in p.stray_labels()) is not skip_assigned
    expected = ClearResult.KEPT if skip_assigned else ClearResult.CLEARED
    assert p.clear_label(ref(1)) is expected
    assert api.issues[1].assignees == ["human"]  # never touched


def test_stray_labels_follow_every_page() -> None:
    api = FakeGitHub(page_size=2)
    for number in range(1, 8):
        api.add_issue(number, labels={READY, CLAIMED})
        for noise in range(3):
            api.comment(number, f"noise {noise}")
    api.comment(7, claim_comment("b"), user=ME)
    assert set(provider(api).stray_labels()) == {ref(n) for n in range(1, 7)}


def test_a_forged_marker_by_a_non_member_keeps_the_label(api: FakeGitHub) -> None:
    stray(api)
    forged = api.comment(1, claim_comment("a", "f" * 32), user="drive-by")
    p = provider(api)
    assert p.stray_labels() == ()
    assert p.clear_label(ref(1)) is ClearResult.KEPT
    api.comments[1] = [c for c in api.comments[1] if c["id"] != forged]  # a person removes it
    assert p.stray_labels() == (ref(1),)
    assert p.clear_label(ref(1)) is ClearResult.CLEARED


def _transfer(api: FakeGitHub) -> None:
    api.transferred[1] = "elsewhere/repo"


@pytest.mark.parametrize(
    "call",
    ["get", "claim", "release", "standing", "held_claims", "stray_labels", "clear_label",
     "release_claim", "restore_label"],
)  # fmt: skip
def test_no_read_follows_a_transfer(api: FakeGitHub, call: str) -> None:
    """ADR-0027 §3: the fake fails the test if any request follows the redirect."""
    claim = held(api)
    _transfer(api)
    p = provider(api)
    if call == "get":
        assert p.get(ref(1)) is None
    elif call == "claim":
        assert p.claim(ref(1)).outcome is ClaimOutcome.NOT_ELIGIBLE
    elif call == "release":
        p.release(ref(1))
    elif call == "standing":
        assert p.standing(ref(1)).standing is Standing.GONE
    elif call == "held_claims":
        assert p.held_claims() == ()
    elif call == "stray_labels":
        assert p.stray_labels() == ()
    elif call == "clear_label":
        assert p.clear_label(ref(1)) is ClearResult.ABSENT
    elif call == "release_claim":
        p.release_claim(claim)
    else:
        p.restore_label(claim)


# The non-atomic removal: what happens between clear_label's final re-read (the
# comments) and its label DELETE, row by row (ADR-0027 §1, the race table).
RACES: dict[str, Callable[[FakeGitHub], object]] = {
    "other-instance-claims": lambda api: api.comment(1, claim_comment("b"), user=ME),
    "human-claim-label": lambda api: api.issues[1].labels.add(HUMAN),
    "assignee": lambda api: api.issues[1].assignees.append("human"),
    "assignee-removed": lambda api: api.issues[1].assignees.clear(),
    "forged-marker": lambda api: api.comment(1, claim_comment("a", "f" * 32), user="mallory"),
    "label-removed": lambda api: api.issues[1].labels.discard(CLAIMED),
    "label-re-added": lambda api: api.issues[1].labels.add(CLAIMED),
    "closed": lambda api: setattr(api.issues[1], "state", "closed"),
    "transferred": _transfer,
}


@pytest.mark.parametrize("race", sorted(RACES))
def test_the_non_atomic_label_removal_touches_nothing_else(api: FakeGitHub, race: str) -> None:
    stray(api)
    p = provider(api)
    p.stray_labels()  # identity learned before the adversary is armed
    fired: list[bool] = []

    def act(request: HttpRequest) -> None:
        path = urllib.parse.urlsplit(request.url).path
        if not fired and request.method == "DELETE" and "/labels/" in path:
            fired.append(True)
            RACES[race](api)

    api.before.append(act)
    comments = [dict(c) for c in api.comments[1]]
    result = p.clear_label(ref(1))
    assert fired
    assert result is ClearResult.CLEARED  # the re-read found nothing: the window is after it
    assert CLAIMED not in api.issues[1].labels  # only Ilmar's label went
    added = [c for c in api.comments[1] if c not in comments]
    assert [c for c in api.comments[1] if c in comments] == comments  # nothing deleted
    if race == "other-instance-claims":
        assert len(added) == 1
        # Repaired by the claim's owner: it reads HELD with the label missing.
        other = provider(api, "b")
        standing = other.standing(ref(1))
        assert standing.standing is Standing.HELD
        assert standing.label_missing
        assert standing.claim is not None
        assert other.restore_label(standing.claim) is RestoreResult.RESTORED
    if race in ("human-claim-label", "assignee"):
        # The human claim alone keeps the task out of the queue, and it is untouched.
        assert ref(1) not in {t.ref for t in p.list_eligible()}
        assert p.claim(ref(1)).outcome is ClaimOutcome.CLAIMED_BY_OTHER
    if race == "forged-marker":
        assert p.claim(ref(1)).outcome is ClaimOutcome.CLAIMED_BY_OTHER
