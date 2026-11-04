# SPDX-License-Identifier: Apache-2.0
"""GitHub Issues as an Ilmar ``TaskProvider`` (ADR-0013).

State model:

* **Eligible:** an open issue (not a pull request) carrying ``eligible_label``,
  none of ``excluded_labels``, not ``claimed_label``, and with no assignees
  (unless ``skip_assigned`` is off).
* **Claimed by this instance:** a claim comment carrying this instance's
  marker. ``claimed_label`` is added so humans and other tools can see it.
* **Claimed by someone else:** ``claimed_label`` without our marker (a human
  or another tool, such as a session following ``docs/workflow.md``), an
  earlier claim comment from another instance, or an assignee.

GitHub has no compare-and-set for labels. Two instances racing for the same
issue are resolved by their claim comments: after posting its own, each
instance re-reads the comments, the earliest active claim wins, and the loser
deletes its comment.
"""

from __future__ import annotations

import contextlib
import re
import urllib.parse
from collections.abc import Sequence
from typing import Any

from ilmar.adapters.github.client import GitHubClient, GitHubNotFoundError
from ilmar.core.naming import safe_identifier
from ilmar.domain.errors import ProviderError
from ilmar.domain.tasks import ClaimOutcome, Task, TaskRef

_REPOSITORY = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_CLAIM = re.compile(r"<!-- ilmar:claim instance=([A-Za-z0-9_-]+) -->")


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
        skip_assigned: bool = True,
    ) -> None:
        if not _REPOSITORY.match(repository):
            raise ValueError(f"repository must look like 'owner/name', got {repository!r}")
        if eligible_label == claimed_label:
            raise ValueError("eligible_label and claimed_label must differ")
        self._client = client
        self._repository = repository
        self._instance = safe_identifier(instance_id, "instance id")
        self._name = name
        self._eligible = eligible_label
        self._claimed = claimed_label
        self._excluded = frozenset(excluded_labels)
        self._skip_assigned = skip_assigned

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

    def claim(self, ref: TaskRef) -> ClaimOutcome:
        number = self._number(ref)
        issue = None if number is None else self._issue(number)
        if number is None or issue is None or not self._is_open_task(issue):
            return ClaimOutcome.NOT_ELIGIBLE
        claims = self._active_claims(number)
        if any(instance == self._instance for _, instance in claims):
            self._add_label(number)
            return ClaimOutcome.CLAIMED
        if claims or self._claimed in _labels(issue) or self._assigned(issue):
            return ClaimOutcome.CLAIMED_BY_OTHER
        if not self._has_eligible_labels(issue):
            return ClaimOutcome.NOT_ELIGIBLE

        mine = self._post_comment(
            number,
            f"Ilmar instance `{self._instance}` is working on this task.\n\n"
            f"<!-- ilmar:claim instance={self._instance} -->",
        )
        self._add_label(number)
        winner = min(self._active_claims(number), default=None)
        if winner is not None and winner[1] == self._instance:
            return ClaimOutcome.CLAIMED
        self._delete_comment(mine)
        return ClaimOutcome.CLAIMED_BY_OTHER

    def release(self, ref: TaskRef) -> None:
        number = self._number(ref)
        if number is None:
            return
        claims = self._active_claims(number)
        mine = [comment_id for comment_id, instance in claims if instance == self._instance]
        if not mine:
            return  # never ours: leave other claims and labels alone
        for comment_id in mine:
            self._delete_comment(comment_id)
        if len(mine) == len(claims):
            self._remove_label(number)

    def post_update(self, ref: TaskRef, message: str) -> None:
        number = self._number(ref)
        if number is None:
            raise ProviderError(f"{ref} does not belong to {self._repository}")
        try:
            self._post_comment(number, message)
        except GitHubNotFoundError as exc:
            raise ProviderError(f"unknown task {ref}") from exc

    # -- helpers ---------------------------------------------------------

    def _is_open_task(self, issue: Any) -> bool:
        return issue.get("state") == "open" and "pull_request" not in issue

    def _has_eligible_labels(self, issue: Any) -> bool:
        labels = _labels(issue)
        return self._eligible in labels and not labels & self._excluded

    def _assigned(self, issue: Any) -> bool:
        return self._skip_assigned and bool(issue.get("assignees"))

    def _is_eligible(self, issue: Any) -> bool:
        return (
            self._is_open_task(issue)
            and self._has_eligible_labels(issue)
            and self._claimed not in _labels(issue)
            and not self._assigned(issue)
        )

    def _active_claims(self, number: int) -> list[tuple[int, str]]:
        """``(comment id, instance)`` for every claim comment, oldest first."""
        comments = self._client.paginate(
            self._repo_path(f"/issues/{number}/comments"), query={"per_page": "100"}
        )
        claims = []
        for comment in comments:
            match = _CLAIM.search(str(comment.get("body", "")))
            if match:
                claims.append((int(comment["id"]), match.group(1)))
        return sorted(claims)

    def _issue(self, number: int) -> Any:
        try:
            return self._client.request("GET", self._repo_path(f"/issues/{number}"))
        except GitHubNotFoundError:
            return None

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


def _labels(issue: Any) -> set[str]:
    return {str(label.get("name")) for label in issue.get("labels", []) if isinstance(label, dict)}


def _quote(text: str) -> str:
    return urllib.parse.quote(text, safe="")
