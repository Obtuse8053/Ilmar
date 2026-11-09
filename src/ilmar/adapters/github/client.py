# SPDX-License-Identifier: Apache-2.0
"""A minimal GitHub REST client on the standard library (ADR-0003).

REST only: GraphQL is not used, because it is unavailable in some
environments Ilmar runs in. The transport is injectable, so tests run offline
against a fake API. The token is sent only to the configured API host and
never appears in error messages.
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, field
from typing import Any

from ilmar.domain.errors import PolicyViolationError, ProviderError

API_VERSION = "2022-11-28"
DEFAULT_API_URL = "https://api.github.com"
_NEXT_LINK = re.compile(r'<([^>]+)>\s*;\s*rel="next"')


@dataclass(frozen=True, slots=True)
class HttpRequest:
    method: str
    url: str
    headers: Mapping[str, str]
    body: bytes | None = None
    follow_redirects: bool = True
    """``False``: a redirect comes back as the 3xx response, never followed
    (ADR-0026 §1: a transferred issue is never read in another repository)."""


@dataclass(frozen=True, slots=True)
class HttpResponse:
    status: int
    headers: Mapping[str, str] = field(default_factory=dict)
    body: bytes = b""


Transport = Callable[[HttpRequest], HttpResponse]


class CrossOriginRedirectError(ProviderError, PolicyViolationError):
    """A redirect pointed to a different origin; it was refused, credentials and all.

    Also a ``PolicyViolationError``, so the coordinator never treats it as a
    transient failure to retry (review of 46c1662).
    """


def _origin(url: str) -> tuple[str, str, int | None]:
    parts = urllib.parse.urlsplit(url)
    return parts.scheme.lower(), (parts.hostname or "").lower(), parts.port


class _SameOriginRedirects(urllib.request.HTTPRedirectHandler):
    """Follow redirects only within the same origin (scheme, host and port).

    ``urllib`` copies request headers, including ``Authorization``, onto a
    redirected request. A redirect to any other origin is refused, so the token
    can never be forwarded.
    """

    max_redirections = 3

    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: Any,
        code: int,
        msg: str,
        headers: Any,
        newurl: str,
    ) -> urllib.request.Request | None:
        if _origin(newurl) != _origin(req.full_url):
            raise CrossOriginRedirectError("GitHub redirected to another origin; refusing")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class _NoRedirects(urllib.request.HTTPRedirectHandler):
    """Never follow a redirect: the 3xx is returned to the caller as it is."""

    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: Any,
        code: int,
        msg: str,
        headers: Any,
        newurl: str,
    ) -> urllib.request.Request | None:
        return None


def urllib_transport(request: HttpRequest, *, timeout: float = 30.0) -> HttpResponse:
    """Real transport. Honours ``HTTPS_PROXY``. Follows same-origin redirects only,
    and none at all when the request says so."""
    handler = _SameOriginRedirects() if request.follow_redirects else _NoRedirects()
    opener = urllib.request.build_opener(handler)
    raw = urllib.request.Request(  # noqa: S310 - the scheme is checked by GitHubClient
        request.url, data=request.body, headers=dict(request.headers), method=request.method
    )
    try:
        with opener.open(raw, timeout=timeout) as response:
            return HttpResponse(response.status, dict(response.headers), response.read())
    except urllib.error.HTTPError as error:
        return HttpResponse(error.code, dict(error.headers or {}), error.read() or b"")
    except (urllib.error.URLError, TimeoutError) as error:
        raise ProviderError(f"GitHub request failed: {error.__class__.__name__}") from None


class GitHubNotFoundError(ProviderError):
    """The resource does not exist (HTTP 404, or 410 for a deleted one)."""


class GitHubMovedError(ProviderError):
    """The resource answered with a redirect that was not followed (HTTP 3xx)."""


class GitHubClient:
    def __init__(
        self,
        token: str,
        *,
        transport: Transport = urllib_transport,
        api_url: str = DEFAULT_API_URL,
    ) -> None:
        parsed = urllib.parse.urlsplit(api_url)
        if parsed.scheme != "https" or not parsed.hostname:
            raise ValueError("the GitHub API URL must be https")
        if not token:
            raise ValueError("a GitHub token is required")
        self._token = token
        self._transport = transport
        self._base = api_url.rstrip("/")
        self._host = parsed.hostname

    def request(
        self,
        method: str,
        path: str,
        *,
        query: Mapping[str, str] | None = None,
        body: Any = None,
        follow_redirects: bool = True,
    ) -> Any:
        url = self._base + path
        if query:
            url += "?" + urllib.parse.urlencode(query)
        response = self._send(method, url, body, follow_redirects=follow_redirects)
        return json.loads(response.body) if response.body else None

    def paginate(self, path: str, *, query: Mapping[str, str] | None = None) -> Iterator[Any]:
        """Yield items from every page, following ``Link: rel="next"`` on the same host."""
        url: str | None = self._base + path + ("?" + urllib.parse.urlencode(query) if query else "")
        while url is not None:
            response = self._send("GET", url, None)
            page = json.loads(response.body) if response.body else []
            if not isinstance(page, list):
                raise ProviderError(f"GitHub returned a non-list page for {path}")
            yield from page
            url = _next_link(response.headers)
            if url is not None and urllib.parse.urlsplit(url).hostname != self._host:
                raise ProviderError("GitHub pagination pointed to another host; refusing")

    def _send(
        self, method: str, url: str, body: Any, *, follow_redirects: bool = True
    ) -> HttpResponse:
        headers = {
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {self._token}",
            "X-GitHub-Api-Version": API_VERSION,
            "User-Agent": "ilmar",
        }
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        response = self._transport(HttpRequest(method, url, headers, data, follow_redirects))
        if response.status in (404, 410):
            raise GitHubNotFoundError(f"GitHub {method} {_path_of(url)}: not found")
        if 300 <= response.status < 400:
            raise GitHubMovedError(f"GitHub {method} {_path_of(url)}: moved (not followed)")
        if not 200 <= response.status < 300:
            raise ProviderError(
                f"GitHub {method} {_path_of(url)}: HTTP {response.status} {_message(response)}"
            )
        return response


def _next_link(headers: Mapping[str, str]) -> str | None:
    link = next((v for k, v in headers.items() if k.lower() == "link"), None)
    match = _NEXT_LINK.search(link) if link else None
    return match.group(1) if match else None


def _path_of(url: str) -> str:
    return urllib.parse.urlsplit(url).path


def _message(response: HttpResponse) -> str:
    try:
        data = json.loads(response.body)
    except (ValueError, UnicodeDecodeError):
        return ""
    message = data.get("message") if isinstance(data, dict) else None
    return str(message)[:200] if message else ""
