# SPDX-License-Identifier: Apache-2.0
"""An in-memory subset of the GitHub REST API, used as an HTTP transport in tests.

It implements only the endpoints Ilmar's GitHub adapters call. It answers
like GitHub does: the issues list includes pull requests, and pagination uses
``Link`` headers. It also enforces that every request carries the expected
token and API version.
"""

from __future__ import annotations

import itertools
import json
import re
import urllib.parse
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Any

from ilmar.adapters.github.client import API_VERSION, HttpRequest, HttpResponse

HOST = "api.github.com"
TOKEN = "test-token-not-a-secret"


@dataclass
class FakeIssue:
    number: int
    title: str
    body: str = ""
    labels: set[str] = field(default_factory=set)
    state: str = "open"
    assignees: list[str] = field(default_factory=list)
    pull_request: bool = False

    def as_json(self, repository: str) -> dict[str, Any]:
        data: dict[str, Any] = {
            "number": self.number,
            "title": self.title,
            "body": self.body,
            "state": self.state,
            "labels": [{"name": name} for name in sorted(self.labels)],
            "assignees": [{"login": login} for login in self.assignees],
            "html_url": f"https://github.com/{repository}/issues/{self.number}",
            "repository_url": f"https://{HOST}/repos/{repository}",
        }
        if self.pull_request:
            data["pull_request"] = {"url": "https://example.invalid"}
        return data


