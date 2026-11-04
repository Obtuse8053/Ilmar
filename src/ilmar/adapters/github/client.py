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

from ilmar.domain.errors import ProviderError

API_VERSION = "2022-11-28"
DEFAULT_API_URL = "https://api.github.com"
_NEXT_LINK = re.compile(r'<([^>]+)>\s*;\s*rel="next"')


@dataclass(frozen=True, slots=True)
class HttpRequest:
    method: str
    url: str
    headers: Mapping[str, str]
    body: bytes | None = None


@dataclass(frozen=True, slots=True)
class HttpResponse:
    status: int
    headers: Mapping[str, str] = field(default_factory=dict)
    body: bytes = b""


Transport = Callable[[HttpRequest], HttpResponse]


def urllib_transport(request: HttpRequest, *, timeout: float = 30.0) -> HttpResponse:
    """Real transport. Honours ``HTTPS_PROXY`` through ``urllib``'s defaults."""
    raw = urllib.request.Request(  # noqa: S310 - the scheme is checked by GitHubClient
        request.url, data=request.body, headers=dict(request.headers), method=request.method
    )
    try:
        with urllib.request.urlopen(raw, timeout=timeout) as response:  # noqa: S310
            return HttpResponse(response.status, dict(response.headers), response.read())
    except urllib.error.HTTPError as error:
        return HttpResponse(error.code, dict(error.headers or {}), error.read() or b"")
    except (urllib.error.URLError, TimeoutError) as error:
        raise ProviderError(f"GitHub request failed: {error.__class__.__name__}") from None


class GitHubNotFoundError(ProviderError):
    """The resource does not exist (HTTP 404)."""


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
    ) -> Any:
        url = self._base + path
        if query:
            url += "?" + urllib.parse.urlencode(query)
        response = self._send(method, url, body)
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

    def _send(self, method: str, url: str, body: Any) -> HttpResponse:
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
        response = self._transport(HttpRequest(method, url, headers, data))
        if response.status == 404:
            raise GitHubNotFoundError(f"GitHub {method} {_path_of(url)}: not found")
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
