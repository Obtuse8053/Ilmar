# SPDX-License-Identifier: Apache-2.0
"""GitHub Issues as an Ilmar ``TaskProvider`` (ADR-0013).

**Threat model (M2).** Exactly one active coordinator per Ilmar instance
(ADR-0012) and one instance per repository. GitHub's REST API offers no atomic
compare-and-set for labels, assignees or comments, so nothing here is an
exclusive lock between instances. Duplicate work *within* an instance is
prevented by the execution store (ADR-0005). A second instance on the same
repository is a misconfiguration, which this adapter *detects* and backs off
from but cannot fully prevent.

State model:

* **Eligible:** an open issue (not a pull request) carrying ``eligible_label``,
  none of ``excluded_labels``, not ``claimed_label``, and unassigned (unless
  ``skip_assigned`` is off).
* ``claimed_label`` is the workflow state "taken by Ilmar". It is **owned by
  Ilmar**: it is the only label Ilmar ever adds or removes, and it must not be
  a label humans use. Labels carry no provenance, so humans claim work with
  their own labels (``human_claim_labels``, e.g. ``status:in-progress``) or by
  assigning someone. Ilmar never adds, removes or reinterprets those; any of
  them, or any assignee, means "claimed by someone else". Ilmar never assigns.
* A claim comment ``<!-- ilmar:claim instance=<id> token=<random> -->`` is an
  **audit record**, not a lock. Ownership is per instance. Whether the claim is
  still backed by an execution is decided by the instance's durable execution
  store during reconciliation (#12, #15).
* If, before or after posting its claim, an instance sees a claim from
  *another* instance, it withdraws its own and reports ``CLAIMED_BY_OTHER``. In
  an overlapping race both back off: safety over progress.
* **Authentic own claims (ADR-0026 §6).** A claim comment is this instance's
  only if its marker names this instance **and** its author is the adapter's
  own identity: the login of ``GET /user``, or the configured ``bot_login``
  where that endpoint is unavailable (GitHub App installation tokens). A
  marker naming this instance written by anyone else is another party's
  claim: Ilmar backs off from it and never deletes it. An unknown identity
  fails closed.
* **Standing (ADR-0026 §1).** ``standing`` reads the issue and its comments
  afresh on every call, with no cache. A deleted or transferred issue is
  ``GONE`` only once the repository itself reads back; a redirect is never
  followed into another repository. Every other uncertainty is a
  ``ProviderError``.
* **Targeted writes (ADR-0026 §5).** ``release_claim`` and ``restore_label``
  re-read exactly one claim comment by its id and act only if it is still this
  instance's authentic claim with the recorded token.
"""

from __future__ import annotations

import contextlib
import re
import secrets
import urllib.parse
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

from ilmar.adapters.github.client import (
    GitHubClient,
    GitHubForbiddenError,
    GitHubMovedError,
    GitHubNotFoundError,
)
from ilmar.core.naming import safe_identifier
from ilmar.domain.errors import ProviderError
from ilmar.domain.tasks import (
    Claim,
    ClaimOutcome,
    ClearResult,
    HeldClaim,
    ReleaseResult,
    RestoreResult,
    Standing,
    Task,
    TaskRef,
    TaskStanding,
)

_REPOSITORY = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_CLAIM = re.compile(r"<!-- ilmar:claim instance=([A-Za-z0-9_-]+) token=([0-9a-f]{16,64}) -->")
_LOGIN = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})(?:\[bot\])?$")


@dataclass(frozen=True, slots=True)
class _ClaimComment:
    comment_id: int
    instance: str
    token: str
    author: str


def check_settings(
    *,
    repository: str,
    eligible_label: str,
    claimed_label: str,
    excluded_labels: Sequence[str],
    human_claim_labels: Sequence[str],
    bot_login: str | None = None,
) -> None:
    """The rules for a provider's settings; used before any provider is built (ADR-0019)."""
    if bot_login is not None and not _LOGIN.match(bot_login):
        raise ValueError("bot_login must be a GitHub login, such as 'my-app[bot]'")
    if not _REPOSITORY.match(repository):
        raise ValueError("repository must look like 'owner/name'")
    if eligible_label == claimed_label:
        raise ValueError("eligible_label and claimed_label must differ")
    if claimed_label in {*excluded_labels, *human_claim_labels}:
        raise ValueError(
            "claimed_label is owned by Ilmar and must not also be an excluded "
            "or human claim label (ADR-0013)"
        )


