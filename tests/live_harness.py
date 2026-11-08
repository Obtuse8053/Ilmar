# SPDX-License-Identifier: Apache-2.0
"""Harness for the opt-in live end-to-end tests (#11d) and the #44 record.

Everything here is pure or takes its I/O as a parameter, so the offline suite
(`tests/unit/test_live_harness.py`) tests it without GitHub, Podman or a model.

Credentials:
* are read from the environment only, wrapped in :class:`Secret` (whose
  ``repr`` and ``str`` are redacted), and handed to subprocesses only through
  their environment;
* are never printed, logged, written or used in an assertion message. Leak
  findings name *where* a secret was found and *which* one (by its environment
  variable), never its value.
"""

from __future__ import annotations

import base64
import contextlib
import json
import os
import re
import shutil
import subprocess
import threading
import time
import urllib.parse
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Protocol

from ilmar.adapters.github.client import HttpRequest, HttpResponse, Transport
from ilmar.adapters.podman.runtime import PODMAN_ENVIRONMENT

REPOSITORY_NAME = "ilmar-disposable"
REPOSITORY_TOPIC = "ilmar-disposable"
MAX_TOKEN_LIFETIME = timedelta(days=7)
ANTHROPIC_HOST = "api.anthropic.com"
API = "https://api.github.com"
GITHUB_TOKEN_ENV = "ILMAR_LIVE_GITHUB_TOKEN"
ANTHROPIC_KEY_ENV = "ANTHROPIC_API_KEY"
PROBE_WORKFLOW = ".github/workflows/ilmar-permission-probe.yml"
PROBE_FILE = "ilmar-permission-probe.txt"


class LiveGuardError(AssertionError):
    """A precondition for touching real services does not hold: nothing was run."""


# -- secrets -----------------------------------------------------------------------------------