class FakeGitHub:
    def __init__(self, repository: str = "owner/repo", *, page_size: int = 100) -> None:
        self.repository = repository
        self.page_size = page_size
        self.issues: dict[int, FakeIssue] = {}
        self.comments: dict[int, list[dict[str, Any]]] = {}
        self.requests: list[HttpRequest] = []
        self.labels_created: set[str] = set()
        self._comment_ids = itertools.count(1000)
        self.before: list[Callable[[HttpRequest], None]] = []
        self.pulls: dict[int, dict[str, Any]] = {}
        self._pull_numbers = itertools.count(500)
        self.head_sha: Callable[[str], str] = lambda branch: "a" * 40
        self.check_runs: dict[str, list[dict[str, Any]]] = {}
        self.statuses: dict[str, list[dict[str, Any]]] = {}
        # Override what GitHub reports as total_count, to simulate truncation.
        self.reported_totals: dict[tuple[str, str], int] = {}
        # ADR-0026: the token's own account, as GET /user reports it (None: the
        # endpoint is unavailable, as for a GitHub App installation token).
        self.user_login: str | None = "ilmar-bot"
        self.poster = "ilmar-bot"  # the author of comments this token posts
        self.transferred: dict[int, str] = {}  # issue number -> new repository
        self.deleted: set[int] = set()  # answered 410 Gone
        self.repository_readable = True
        # (method, path regex) -> HTTP status, answered instead (403, 429, 5xx ...)
        self.failures: dict[tuple[str, str], int] = {}

    # -- test helpers ----------------------------------------------------

    def add_issue(self, number: int, title: str = "", **kwargs: Any) -> FakeIssue:
        issue = FakeIssue(number=number, title=title or f"Issue {number}", **kwargs)
        self.issues[number] = issue
        self.comments.setdefault(number, [])
        return issue

    def comment(self, number: int, body: str, *, user: str = "someone") -> int:
        comment_id = next(self._comment_ids)
        self.comments[number].append({
            "id": comment_id, "body": body, "user": {"login": user},
            "issue_url": f"https://{HOST}/repos/{self.repository}/issues/{number}",
        })  # fmt: skip
        return comment_id

    def edit_comment(self, comment_id: int, body: str) -> None:
        for comments in self.comments.values():
            for comment in comments:
                if comment["id"] == comment_id:
                    comment["body"] = body
                    return
        raise KeyError(comment_id)

    def add_pull(self, head: str, base: str = "main", body: str = "", state: str = "open") -> int:
        """A pull request opened by someone (no Ilmar marker unless ``body`` has one)."""
        number = next(self._pull_numbers)
        owner = self.repository.split("/")[0]
        self.pulls[number] = {
            "number": number,
            "title": f"PR {number}",
            "body": body,
            "state": state,
            "html_url": f"https://github.com/{self.repository}/pull/{number}",
            "head": {"ref": head, "label": f"{owner}:{head}"},
            "base": {"ref": base},
        }
        return number

    # -- transport -------------------------------------------------------

    def __call__(self, request: HttpRequest) -> HttpResponse:
        for hook in list(self.before):
            hook(request)
        self.requests.append(request)
        url = urllib.parse.urlsplit(request.url)
        if url.scheme != "https" or url.hostname != HOST:
            raise AssertionError(f"request to unexpected host: {request.url}")
        if request.headers.get("Authorization") != f"Bearer {TOKEN}":
            return _json(401, {"message": "Bad credentials"})
        if request.headers.get("X-GitHub-Api-Version") != API_VERSION:
            return _json(400, {"message": "missing API version"})
        query = dict(urllib.parse.parse_qsl(url.query))
        body = json.loads(request.body) if request.body else None
        for (method, pattern), status in self.failures.items():
            if method == request.method and re.fullmatch(pattern, url.path):
                headers = {"x-ratelimit-remaining": "0"} if status in (403, 429) else {}
                return _json(status, {"message": f"injected {status}"}, headers)
        if url.path == "/user" and request.method == "GET":
            if self.user_login is None:
                return _json(403, {"message": "Resource not accessible by integration"})
            return _json(200, {"login": self.user_login})
        prefix = f"/repos/{self.repository}"
        if url.path == prefix and request.method == "GET":
            if not self.repository_readable:
                return _json(404, {"message": "Not Found"})
            return _json(200, {"full_name": self.repository})
        if not url.path.startswith(prefix + "/"):
            return _json(404, {"message": "Not Found"})
        path = url.path[len(prefix) :]
        if (match := re.fullmatch(r"/issues/(\d+)(/.*)?", path)) and request.method == "GET":
            number = int(match.group(1))
            if number in self.deleted:
                return _json(410, {"message": "This issue was deleted"})
            if number in self.transferred:
                if request.follow_redirects:
                    raise AssertionError("a transferred issue was followed")
                new = f"https://{HOST}/repos/{self.transferred[number]}/issues/{number}"
                return HttpResponse(301, {"Location": new}, b"")
        return self._route(request.method, path, query, body, request.url)

    def _route(
        self, method: str, path: str, query: dict[str, str], body: Any, full_url: str
    ) -> HttpResponse:
        if method == "GET" and path == "/issues":
            wanted = set(filter(None, query.get("labels", "").split(",")))
            state = query.get("state", "open")
            found = [
                i.as_json(self.repository)
                for i in sorted(self.issues.values(), key=lambda i: -i.number)
                if (state == "all" or i.state == state) and wanted <= i.labels
            ]
            return self._page(found, query, full_url)
        if match := re.fullmatch(r"/issues/(\d+)", path):
            issue = self.issues.get(int(match.group(1)))
            if method == "GET" and issue is not None:
                return _json(200, issue.as_json(self.repository))
            return _json(404, {"message": "Not Found"})
        if match := re.fullmatch(r"/issues/(\d+)/comments", path):
            number = int(match.group(1))
            if number not in self.issues:
                return _json(404, {"message": "Not Found"})
            if method == "GET":
                return self._page(list(self.comments[number]), query, full_url)
            if method == "POST":
                comment_id = self.comment(number, body["body"], user=self.poster)
                return _json(201, {"id": comment_id, "body": body["body"]})
        if match := re.fullmatch(r"/issues/comments/(\d+)", path):
            comment_id = int(match.group(1))
            for comments in self.comments.values():
                for comment in comments:
                    if comment["id"] == comment_id and method == "DELETE":
                        comments.remove(comment)
                        return HttpResponse(204)
                    if comment["id"] == comment_id and method == "GET":
                        return _json(200, comment)
            return _json(404, {"message": "Not Found"})
        if (match := re.fullmatch(r"/issues/(\d+)/labels", path)) and int(
            match.group(1)
        ) in self.pulls:
            labelled = self.pulls[int(match.group(1))]
            names = {label["name"] for label in labelled.get("labels", [])} | set(body["labels"])
            labelled["labels"] = [{"name": n} for n in sorted(names)]
            return _json(200, labelled["labels"])
        if match := re.fullmatch(r"/issues/(\d+)/labels", path):
            issue = self.issues.get(int(match.group(1)))
            if issue is None or method != "POST":
                return _json(404, {"message": "Not Found"})
            for name in body["labels"]:
                self.labels_created.add(name)
                issue.labels.add(name)
            return _json(200, [{"name": n} for n in sorted(issue.labels)])
        if match := re.fullmatch(r"/issues/(\d+)/labels/(.+)", path):
            issue = self.issues.get(int(match.group(1)))
            name = urllib.parse.unquote(match.group(2))
            if issue is None or method != "DELETE" or name not in issue.labels:
                return _json(404, {"message": "Label does not exist"})
            issue.labels.discard(name)
            return _json(200, [{"name": n} for n in sorted(issue.labels)])
        if path == "/pulls":
            if method == "GET":
                found = [
                    self._pull_json(p)
                    for p in self.pulls.values()
                    if p["state"] == query.get("state", "open")
                    and ("head" not in query or p["head"]["label"] == query["head"])
                ]
                return self._page(found, query, full_url)
            if method == "POST":
                if any(
                    p["state"] == "open" and p["head"]["ref"] == body["head"]
                    for p in self.pulls.values()
                ):
                    return _json(422, {"message": "A pull request already exists"})
                number = self.add_pull(body["head"], body["base"], body["body"])
                self.pulls[number]["title"] = body["title"]
                return _json(201, self._pull_json(self.pulls[number]))
        if match := re.fullmatch(r"/pulls/(\d+)", path):
            pull = self.pulls.get(int(match.group(1)))
            if pull is None:
                return _json(404, {"message": "Not Found"})
            if method == "PATCH":
                pull.update({k: v for k, v in body.items() if k in {"title", "body"}})
            return _json(200, self._pull_json(pull))
        if match := re.fullmatch(r"/commits/([0-9a-f]{40})/check-runs", path):
            runs = self.check_runs.get(match.group(1), [])
            total = self.reported_totals.get(("check_runs", match.group(1)), len(runs))
            return _json(200, {"total_count": total, "check_runs": _slice(runs, query)})
        if match := re.fullmatch(r"/commits/([0-9a-f]{40})/status", path):
            statuses = self.statuses.get(match.group(1), [])
            total = self.reported_totals.get(("statuses", match.group(1)), len(statuses))
            return _json(200, {"total_count": total, "statuses": _slice(statuses, query)})
        return _json(404, {"message": f"unsupported in fake: {method} {path}"})

    def _pull_json(self, pull: dict[str, Any]) -> dict[str, Any]:
        head = dict(pull["head"]) | {"sha": self.head_sha(pull["head"]["ref"])}
        return dict(pull) | {"head": head}

    def _page(self, items: list[Any], query: dict[str, str], full_url: str) -> HttpResponse:
        size = min(int(query.get("per_page", "30")), self.page_size)
        page = int(query.get("page", "1"))
        chunk = items[(page - 1) * size : page * size]
        headers = {}
        if page * size < len(items):
            parts = urllib.parse.urlsplit(full_url)
            next_query = dict(urllib.parse.parse_qsl(parts.query)) | {"page": str(page + 1)}
            next_url = parts._replace(query=urllib.parse.urlencode(next_query)).geturl()
            headers["Link"] = f'<{next_url}>; rel="next"'
        return _json(200, chunk, headers)


def _slice(items: list[Any], query: dict[str, str]) -> list[Any]:
    size = min(int(query.get("per_page", "30")), 100)
    page = int(query.get("page", "1"))
    return items[(page - 1) * size : page * size]


def _json(status: int, data: Any, headers: dict[str, str] | None = None) -> HttpResponse:
    return HttpResponse(status, headers or {}, json.dumps(data).encode())


def claim_comment(instance: str, token: str = "0" * 32) -> str:
    return f"Claimed.\n\n<!-- ilmar:claim instance={instance} token={token} -->"


def labelled(*names: str) -> set[str]:
    return set(names)


def iter_bodies(requests: Iterable[HttpRequest]) -> list[str]:
    return [r.body.decode() for r in requests if r.body]
