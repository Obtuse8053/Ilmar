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

    # -- test helpers ----------------------------------------------------

    def add_issue(self, number: int, title: str = "", **kwargs: Any) -> FakeIssue:
        issue = FakeIssue(number=number, title=title or f"Issue {number}", **kwargs)
        self.issues[number] = issue
        self.comments.setdefault(number, [])
        return issue

    def comment(self, number: int, body: str, *, user: str = "someone") -> int:
        comment_id = next(self._comment_ids)
        self.comments[number].append({"id": comment_id, "body": body, "user": {"login": user}})
        return comment_id

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
        prefix = f"/repos/{self.repository}"
        if not url.path.startswith(prefix + "/"):
            return _json(404, {"message": "Not Found"})
        return self._route(request.method, url.path[len(prefix) :], query, body, request.url)

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
                comment_id = self.comment(number, body["body"], user="ilmar-bot")
                return _json(201, {"id": comment_id, "body": body["body"]})
        if match := re.fullmatch(r"/issues/comments/(\d+)", path):
            comment_id = int(match.group(1))
            for comments in self.comments.values():
                for comment in comments:
                    if comment["id"] == comment_id and method == "DELETE":
                        comments.remove(comment)
                        return HttpResponse(204)
            return _json(404, {"message": "Not Found"})
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
        return _json(404, {"message": f"unsupported in fake: {method} {path}"})

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


def _json(status: int, data: Any, headers: dict[str, str] | None = None) -> HttpResponse:
    return HttpResponse(status, headers or {}, json.dumps(data).encode())


def claim_comment(instance: str) -> str:
    return f"Claimed.\n\n<!-- ilmar:claim instance={instance} -->"


def labelled(*names: str) -> set[str]:
    return set(names)


def iter_bodies(requests: Iterable[HttpRequest]) -> list[str]:
    return [r.body.decode() for r in requests if r.body]
