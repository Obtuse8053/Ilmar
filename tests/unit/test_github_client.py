# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import http.server
import json
import threading
from collections.abc import Iterator

import pytest

from ilmar.adapters.github.client import (
    API_VERSION,
    CrossOriginRedirectError,
    GitHubClient,
    GitHubNotFoundError,
    HttpRequest,
    HttpResponse,
    _origin,
    urllib_transport,
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


# -- redirects through the real transport (PR #38 review) -----------------


class _Server:
    def __init__(self, handler: type[http.server.BaseHTTPRequestHandler]) -> None:
        self.httpd = http.server.HTTPServer(("127.0.0.1", 0), handler)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.httpd.server_port}"

    def close(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()


@pytest.fixture
def no_proxy(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "HTTPS_PROXY",
        "https_proxy",
        "HTTP_PROXY",
        "http_proxy",
        "ALL_PROXY",
        "all_proxy",
    ):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def servers() -> Iterator[tuple[_Server, _Server, list[dict[str, str | None]]]]:
    seen: list[dict[str, str | None]] = []

    class Other(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            seen.append({"path": self.path, "auth": self.headers.get("Authorization")})
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"[]")

        def log_message(self, *args: object) -> None:
            pass

    other = _Server(Other)

    class Api(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            if self.path == "/cross":
                target = f"{other.url}/steal"
            elif self.path == "/same":
                target = "/final"
            else:
                seen.append({"path": self.path, "auth": self.headers.get("Authorization")})
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"[]")
                return
            self.send_response(302)
            self.send_header("Location", target)
            self.end_headers()

        def log_message(self, *args: object) -> None:
            pass

    api = _Server(Api)
    yield api, other, seen
    api.close()
    other.close()


@pytest.mark.usefixtures("no_proxy")
def test_real_transport_refuses_cross_origin_redirects(
    servers: tuple[_Server, _Server, list[dict[str, str | None]]],
) -> None:
    api, _, seen = servers
    request = HttpRequest("GET", f"{api.url}/cross", {"Authorization": f"Bearer {TOKEN}"})
    with pytest.raises(CrossOriginRedirectError):
        urllib_transport(request)
    assert seen == []  # the other origin never received a request, let alone the token


@pytest.mark.usefixtures("no_proxy")
def test_real_transport_follows_same_origin_redirects_with_credentials(
    servers: tuple[_Server, _Server, list[dict[str, str | None]]],
) -> None:
    api, _, seen = servers
    response = urllib_transport(
        HttpRequest("GET", f"{api.url}/same", {"Authorization": f"Bearer {TOKEN}"})
    )
    assert response.status == 200
    assert seen == [{"path": "/final", "auth": f"Bearer {TOKEN}"}]


def test_a_different_port_is_a_different_origin() -> None:
    assert _origin("https://api.github.com/x") == _origin("https://API.github.com/y")
    assert _origin("https://api.github.com/x") != _origin("https://api.github.com:8443/x")
    assert _origin("https://api.github.com/x") != _origin("http://api.github.com/x")
