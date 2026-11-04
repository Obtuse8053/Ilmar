# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import json

import pytest

from ilmar.adapters.github.client import (
    API_VERSION,
    GitHubClient,
    GitHubNotFoundError,
    HttpRequest,
    HttpResponse,
)
from ilmar.domain.errors import ProviderError

from fake_github import TOKEN, FakeGitHub


def test_requests_carry_auth_version_and_accept_headers() -> None:
    api = FakeGitHub()
    api.add_issue(1)
    GitHubClient(TOKEN, transport=api).request("GET", "/repos/owner/repo/issues/1")
    headers = api.requests[-1].headers
    assert headers["Authorization"] == f"Bearer {TOKEN}"
    assert headers["X-GitHub-Api-Version"] == API_VERSION
    assert headers["Accept"] == "application/vnd.github+json"


def test_not_found_is_a_distinct_error() -> None:
    with pytest.raises(GitHubNotFoundError):
        GitHubClient(TOKEN, transport=FakeGitHub()).request("GET", "/repos/owner/repo/issues/9")


def test_errors_never_contain_the_token() -> None:
    def failing(request: HttpRequest) -> HttpResponse:
        return HttpResponse(500, {}, json.dumps({"message": "boom"}).encode())

    with pytest.raises(ProviderError) as info:
        GitHubClient(TOKEN, transport=failing).request("GET", "/repos/o/r/issues?x=1")
    assert TOKEN not in str(info.value)
    assert "HTTP 500" in str(info.value)
    assert "boom" in str(info.value)


def test_pagination_follows_link_headers() -> None:
    api = FakeGitHub(page_size=2)
    for number in range(1, 6):
        api.add_issue(number, labels={"x"})
    client = GitHubClient(TOKEN, transport=api)
    items = list(client.paginate("/repos/owner/repo/issues", query={"labels": "x"}))
    assert sorted(i["number"] for i in items) == [1, 2, 3, 4, 5]
    assert len(api.requests) == 3


def test_pagination_refuses_to_follow_links_to_another_host() -> None:
    sent: list[str] = []

    def hostile(request: HttpRequest) -> HttpResponse:
        sent.append(request.url)
        return HttpResponse(200, {"Link": '<https://evil.example/steal>; rel="next"'}, b"[]")

    client = GitHubClient(TOKEN, transport=hostile)
    with pytest.raises(ProviderError, match="another host"):
        list(client.paginate("/repos/o/r/issues"))
    assert all("evil.example" not in url for url in sent)


@pytest.mark.parametrize("url", ["http://api.github.com", "ftp://x", "api.github.com"])
def test_only_https_api_urls(url: str) -> None:
    with pytest.raises(ValueError, match="https"):
        GitHubClient(TOKEN, api_url=url)


def test_token_is_required() -> None:
    with pytest.raises(ValueError, match="token"):
        GitHubClient("")