class GitHubTaskProvider:
    def __init__(
        self,
        client: GitHubClient,
        *,
        repository: str,
        instance_id: str,
        name: str = "github",
        eligible_label: str = "ilmar:ready",
        claimed_label: str = "ilmar:claimed",
        excluded_labels: Sequence[str] = (),
        human_claim_labels: Sequence[str] = (),
        skip_assigned: bool = True,
        bot_login: str | None = None,
        new_token: Callable[[], str] = lambda: secrets.token_hex(16),
    ) -> None:
        check_settings(
            repository=repository,
            eligible_label=eligible_label,
            claimed_label=claimed_label,
            excluded_labels=excluded_labels,
            human_claim_labels=human_claim_labels,
            bot_login=bot_login,
        )
        self._client = client
        self._repository = repository
        self._instance = safe_identifier(instance_id, "instance id")
        self._name = name
        self._eligible = eligible_label
        self._claimed = claimed_label
        self._excluded = frozenset(excluded_labels)
        self._human_claims = frozenset(human_claim_labels)
        self._skip_assigned = skip_assigned
        self._bot_login = bot_login
        self._login: str | None = None  # learned once; never a tracker standing
        self._new_token = new_token

    @property
    def name(self) -> str:
        return self._name

    # -- TaskProvider ----------------------------------------------------

    def list_eligible(self) -> Sequence[Task]:
        issues = self._client.paginate(
            self._repo_path("/issues"),
            query={"state": "open", "labels": self._eligible, "per_page": "100"},
        )
        return tuple(self._task(issue) for issue in issues if self._is_eligible(issue))

    def get(self, ref: TaskRef) -> Task | None:
        number = self._number(ref)
        if number is None:
            return None
        issue = self._issue(number)
        if issue is None or "pull_request" in issue:
            return None
        return self._task(issue)

    def claim(self, ref: TaskRef) -> Claim:
        self._own_login()  # an unknown identity fails closed, before any write
        number = self._number(ref)
        issue = None if number is None else self._issue(number)
        if number is None or issue is None or not self._is_open_task(issue):
            return Claim(ClaimOutcome.NOT_ELIGIBLE)
        claims = self._claims(number)
        own = [c for c in claims if self._authentic(c)]
        if len(own) != len(claims) or self._claimed_by_human(issue):
            return Claim(ClaimOutcome.CLAIMED_BY_OTHER)  # forged own markers included
        if own:  # only ours: held by this instance (reconciled by the coordinator)
            self._add_label(number)
            return Claim(ClaimOutcome.CLAIMED, own[0].token, str(own[0].comment_id))
        if self._claimed in _labels(issue):
            # Our label without any claim comment: a leftover. Stay conservative;
            # reconciliation (ADR-0026) or a human clears it.
            return Claim(ClaimOutcome.CLAIMED_BY_OTHER)
        if not self._has_eligible_labels(issue):
            return Claim(ClaimOutcome.NOT_ELIGIBLE)

        token = self._new_token()
        mine = self._post_comment(
            number,
            f"Ilmar instance `{self._instance}` is working on this task.\n\n"
            f"<!-- ilmar:claim instance={self._instance} token={token} -->",
        )
        self._add_label(number)
        if any(not self._authentic(c) for c in self._claims(number)):
            # Another party is claiming too: a misconfiguration (ADR-0013). Back off.
            self._withdraw(number, [mine])
            return Claim(ClaimOutcome.CLAIMED_BY_OTHER)
        return Claim(ClaimOutcome.CLAIMED, token, str(mine))

    def release(self, ref: TaskRef) -> None:
        self._own_login()
        number = self._number(ref)
        if number is None:
            return
        try:
            claims = self._claims(number)
        except (GitHubNotFoundError, GitHubMovedError):
            return  # the task is gone: nothing to release, and that counts as done
        mine = [c.comment_id for c in claims if self._authentic(c)]
        if mine:  # never ours: leave other claims and forged copies alone
            self._withdraw(number, mine)
        else:
            # Ilmar's claim is already gone (a person deleted it): its label is
            # still Ilmar's to remove (ADR-0026 §4), but only if no claim of
            # anyone remains (§5, step 5).
            self._drop_label_if_unclaimed(number)

    def post_update(self, ref: TaskRef, message: str) -> None:
        number = self._number(ref)
        if number is None:
            raise ProviderError(f"{ref} does not belong to {self._repository}")
        try:
            self._post_comment(number, message)
        except GitHubNotFoundError as exc:
            raise ProviderError(f"unknown task {ref}") from exc

    def standing(self, ref: TaskRef) -> TaskStanding:
        self._own_login()  # an unknown identity fails closed, before any read
        number = self._number(ref)
        if number is None:
            raise ProviderError(f"{ref} does not belong to {self._repository}")
        try:
            issue = self._client.request(
                "GET", self._repo_path(f"/issues/{number}"), follow_redirects=False
            )
        except (GitHubNotFoundError, GitHubMovedError):
            return self._gone()  # deleted, or transferred: never followed
        if not isinstance(issue, dict) or "pull_request" in issue:
            raise ProviderError(f"{ref} is not an issue of {self._repository}")
        if not self._same_repository(issue):
            return self._gone()
        claims = self._claims(number)
        own = [c for c in claims if self._authentic(c)]
        labels = _labels(issue)
        reasons = set()
        if issue.get("state") != "open":
            reasons.add("task-closed")
        if self._eligible not in labels:
            reasons.add("task-unlabelled")
        if labels & self._excluded:
            reasons.add("task-excluded")
        if self._claimed_by_human(issue):
            reasons.add("task-claimed-by-human")
        if len(own) != len(claims):
            reasons.add("task-claimed-by-other-instance")
        if not own:
            reasons.add("task-claim-lost")
        if reasons:
            return TaskStanding.decisive(reasons)
        first = own[0]
        return TaskStanding(
            Standing.HELD,
            claim=HeldClaim(ref, first.token, str(first.comment_id)),
            label_missing=self._claimed not in labels,
        )

    def held_claims(self) -> Sequence[HeldClaim]:
        self._own_login()
        issues = self._client.paginate(
            self._repo_path("/issues"),
            query={"state": "all", "labels": self._claimed, "per_page": "100"},
            follow_redirects=False,
        )
        held = []
        for issue in issues:
            if "pull_request" in issue or self._claimed not in _labels(issue):
                continue
            ref = TaskRef(self._name, f"{self._repository}#{int(issue['number'])}")
            try:
                claims = self._claims(int(issue["number"]))
            except (GitHubNotFoundError, GitHubMovedError):
                continue  # gone or moved since it was listed: not ours to act on here
            held += [
                HeldClaim(ref, c.token, str(c.comment_id)) for c in claims if self._authentic(c)
            ]
        return tuple(held)

    def release_claim(self, claim: HeldClaim) -> ReleaseResult:
        number = self._claim_number(claim)
        found = self._claim_object(number, claim)
        if found is None:
            result = ReleaseResult.ABSENT
        elif found is False:
            return ReleaseResult.CHANGED  # no longer our authentic claim: never deleted
        else:
            self._delete_comment(int(claim.object_id))  # exactly that object, by its id
            result = ReleaseResult.RELEASED
        self._drop_label_if_unclaimed(number)
        return result

    def stray_labels(self) -> Sequence[TaskRef]:
        self._own_login()
        issues = self._client.paginate(
            self._repo_path("/issues"),
            query={"state": "all", "labels": self._claimed, "per_page": "100"},
            follow_redirects=False,
        )
        stray = []
        for issue in issues:
            if "pull_request" in issue or self._claimed not in _labels(issue):
                continue
            if self._claimed_by_human(issue):
                continue
            try:
                claims = self._claims(int(issue["number"]))
            except (GitHubNotFoundError, GitHubMovedError):
                continue  # gone or moved since it was listed
            if claims:
                continue  # any claim of anyone, forged ones included, keeps the label
            stray.append(TaskRef(self._name, f"{self._repository}#{int(issue['number'])}"))
        return tuple(stray)

    def clear_label(self, ref: TaskRef) -> ClearResult:
        self._own_login()
        number = self._number(ref)
        if number is None:
            raise ProviderError(f"{ref} does not belong to {self._repository}")
        issue = self._issue(number)  # a fresh re-read; a transfer is never followed
        if issue is None or self._claimed not in _labels(issue):
            return ClearResult.ABSENT
        try:
            claims = self._claims(number)
        except (GitHubNotFoundError, GitHubMovedError):
            return ClearResult.ABSENT
        if claims or self._claimed_by_human(issue):
            return ClearResult.KEPT
        # Not atomic (ADR-0013): a claim posted from here on loses only this label,
        # and its owner restores it (ADR-0027 §1).
        self._remove_label(number)
        return ClearResult.CLEARED

    def restore_label(self, claim: HeldClaim) -> RestoreResult:
        number = self._claim_number(claim)
        found = self._claim_object(number, claim)
        if found is None:
            return RestoreResult.ABSENT
        if found is False:
            return RestoreResult.CHANGED
        self._add_label(number)  # the only write: never a claim, never a removal
        return RestoreResult.RESTORED

    # -- helpers ---------------------------------------------------------

    def _own_login(self) -> str:
        """This adapter's own identity: ``GET /user`` once, else ``bot_login``.

        Fails closed: with neither, or with a configured login that differs
        from the token's own, nothing can be recognised as ours.
        """
        if self._login is not None:
            return self._login
        try:
            user = self._client.request("GET", "/user", follow_redirects=False)
        except (GitHubForbiddenError, GitHubNotFoundError):
            user = None  # definitively unavailable, as for an App installation token
        # Any other failure (5xx, a rate limit, the network) propagates: nothing is
        # cached, so a transient error never substitutes bot_login for the token's
        # own login and never skips the mismatch check below.
        login = user.get("login") if isinstance(user, dict) else None
        if isinstance(login, str) and _LOGIN.match(login):
            if self._bot_login is not None and login != self._bot_login:
                raise ProviderError("bot_login does not match the token's own login")
            self._login = login
        elif self._bot_login is not None:
            self._login = self._bot_login
        else:
            raise ProviderError("the token's own login is unknown; configure bot_login (ADR-0026)")
        return self._login

    def _authentic(self, claim: _ClaimComment) -> bool:
        return claim.instance == self._instance and claim.author == self._own_login()

    def _gone(self) -> TaskStanding:
        """``GONE`` only with the repository itself readable (ADR-0026 §1)."""
        try:
            repository = self._client.request("GET", self._repo_path(""), follow_redirects=False)
        except ProviderError:
            raise ProviderError("the issue is unreadable and so is its repository") from None
        name = repository.get("full_name") if isinstance(repository, dict) else None
        if not isinstance(name, str) or name.lower() != self._repository.lower():
            raise ProviderError("the repository did not read back as itself")
        return TaskStanding(Standing.GONE, "task-gone")

    def _same_repository(self, issue: dict[str, Any]) -> bool:
        url = issue.get("repository_url")
        if url is None:
            return True  # an answer to this repository's own path
        return str(url).lower().endswith(f"/repos/{self._repository}".lower())

    def _claim_number(self, claim: HeldClaim) -> int:
        number = self._number(claim.ref)
        if number is None or not claim.object_id.isdigit():
            raise ProviderError(f"{claim.ref} is not a claim of {self._repository}")
        return number

    def _claim_object(self, number: int, claim: HeldClaim) -> _ClaimComment | bool | None:
        """Re-read exactly the claim's comment by its id: ``None`` if it is gone,
        ``False`` if it is no longer this instance's authentic claim with that
        token on that issue, otherwise the claim."""
        try:
            comment = self._client.request(
                "GET",
                self._repo_path(f"/issues/comments/{int(claim.object_id)}"),
                follow_redirects=False,
            )
        except GitHubNotFoundError:
            return None
        parsed = _claim_of(comment)
        issue_url = str(comment.get("issue_url", "")) if isinstance(comment, dict) else ""
        on_issue = issue_url.lower().endswith(f"/repos/{self._repository}/issues/{number}".lower())
        if parsed is None or not on_issue or not self._authentic(parsed):
            return False
        return parsed if parsed.token == claim.claim_id else False

    def _drop_label_if_unclaimed(self, number: int) -> None:
        """Remove ``claimed_label`` only if no claim of anyone remains: no claim
        comment, no human claim label and no assignee (ADR-0026 §5, step 5)."""
        issue = self._issue(number)
        if issue is None or self._claimed not in _labels(issue):
            return
        if self._claims(number) or self._claimed_by_human(issue):
            return
        self._remove_label(number)

    def _is_open_task(self, issue: Any) -> bool:
        return issue.get("state") == "open" and "pull_request" not in issue

    def _has_eligible_labels(self, issue: Any) -> bool:
        labels = _labels(issue)
        return self._eligible in labels and not labels & self._excluded

    def _assigned(self, issue: Any) -> bool:
        return self._skip_assigned and bool(issue.get("assignees"))

    def _claimed_by_human(self, issue: Any) -> bool:
        return self._assigned(issue) or bool(_labels(issue) & self._human_claims)

    def _is_eligible(self, issue: Any) -> bool:
        return (
            self._is_open_task(issue)
            and self._has_eligible_labels(issue)
            and self._claimed not in _labels(issue)
            and not self._claimed_by_human(issue)
        )

    def _withdraw(self, number: int, comment_ids: list[int]) -> None:
        """Delete our claim comments; drop Ilmar's own label once no claim remains.

        Only ``claimed_label`` is ever removed; human claim labels are never touched.
        """
        for comment_id in comment_ids:
            self._delete_comment(comment_id)
        if not self._claims(number):
            self._remove_label(number)

    def _claims(self, number: int) -> list[_ClaimComment]:
        """Every claim comment, by anyone, oldest first."""
        comments = self._client.paginate(
            self._repo_path(f"/issues/{number}/comments"),
            query={"per_page": "100"},
            follow_redirects=False,
        )
        return sorted(
            (c for c in map(_claim_of, comments) if c is not None), key=lambda c: c.comment_id
        )

    def _issue(self, number: int) -> Any:
        """The issue, or ``None`` if it is gone; a transfer is never followed."""
        try:
            issue = self._client.request(
                "GET", self._repo_path(f"/issues/{number}"), follow_redirects=False
            )
        except (GitHubNotFoundError, GitHubMovedError):
            return None
        return issue if isinstance(issue, dict) and self._same_repository(issue) else None

    def _post_comment(self, number: int, body: str) -> int:
        created = self._client.request(
            "POST", self._repo_path(f"/issues/{number}/comments"), body={"body": body}
        )
        return int(created["id"])

    def _delete_comment(self, comment_id: int) -> None:
        with contextlib.suppress(GitHubNotFoundError):
            self._client.request("DELETE", self._repo_path(f"/issues/comments/{comment_id}"))

    def _add_label(self, number: int) -> None:
        self._client.request(
            "POST", self._repo_path(f"/issues/{number}/labels"), body={"labels": [self._claimed]}
        )

    def _remove_label(self, number: int) -> None:
        label = _quote(self._claimed)
        with contextlib.suppress(GitHubNotFoundError):
            self._client.request("DELETE", self._repo_path(f"/issues/{number}/labels/{label}"))

    def _task(self, issue: Any) -> Task:
        return Task(
            ref=TaskRef(self._name, f"{self._repository}#{int(issue['number'])}"),
            title=str(issue.get("title") or ""),
            description=str(issue.get("body") or ""),
            labels=frozenset(_labels(issue)),
            url=issue.get("html_url"),
        )

    def _number(self, ref: TaskRef) -> int | None:
        if ref.provider != self._name:
            return None
        repository, _, number = ref.key.rpartition("#")
        if repository != self._repository or not number.isdigit():
            return None
        return int(number)

    def _repo_path(self, suffix: str) -> str:
        return f"/repos/{self._repository}{suffix}"


def _claim_of(comment: Any) -> _ClaimComment | None:
    if not isinstance(comment, dict):
        return None
    match = _CLAIM.search(str(comment.get("body", "")))
    user = comment.get("user")
    author = user.get("login") if isinstance(user, dict) else None
    if match is None or not isinstance(author, str):
        return None
    return _ClaimComment(int(comment["id"]), match.group(1), match.group(2), author)


def _labels(issue: Any) -> set[str]:
    return {str(label.get("name")) for label in issue.get("labels", []) if isinstance(label, dict)}


def _quote(text: str) -> str:
    return urllib.parse.quote(text, safe="")