class Secret:
    """A credential value that never shows itself."""

    __slots__ = ("_value", "env")

    def __init__(self, env: str, value: str) -> None:
        if len(value) < 20:
            raise LiveGuardError(f"{env} is too short to be a real credential")
        self.env = env
        self._value = value

    def reveal(self) -> str:
        return self._value

    def forms(self) -> tuple[bytes, ...]:
        """Every encoding in which this value could leak.

        Raw, base64, git Basic auth and URL-encoded, plus 20-character fragments
        of the secret part (after a well-known prefix such as ``github_pat_`` or
        ``sk-ant-api03-``): tools record keys by such fragments (Claude Code
        remembers approved keys by their last 20 characters).
        """
        raw = self._value.encode()
        forms = {
            raw,
            base64.b64encode(raw),
            base64.b64encode(b"x-access-token:" + raw),
            urllib.parse.quote(self._value, safe="").encode(),
        }
        body = raw[len(_known_prefix(self._value)) :]
        if len(body) >= 40:
            forms |= {body[:20], body[len(body) // 2 - 10 : len(body) // 2 + 10], body[-20:]}
        return tuple(sorted(forms))

    def __repr__(self) -> str:
        return f"Secret({self.env}=<redacted>)"

    __str__ = __repr__


_KNOWN_PREFIXES = ("github_pat_", "sk-ant-api03-", "sk-ant-admin01-", "sk-ant-", "ghp_")


def _known_prefix(value: str) -> str:
    return next((p for p in _KNOWN_PREFIXES if value.startswith(p)), "")


def redact(text: str, secrets: Iterable[Secret]) -> str:
    """``text`` with every form of every secret replaced by its variable name."""
    forms = [(form.decode(), s.env) for s in secrets for form in s.forms()]
    for form, env in sorted(forms, key=lambda f: -len(f[0])):  # longest first: no partial left
        text = text.replace(form, f"<{env}>")
    return text


def find_secrets(data: bytes, secrets: Iterable[Secret]) -> list[str]:
    """The variable names of the secrets that occur in ``data``, in any form."""
    return sorted({s.env for s in secrets if any(form in data for form in s.forms())})


def scan_tree(root: Path, secrets: Sequence[Secret]) -> dict[str, list[str]]:
    """``{relative path: [secret names]}`` for every file under ``root`` holding a secret.

    Reads raw bytes (SQLite databases, git objects are compressed: those are
    checked separately through ``git`` where it matters). Symlinks are not
    followed.
    """
    found: dict[str, list[str]] = {}
    if not root.exists():
        return found
    for path in sorted(root.rglob("*")):
        if path.is_symlink() or not path.is_file():
            continue
        try:
            data = path.read_bytes()
        except OSError:
            continue
        if hits := find_secrets(data, secrets):
            found[str(path.relative_to(root))] = hits
    return found


def secret_spool_files(directory: Path, secrets: Sequence[Secret]) -> dict[str, list[str]]:
    """Files directly in a temporary directory that hold a secret (e.g. a stale env file)."""
    found: dict[str, list[str]] = {}
    for path in sorted(directory.glob("*")) if directory.is_dir() else []:
        if path.is_symlink() or not path.is_file():
            continue
        try:
            data = path.read_bytes()
        except OSError:  # another user's file
            continue
        if hits := find_secrets(data, secrets):
            found[path.name] = hits
    return found


@dataclass(frozen=True)
class LiveSettings:
    repository: str
    github_token: Secret
    anthropic_key: Secret
    claude_image: str
    claude_version: str
    proxy_image: str
    verify_image: str
    outside_repository: str
    report_dir: Path

    @property
    def secrets(self) -> tuple[Secret, Secret]:
        return (self.github_token, self.anthropic_key)


LIVE_VARIABLES = (
    "ILMAR_LIVE_REPO",
    GITHUB_TOKEN_ENV,
    ANTHROPIC_KEY_ENV,
    "ILMAR_TEST_CLAUDE_IMAGE",
    "ILMAR_TEST_CLAUDE_VERSION",
    "ILMAR_TEST_PODMAN_PROXY_IMAGE",
    "ILMAR_LIVE_OUTSIDE_REPO",
)


def missing_variables(env: Mapping[str, str]) -> list[str]:
    return [name for name in LIVE_VARIABLES if not env.get(name)]


def settings_from(env: Mapping[str, str], *, default_report: Path) -> LiveSettings:
    """Read and check the live settings. Raises :class:`LiveGuardError`, never leaks values."""
    if missing := missing_variables(env):
        raise LiveGuardError(f"not set: {', '.join(missing)}")
    repository = env["ILMAR_LIVE_REPO"]
    owner, _, name = repository.partition("/")
    if not owner or name != REPOSITORY_NAME:
        raise LiveGuardError(f"ILMAR_LIVE_REPO must be <owner>/{REPOSITORY_NAME}")
    outside = env["ILMAR_LIVE_OUTSIDE_REPO"]
    if outside.lower() == repository.lower() or "/" not in outside:
        raise LiveGuardError("ILMAR_LIVE_OUTSIDE_REPO must be another <owner>/<repo>")
    token = Secret(GITHUB_TOKEN_ENV, env[GITHUB_TOKEN_ENV])
    if not token.reveal().startswith("github_pat_"):
        raise LiveGuardError(f"{GITHUB_TOKEN_ENV} must be a fine-grained token (github_pat_…)")
    key = Secret(ANTHROPIC_KEY_ENV, env[ANTHROPIC_KEY_ENV])
    if key.reveal() == token.reveal():
        raise LiveGuardError("the two credentials must differ")
    version = env["ILMAR_TEST_CLAUDE_VERSION"]
    if not re.fullmatch(r"\d+\.\d+\.\d+", version):
        raise LiveGuardError("ILMAR_TEST_CLAUDE_VERSION must be a pinned x.y.z version")
    proxy = env["ILMAR_TEST_PODMAN_PROXY_IMAGE"]
    return LiveSettings(
        repository=repository,
        github_token=token,
        anthropic_key=key,
        claude_image=env["ILMAR_TEST_CLAUDE_IMAGE"],
        claude_version=version,
        proxy_image=proxy,
        verify_image=env.get("ILMAR_LIVE_VERIFY_IMAGE") or proxy,
        outside_repository=outside,
        report_dir=Path(env.get("ILMAR_LIVE_REPORT_DIR") or default_report),
    )


# -- GitHub (raw responses: the guard needs statuses and headers) ------------------------------


@dataclass(frozen=True)
class Reply:
    status: int
    headers: Mapping[str, str]
    data: Any

    def header(self, name: str) -> str:
        wanted = name.lower()
        return next((v for k, v in self.headers.items() if k.lower() == wanted), "")


class LiveGitHub:
    """A minimal REST client for setting up and checking live runs.

    The token goes only into the ``Authorization`` header. Errors carry the
    status and the API's message, never the request headers.
    """

    def __init__(self, token: Secret, transport: Transport) -> None:
        self._token = token
        self._transport = transport

    def call(self, method: str, path: str, body: Any = None) -> Reply:
        response: HttpResponse = self._transport(
            HttpRequest(
                method=method,
                url=API + path,
                headers={
                    "Accept": "application/vnd.github+json",
                    "Authorization": f"Bearer {self._token.reveal()}",
                    "X-GitHub-Api-Version": "2022-11-28",
                    "User-Agent": "ilmar-live-test",
                    **({"Content-Type": "application/json"} if body is not None else {}),
                },
                body=json.dumps(body).encode() if body is not None else None,
            )
        )
        data = json.loads(response.body) if response.body else None
        return Reply(response.status, response.headers, data)

    def ok(self, method: str, path: str, body: Any = None) -> Any:
        reply = self.call(method, path, body)
        if not 200 <= reply.status < 300:
            message = reply.data.get("message", "") if isinstance(reply.data, dict) else ""
            raise AssertionError(f"GitHub {method} {path}: HTTP {reply.status} {message}")
        return reply.data


def token_expiry(header: str) -> datetime | None:
    """Parse ``GitHub-Authentication-Token-Expiration`` (``2026-11-12 10:00:00 UTC``)."""
    if not header:
        return None
    text = header.strip().replace(" UTC", " +0000")
    for pattern in ("%Y-%m-%d %H:%M:%S %z", "%Y-%m-%dT%H:%M:%S%z"):
        with contextlib.suppress(ValueError):
            return datetime.strptime(text, pattern).astimezone(UTC)
    return None


@dataclass
class Preflight:
    """What the guard established; safe to print and to record (no values)."""

    repository: str
    default_branch: str = ""
    token_expires: str = ""
    checks: list[str] = field(default_factory=list)


def preflight(github: LiveGitHub, settings: LiveSettings, *, now: datetime) -> Preflight:
    """Refuse to run unless the repository and the token are what #11d requires.

    * the repository is ``<owner>/ilmar-disposable``, private, with the topic;
    * the token is fine-grained, expires within seven days, cannot see the
      outside repository, and has no Administration, Actions or Workflows
      permission. Workflows is write-only, so it is probed with one write to
      a throwaway branch of the disposable repository, removed right after.
    """
    result = Preflight(settings.repository)
    reply = github.call("GET", f"/repos/{settings.repository}")
    if reply.status != 200 or not isinstance(reply.data, dict):
        raise LiveGuardError(f"{settings.repository} is not reachable (HTTP {reply.status})")
    repo = reply.data
    if repo.get("name") != REPOSITORY_NAME:
        raise LiveGuardError(f"the repository is not named {REPOSITORY_NAME}")
    if str(repo.get("full_name", "")).lower() != settings.repository.lower():
        # GitHub follows renames and transfers: refuse a repository reached by redirect.
        raise LiveGuardError(f"{settings.repository} redirects to another repository")
    if repo.get("private") is not True:
        raise LiveGuardError("the disposable repository must be private")
    if REPOSITORY_TOPIC not in (repo.get("topics") or []):
        raise LiveGuardError(f"the repository lacks the {REPOSITORY_TOPIC!r} topic")
    if repo.get("archived") or not repo.get("default_branch"):
        raise LiveGuardError("the repository must be unarchived and have a default branch")
    result.default_branch = str(repo["default_branch"])
    result.checks.append("repository: named, private, topic present")

    if reply.header("X-OAuth-Scopes"):
        raise LiveGuardError("a classic token was used; a fine-grained one is required")
    expires = token_expiry(reply.header("GitHub-Authentication-Token-Expiration"))
    if expires is None:
        raise LiveGuardError("the token has no expiry; it must expire within 7 days")
    if not now < expires <= now + MAX_TOKEN_LIFETIME:
        raise LiveGuardError(f"the token expires {expires:%Y-%m-%d %H:%M} UTC, not within 7 days")
    result.token_expires = f"{expires:%Y-%m-%d %H:%M} UTC"
    result.checks.append("token: fine-grained, expires within 7 days")

    outside = github.call("GET", f"/repos/{settings.outside_repository}")
    if outside.status != 404:
        raise LiveGuardError(
            f"the token can see {settings.outside_repository} (HTTP {outside.status}): it "
            "must be limited to the disposable repository, and the outside repository must "
            "be private"
        )
    result.checks.append("token: cannot see the outside repository")

    for permission, path in (
        ("Administration", f"/repos/{settings.repository}/actions/permissions"),
        ("Actions", f"/repos/{settings.repository}/actions/runs?per_page=1"),
        ("Secrets", f"/repos/{settings.repository}/actions/secrets?per_page=1"),
        ("Webhooks", f"/repos/{settings.repository}/hooks?per_page=1"),
    ):
        refuse_if_granted(github.call("GET", path), permission)
        result.checks.append(f"token: no {permission} permission")

    for permission, path in (  # what the run needs, read side: too little must also stop it
        ("Issues", f"/repos/{settings.repository}/issues?per_page=1"),
        ("Pull requests", f"/repos/{settings.repository}/pulls?per_page=1"),
    ):
        status = github.call("GET", path).status
        if status != 200:
            raise LiveGuardError(f"the token lacks {permission} permission (HTTP {status})")
        result.checks.append(f"token: has {permission} permission")

    probe_write_permissions(github, settings.repository, result.default_branch, now=now)
    result.checks.append("token: can write contents and pull requests")
    result.checks.append("token: cannot write workflows")
    probe_issue_permissions(github, settings.repository, now=now)
    result.checks.append("token: can write issues, labels and comments")
    return result


def probe_issue_permissions(github: LiveGitHub, repository: str, *, now: datetime) -> None:
    """Prove Issues write: everything seeding a run and claiming its task does.

    Pull requests write alone also allows the label calls, so the probe starts
    with creating an issue, which needs Issues write. It then creates a label,
    adds it (checking GitHub kept it) and comments, as Ilmar's claim does. The
    probe issue is closed and its label deleted afterwards, whatever happens.
    Every refusal names Issues write.
    """
    label = f"ilmar-live:probe-{now:%Y%m%d%H%M%S}"
    created = github.call("POST", f"/repos/{repository}/issues", {
        "title": "ilmar permission probe (closed at once)",
        "body": "Created and closed by Ilmar's live-test preflight.",
    })  # fmt: skip
    if created.status != 201 or not isinstance(created.data, dict):
        raise LiveGuardError(
            f"the token cannot create issues (HTTP {created.status}): it needs Issues write"
        )
    number = int(created.data["number"])
    try:
        steps: tuple[tuple[str, str, str, Any, int], ...] = (
            ("create labels", "POST", f"/repos/{repository}/labels",
             {"name": label, "color": "ededed"}, 201),
            ("add labels to issues", "POST", f"/repos/{repository}/issues/{number}/labels",
             {"labels": [label]}, 200),
            ("comment on issues", "POST", f"/repos/{repository}/issues/{number}/comments",
             {"body": "ilmar permission probe"}, 201),
        )  # fmt: skip
        for what, method, path, body, expected in steps:
            reply = github.call(method, path, body)
            if reply.status != expected:
                raise LiveGuardError(
                    f"the token cannot {what} (HTTP {reply.status}): it needs Issues write"
                )
            if what == "add labels to issues" and not (
                isinstance(reply.data, list)
                and any(isinstance(i, dict) and i.get("name") == label for i in reply.data)
            ):
                raise LiveGuardError("GitHub did not keep the label on the probe issue")
    finally:
        github.call("PATCH", f"/repos/{repository}/issues/{number}", {"state": "closed"})
        github.call("DELETE", f"/repos/{repository}/labels/{urllib.parse.quote(label, safe='')}")


def refuse_if_granted(reply: Reply, permission: str) -> None:
    """403/404 means "not granted"; success means granted; anything else is not an answer."""
    if 200 <= reply.status < 300:
        raise LiveGuardError(f"the token has {permission} permission; it must not")
    if reply.status not in (403, 404):
        raise LiveGuardError(f"{permission} permission could not be checked (HTTP {reply.status})")


def probe_write_permissions(
    github: LiveGitHub, repository: str, base: str, *, now: datetime
) -> None:
    """Prove the writes the run needs work, then that a workflow write is refused.

    On a throwaway branch, deleted afterwards whatever happens:
    1. create the branch, then a harmless file: Contents write works, so a
       refused workflow write below cannot be a missing Contents permission;
    2. open a pull request from it and close it again: Pull requests write;
    3. try to create a workflow file: it must be refused with 403 or 404.
       Any other failure (422, 5xx) is not an answer and stops the run.
    """
    head = github.ok("GET", f"/repos/{repository}/git/ref/heads/{base}")["object"]["sha"]
    branch = f"ilmar-live/probe-{now:%Y%m%d%H%M%S}"
    github.ok("POST", f"/repos/{repository}/git/refs", {"ref": f"refs/heads/{branch}", "sha": head})
    try:
        control = github.call("PUT", f"/repos/{repository}/contents/{PROBE_FILE}", {
            "message": "ilmar permission probe (control)", "branch": branch,
            "content": base64.b64encode(b"probe\n").decode(),
        })  # fmt: skip
        if control.status != 201:
            raise LiveGuardError(f"the token cannot write contents (HTTP {control.status})")
        request = github.call("POST", f"/repos/{repository}/pulls", {
            "title": "ilmar permission probe (closed at once)", "head": branch, "base": base,
            "body": "Created and closed by Ilmar's live-test guard.",
        })  # fmt: skip
        if request.status != 201 or not isinstance(request.data, dict):
            raise LiveGuardError(f"the token cannot open pull requests (HTTP {request.status})")
        github.call("PATCH", f"/repos/{repository}/pulls/{request.data['number']}",
                    {"state": "closed"})  # fmt: skip
        workflow = github.call("PUT", f"/repos/{repository}/contents/{PROBE_WORKFLOW}", {
            "message": "ilmar permission probe (must be refused)", "branch": branch,
            "content": base64.b64encode(b"name: probe\non: []\njobs: {}\n").decode(),
        })  # fmt: skip
    finally:
        github.call("DELETE", f"/repos/{repository}/git/refs/heads/{branch}")
    refuse_if_granted(workflow, "Workflows")


# -- the disposable project --------------------------------------------------------------------

SEED_FILES = {
    "README.md": "A disposable project for Ilmar's live tests. Safe to delete.\n",
    "calc.py": "def add(a: int, b: int) -> int:\n    return a - b\n",
    "test_calc.py": (
        "import unittest\n\nfrom calc import add\n\n\n"
        "class AddTest(unittest.TestCase):\n"
        "    def test_adds(self) -> None:\n"
        "        self.assertEqual(add(2, 3), 5)\n\n\n"
        'if __name__ == "__main__":\n    unittest.main()\n'
    ),
}
ISSUE_TITLE = "add() subtracts instead of adding"
ISSUE_BODY = (
    "`add(2, 3)` in `calc.py` returns -1. Fix `add` so that `test_calc.py` passes. "
    "Change only `calc.py`.\n\n_Created by Ilmar's live test; safe to close._"
)
EXPECTED_PATHS = frozenset({"calc.py"})


@dataclass(frozen=True)
class LiveRun:
    run_id: str
    base_branch: str
    label: str
    issue: int


# How long the seeded issue may take to appear in the coordinator's own query.
# Not a run timeout: a precondition. If GitHub has not listed the issue by then,
# the run stops here, saying so, instead of the coordinator finding no task.
SEED_LISTING_TIMEOUT = 60.0


def eligible_issues_path(repository: str, label: str) -> str:
    """Exactly the listing GitHubTaskProvider.list_eligible asks GitHub for."""
    query = urllib.parse.urlencode({"state": "open", "labels": label, "per_page": "100"})
    return f"/repos/{repository}/issues?{query}"


def issue_is_listed(github: LiveGitHub, repository: str, label: str, issue: int) -> bool:
    reply = github.call("GET", eligible_issues_path(repository, label))
    return (
        reply.status == 200
        and isinstance(reply.data, list)
        and any(isinstance(i, dict) and i.get("number") == issue for i in reply.data)
    )


def seed_run(
    github: LiveGitHub, repository: str, default_branch: str, run_id: str, *,
    clock: Callable[[], float] = time.monotonic, sleep: Callable[[float], None] = time.sleep,
) -> LiveRun:  # fmt: skip
    """A fresh base branch holding only the seed project, and one issue for it.

    Nothing existing is rewritten: the branch is new and its commit's parent is
    the default branch's head.

    The issue is ready only once the coordinator can see it: the run's label is
    created and put on the issue through endpoints that fail loudly (GitHub
    silently drops labels passed when an issue is created, if the caller may
    not set them), and the issue must appear in the coordinator's own listing.
    Otherwise this stops, says which, and removes what it made.
    """
    if not re.fullmatch(r"[a-z0-9-]{6,40}", run_id):
        raise ValueError("run id: lowercase letters, digits and dashes")
    head = github.ok("GET", f"/repos/{repository}/git/ref/heads/{default_branch}")["object"]["sha"]
    tree = github.ok(
        "POST",
        f"/repos/{repository}/git/trees",
        {
            "tree": [
                {"path": path, "mode": "100644", "type": "blob", "content": content}
                for path, content in sorted(SEED_FILES.items())
            ]
        },
    )["sha"]
    commit = github.ok(
        "POST",
        f"/repos/{repository}/git/commits",
        {"message": f"Seed for live run {run_id}", "tree": tree, "parents": [head]},
    )["sha"]
    base = f"ilmar-live/{run_id}"
    github.ok("POST", f"/repos/{repository}/git/refs", {"ref": f"refs/heads/{base}", "sha": commit})
    label = f"ilmar-live:{run_id}"
    run = LiveRun(run_id, base, label, 0)
    try:
        made = github.call("POST", f"/repos/{repository}/labels", {
            "name": label, "color": "c5def5", "description": "Ilmar live test run; safe to delete",
        })  # fmt: skip
        if made.status != 201:
            raise LiveGuardError(f"the run's label could not be created (HTTP {made.status})")
        created = github.ok("POST", f"/repos/{repository}/issues",
                            {"title": f"{ISSUE_TITLE} ({run_id})", "body": ISSUE_BODY})  # fmt: skip
        run = LiveRun(run_id, base, label, int(created["number"]))
        labelled = github.call("POST", f"/repos/{repository}/issues/{run.issue}/labels",
                               {"labels": [label]})  # fmt: skip
        names = (
            {str(item.get("name")) for item in labelled.data if isinstance(item, dict)}
            if labelled.status == 200 and isinstance(labelled.data, list)
            else set()
        )
        if label not in names:
            raise LiveGuardError(
                f"GitHub did not put the run's label on the seeded issue (HTTP "
                f"{labelled.status}); the token needs Issues write"
            )
        listed = wait_for(
            lambda: issue_is_listed(github, repository, label, run.issue),
            timeout=SEED_LISTING_TIMEOUT, interval=1.0, clock=clock, sleep=sleep,
        )  # fmt: skip
        if not listed:
            raise LiveGuardError(
                f"GitHub did not list the seeded issue under its label within "
                f"{SEED_LISTING_TIMEOUT:.0f} s, so the coordinator would find no task"
            )
    except BaseException:
        cleanup_run(github, repository, run)  # best effort; the original error wins
        raise
    return run


def change_requests_for(github: LiveGitHub, repository: str, base: str) -> list[dict[str, Any]]:
    """Every pull request (any state) into the run's base branch."""
    query = urllib.parse.urlencode({"state": "all", "base": base, "per_page": "100"})
    data = github.ok("GET", f"/repos/{repository}/pulls?{query}")
    return [p for p in data if isinstance(p, dict)]


def cleanup_run(github: LiveGitHub, repository: str, run: LiveRun) -> list[str]:
    """Close what the run opened and delete its branches; best effort, returns what failed."""
    problems = []
    with contextlib.suppress(AssertionError):
        for request in change_requests_for(github, repository, run.base_branch):
            if request.get("state") == "open":
                github.call("PATCH", f"/repos/{repository}/pulls/{request['number']}",
                            {"state": "closed"})  # fmt: skip
            head = request.get("head", {}).get("ref", "")
            if head.startswith("ilmar/"):
                github.call("DELETE", f"/repos/{repository}/git/refs/heads/{head}")
    for method, path, body in (
        ("PATCH", f"/repos/{repository}/issues/{run.issue}", {"state": "closed"}),
        ("DELETE", f"/repos/{repository}/git/refs/heads/{run.base_branch}", None),
        ("DELETE", f"/repos/{repository}/labels/{urllib.parse.quote(run.label, safe='')}", None),
    ):
        status = github.call(method, path, body).status
        if status >= 300 and status != 404:
            problems.append(f"{method} {path}: HTTP {status}")
    return problems


# -- configuration -----------------------------------------------------------------------------


def live_config(settings: LiveSettings, run: LiveRun, state_dir: Path) -> str:
    """The TOML for one live run: names of variables only, never their values."""

    def q(value: str) -> str:
        return json.dumps(value)

    text = f"""\
# Generated by Ilmar's live test for run {run.run_id}; contains no secrets.
[ilmar]
state_dir = {q(str(state_dir))}
instance_id = {q("live-" + run.run_id[:12])}

[limits]
max_attempts = 3
max_concurrent_executions = 1
agent_timeout_seconds = 900

[adapters.task_provider]
kind = "github"
options = {{ repository = {q(settings.repository)}, eligible_label = {q(run.label)}, \
claimed_label = "ilmar:claimed", token_env = {q(GITHUB_TOKEN_ENV)} }}

[adapters.source_control]
kind = "github"
options = {{ repository = {q(settings.repository)}, base_branch = {q(run.base_branch)}, \
token_env = {q(GITHUB_TOKEN_ENV)} }}

[adapters.container_runtime]
kind = "podman"
options = {{ proxy_image = {q(settings.proxy_image)} }}

[adapters.agent_runner]
kind = "claude-code"
options = {{ image = {q(settings.claude_image)}, api_key_env = {q(ANTHROPIC_KEY_ENV)}, \
expected_version = {q(settings.claude_version)}, max_turns = 30 }}

[adapters.execution_store]
kind = "sqlite"
options = {{ path = {q(str(state_dir / "executions.sqlite3"))} }}

[verification]
image = {q(settings.verify_image)}

[[verification.commands]]
name = "tests"
job = "tests"
argv = ["python3", "-m", "unittest", "-v"]
timeout_seconds = 300
"""
    if find_secrets(text.encode(), settings.secrets):
        raise LiveGuardError("a credential value reached the configuration")
    return text


def write_config(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w") as handle:
        handle.write(text)
    return path


def child_environment(settings: LiveSettings, base: Mapping[str, str]) -> dict[str, str]:
    """The CLI's environment: what Podman and git need, plus exactly the two credentials."""
    keep = ("PATH", "HOME", "LANG", "XDG_RUNTIME_DIR", "TMPDIR", "CONTAINERS_CONF")
    env = {k: base[k] for k in keep if k in base}
    env[GITHUB_TOKEN_ENV] = settings.github_token.reveal()
    env[ANTHROPIC_KEY_ENV] = settings.anthropic_key.reveal()
    return env


# -- watching the sandboxes while a run is in progress -----------------------------------------


class LogFollower(Protocol):
    def text(self) -> str: ...
    def stop(self) -> None: ...


class PodmanView(Protocol):
    def containers(self, instance: str) -> list[str]: ...
    def inspect(self, container_id: str) -> dict[str, Any] | None: ...
    def network_internal(self, network: str) -> bool | None: ...
    def follow_logs(self, container_id: str) -> LogFollower: ...


@dataclass
class SeenContainer:
    """What the watcher learned about one container: derived facts, never raw data."""

    name: str
    role: str
    allowed_hosts: tuple[str, ...] = ()
    network_mode: str = ""
    networks: tuple[str, ...] = ()
    secrets_in_definition: list[str] = field(default_factory=list)
    log: str = ""


def describe(inspected: Mapping[str, Any], secrets: Sequence[Secret]) -> SeenContainer:
    config = inspected.get("Config") or {}
    labels = config.get("Labels") or {}
    command = [str(a) for a in (config.get("Cmd") or [])]
    allowed: tuple[str, ...] = ()
    if "--allow" in command:
        index = command.index("--allow")
        if index + 1 < len(command):
            allowed = tuple(h for h in command[index + 1].split(",") if h)
    host = inspected.get("HostConfig") or {}
    attached = (inspected.get("NetworkSettings") or {}).get("Networks") or {}
    return SeenContainer(
        name=str(inspected.get("Name", "")).lstrip("/"),
        role=str(labels.get("io.ilmar.role", "")),
        allowed_hosts=allowed,
        network_mode=str(host.get("NetworkMode", "")),
        networks=tuple(sorted(attached)),
        secrets_in_definition=find_secrets(json.dumps(inspected).encode(), secrets),
    )


def parse_egress(log: str) -> tuple[list[str], list[str]]:
    """``(hosts allowed, denial lines)`` from an egress proxy's log."""
    allowed = sorted(set(re.findall(r"ilmar-egress ALLOW host=(\S+)", log)))
    denied = [line for line in log.splitlines() if "ilmar-egress DENY" in line]
    return allowed, denied


TRUSTED_WITH_TOKEN = ("python", "git")
"""Host programs that may hold the GitHub token in their environment: the
coordinator and the git it runs. Never podman, conmon, a runtime or a sandbox."""


def environments_with_secrets(
    secrets: Sequence[Secret], proc: Path = Path("/proc"), *, trusted: Sequence[str] = ()
) -> list[str]:
    """``<program>: <secret names>`` for readable processes whose environment holds a
    secret, except programs whose name starts with one of ``trusted``.

    Processes of other users (and most sandboxed ones, in their own user
    namespace) are unreadable and skipped: this is a best-effort net.
    """
    found = []
    for entry in proc.glob("[0-9]*"):
        try:
            environ = (entry / "environ").read_bytes()
            program = (entry / "comm").read_text().strip()
        except OSError:
            continue
        if program.startswith(tuple(trusted)):
            continue
        if hits := find_secrets(environ, secrets):
            found.append(f"{program}: {', '.join(hits)}")
    return sorted(found)


def processes_mentioning(needles: Iterable[str], proc: Path = Path("/proc")) -> list[str]:
    """Programs whose argv mentions one of ``needles`` (e.g. an orphaned ``podman exec``)."""
    wanted = [n.encode() for n in needles if n]
    found = []
    for entry in proc.glob("[0-9]*"):
        try:
            argv = (entry / "cmdline").read_bytes()
        except OSError:
            continue
        if any(n in argv for n in wanted):
            found.append(Path(argv.split(b"\0", 1)[0].decode(errors="replace")).name)
    return sorted(found)


def wipe_leaks(root: Path, secrets: Sequence[Secret]) -> list[str]:
    """Delete every file under ``root`` that holds a secret; return their relative paths."""
    leaked = scan_tree(root, secrets)
    for relative in leaked:
        with contextlib.suppress(OSError):
            (root / relative).unlink()
    return sorted(leaked)


def processes_with_secrets(secrets: Sequence[Secret], proc: Path = Path("/proc")) -> list[str]:
    """``<program>: <secret names>`` for every readable process whose argv holds a secret."""
    found = []
    for entry in proc.glob("[0-9]*"):
        try:
            argv = (entry / "cmdline").read_bytes()
        except OSError:
            continue
        if hits := find_secrets(argv, secrets):
            program = Path(argv.split(b"\0", 1)[0].decode(errors="replace")).name
            found.append(f"{program}: {', '.join(hits)}")
    return sorted(found)


class Watcher:
    """Polls Podman for the instance's containers during a run (a background thread).

    Each container is described once; egress proxies' logs are followed until
    they go away. Process argv is checked for secrets on every poll.
    """

    def __init__(
        self,
        view: PodmanView,
        instance: str,
        secrets: Sequence[Secret],
        *,
        interval: float = 0.2,
        proc: Path | None = Path("/proc"),
    ) -> None:
        self._view, self._instance, self._secrets = view, instance, secrets
        self._interval, self._proc = interval, proc
        self.seen: dict[str, SeenContainer] = {}
        self.argv_leaks: set[str] = set()
        self.environment_leaks: set[str] = set()
        self.internal_networks: dict[str, bool | None] = {}
        self._followers: dict[str, LogFollower] = {}
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self.errors: list[str] = []

    def poll(self) -> None:
        for container_id in self._view.containers(self._instance):
            if container_id in self.seen:
                continue
            inspected = self._view.inspect(container_id)
            if inspected is None:
                continue
            seen = describe(inspected, self._secrets)
            self.seen[container_id] = seen
            if seen.role == "egress-proxy":
                self._followers[container_id] = self._view.follow_logs(container_id)
            if seen.role == "sandbox":
                for network in seen.networks:
                    self.internal_networks.setdefault(network, self._view.network_internal(network))
        if self._proc is not None:
            self.argv_leaks.update(processes_with_secrets(self._secrets, self._proc))
            token = [s for s in self._secrets if s.env == GITHUB_TOKEN_ENV]
            self.environment_leaks.update(
                environments_with_secrets(token, self._proc, trusted=TRUSTED_WITH_TOKEN)
            )

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.poll()
            except Exception as error:
                self.errors.append(type(error).__name__)
            self._stop.wait(self._interval)

    def __enter__(self) -> Watcher:
        self._thread.start()
        return self

    def __exit__(self, *_: object) -> None:
        self.stop()

    def stop(self) -> None:
        """Stop polling and collect the followed logs (also when never started)."""
        self._stop.set()
        if self._thread.is_alive():
            self._thread.join(timeout=10)
        for container_id, follower in self._followers.items():
            follower.stop()
            self.seen[container_id].log = follower.text()

    def proxy_log_so_far(self) -> str:
        """Everything the egress proxies logged up to now (while the run goes on)."""
        return "".join(follower.text() for follower in list(self._followers.values()))

    def sandboxes(self) -> list[SeenContainer]:
        return [s for s in self.seen.values() if s.role == "sandbox"]

    def proxies(self) -> list[SeenContainer]:
        return [s for s in self.seen.values() if s.role == "egress-proxy"]


class SubprocessPodman:
    """The real view: ``podman ps``/``inspect``/``logs --follow``."""

    def __init__(self, podman: str | None = None) -> None:
        self._podman = podman or tool("podman")

    def _run(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(  # noqa: S603 - fixed argv
            [self._podman, *args], capture_output=True, text=True, check=False, timeout=30,
            env=host_environment(),
        )  # fmt: skip

    def containers(self, instance: str) -> list[str]:
        done = self._run("ps", "-a", "-q", "--no-trunc", "--filter",
                         f"label=io.ilmar.owner.instance={instance}")  # fmt: skip
        return done.stdout.split()

    def inspect(self, container_id: str) -> dict[str, Any] | None:
        done = self._run("container", "inspect", container_id)
        if done.returncode != 0:
            return None
        data = json.loads(done.stdout)
        return data[0] if data else None

    def network_internal(self, network: str) -> bool | None:
        done = self._run("network", "inspect", network)
        if done.returncode != 0:
            return None
        data = json.loads(done.stdout)
        return bool(data[0].get("internal")) if data else None

    def follow_logs(self, container_id: str) -> LogFollower:
        return _Follower([self._podman, "logs", "--follow", container_id])

    def networks(self, instance: str) -> list[str]:
        done = self._run("network", "ls", "-q", "--filter",
                         f"label=io.ilmar.owner.instance={instance}")  # fmt: skip
        return done.stdout.split()

    def remove_instance(self, instance: str) -> list[str]:
        """Remove every container and network labelled with this run's instance.

        The instance id is unique per live run, so nothing else is touched. Used
        after a failure, so no sandbox keeps a credential in memory.
        """
        removed = []
        for container_id in self.containers(instance):
            self._run("rm", "--force", "--time", "0", container_id)
            removed.append(container_id[:12])
        for network in self.networks(instance):
            self._run("network", "rm", "--force", network)
            removed.append(network)
        return removed


class _Follower:
    def __init__(self, argv: list[str]) -> None:
        self._chunks: list[str] = []
        self._process = subprocess.Popen(  # noqa: S603 - fixed argv
            argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
            env=host_environment(),
        )  # fmt: skip
        self._reader = threading.Thread(target=self._read, daemon=True)
        self._reader.start()

    def _read(self) -> None:
        assert self._process.stdout is not None
        for line in self._process.stdout:
            self._chunks.append(line)

    def text(self) -> str:
        return "".join(self._chunks)

    def stop(self) -> None:
        """End the follower and release its pipe.

        The pipe must be closed here: left to the garbage collector it raises a
        ResourceWarning (an error under this project's pytest settings) in
        whichever test happens to be running then.
        """
        with contextlib.suppress(subprocess.TimeoutExpired):
            self._process.wait(timeout=5)  # ends by itself when the proxy is removed
        if self._process.poll() is None:
            self._process.terminate()
            try:
                self._process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._process.kill()
                self._process.wait(timeout=5)
        self._reader.join(timeout=5)  # the pipe is at EOF now that the process ended
        if self._process.stdout is not None:
            self._process.stdout.close()


# -- reading the state the coordinator left ----------------------------------------------------


def read_operations(database: Path) -> list[tuple[str, int, str, str]]:
    """``(kind, attempt, status, key)`` of every operation, read-only (safe during a run)."""
    import sqlite3

    if not database.exists():
        return []
    connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True, timeout=5)
    try:
        rows = connection.execute("SELECT kind, attempt, status, key FROM operations").fetchall()
    except sqlite3.OperationalError:
        return []
    finally:
        connection.close()
    return [(str(k), int(a), str(s), str(key)) for k, a, s, key in rows]


def wait_for(
    condition: Callable[[], bool], *, timeout: float, interval: float = 0.5,
    clock: Callable[[], float] = time.monotonic, sleep: Callable[[float], None] = time.sleep,
) -> bool:  # fmt: skip
    """Poll ``condition`` until true or ``timeout`` seconds (monotonic) have passed."""
    deadline = clock() + timeout
    while True:
        if condition():
            return True
        if clock() >= deadline:
            return False
        sleep(interval)


def wait_while_running(
    running: Callable[[], bool], condition: Callable[[], bool], *, timeout: float,
    interval: float = 0.5, clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> str:  # fmt: skip
    """Wait for ``condition`` while a process runs: ``"met"``, ``"exited"`` or ``"timeout"``.

    A process that has stopped can no longer make the condition true, so the
    wait ends there instead of running out the timeout.
    """
    outcome = "timeout"

    def settled() -> bool:
        nonlocal outcome
        if condition():
            outcome = "met"
        elif not running():
            outcome = "exited"
        return outcome != "timeout"

    wait_for(settled, timeout=timeout, interval=interval, clock=clock, sleep=sleep)
    return outcome


# -- the record for #44 and #11d ---------------------------------------------------------------


def write_report(directory: Path, name: str, report: Mapping[str, Any],
                 secrets: Sequence[Secret]) -> Path:  # fmt: skip
    """Write a JSON report after checking it holds no credential in any form."""
    text = json.dumps(report, indent=2, sort_keys=True, default=str) + "\n"
    if find_secrets(text.encode(), secrets):
        raise LiveGuardError("a credential value reached the report; nothing was written")
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = directory / f"{name}.json"
    path.write_text(text)
    return path


def host_environment(base: Mapping[str, str] | None = None) -> dict[str, str]:
    """What the harness's own podman and git calls get: never a credential.

    The test process holds both credentials in its environment; a child that
    inherited them (podman, conmon, a container) would hold them too.
    """
    source = os.environ if base is None else base
    return {k: source[k] for k in PODMAN_ENVIRONMENT if k in source}


def tool(name: str) -> str:
    """The absolute path of a host program (no lookup through relative PATH entries)."""
    found = shutil.which(name)
    if found is None or not Path(found).is_absolute():
        raise LiveGuardError(f"{name} is not installed")
    return found


def git_blobs(repository: Path, revision: str) -> Iterator[tuple[str, bytes]]:
    """``(path, content)`` of every file in ``revision`` of a bare repository."""
    git = tool("git")
    listing = subprocess.run(  # noqa: S603 - fixed argv
        [git, "--git-dir", str(repository), "ls-tree", "-r", "-z", revision],
        capture_output=True, check=True, env=host_environment(),
    )  # fmt: skip
    for entry in listing.stdout.split(b"\0"):
        if not entry:
            continue
        meta, _, path = entry.partition(b"\t")
        object_id = meta.split()[2].decode()
        blob = subprocess.run(  # noqa: S603 - fixed argv
            [git, "--git-dir", str(repository), "cat-file", "blob", object_id],
            capture_output=True, check=True, env=host_environment(),
        )  # fmt: skip
        yield path.decode(errors="replace"), blob.stdout
