# SPDX-License-Identifier: Apache-2.0
"""The live-test harness, offline (#11d): guards, leak detection, watching, configuration.

No network, no Podman, no credentials: GitHub is a scripted transport, Podman a
fake view, and every "secret" below is made up.
"""

from __future__ import annotations

import base64
import json
import os
import re
import stat
import urllib.parse
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

import live_harness as h
from ilmar.adapters.github.client import HttpRequest, HttpResponse
from ilmar.config import load_config

NOW = datetime(2026, 11, 5, 20, tzinfo=UTC)
TOKEN = "github_pat_" + "A1b2C3d4" * 8
KEY = "sk-ant-api03-" + "Zy9Xw8Vu" * 8
ENV = {
    "ILMAR_LIVE_REPO": "owner/ilmar-disposable",
    "ILMAR_LIVE_GITHUB_TOKEN": TOKEN,
    "ANTHROPIC_API_KEY": KEY,
    "ILMAR_TEST_CLAUDE_IMAGE": "localhost/ilmar-claude-code:2.1.0",
    "ILMAR_TEST_CLAUDE_VERSION": "2.1.0",
    "ILMAR_TEST_PODMAN_PROXY_IMAGE": "docker.io/library/python:3.12-alpine",
    "ILMAR_LIVE_OUTSIDE_REPO": "owner/Ilmar",
}


def settings(**changes: str) -> h.LiveSettings:
    return h.settings_from({**ENV, **changes}, default_report=Path("/tmp/report"))


def expiry(at: datetime) -> str:
    return at.strftime("%Y-%m-%d %H:%M:%S UTC")


@dataclass
class FakeGitHub:
    """A scripted GitHub: answers by (method, path), records every request."""

    repo: dict[str, Any] = field(
        default_factory=lambda: {
            "name": "ilmar-disposable",
            "private": True,
            "topics": ["ilmar-disposable"],
            "default_branch": "main",
            "full_name": "owner/ilmar-disposable",
        }
    )
    headers: dict[str, str] = field(
        default_factory=lambda: {
            "GitHub-Authentication-Token-Expiration": expiry(NOW + timedelta(days=6))
        }
    )
    statuses: dict[tuple[str, str], int] = field(default_factory=dict)
    requests: list[HttpRequest] = field(default_factory=list)
    labelled: dict[int, set[str]] = field(default_factory=dict)  # issue: labels it carries
    drop_labels: bool = False  # answer an add-labels call without adding them
    unlisted_reads: int = 0  # how many label listings miss a labelled issue (lag)

    def __call__(self, request: HttpRequest) -> HttpResponse:
        self.requests.append(request)
        path = request.url.removeprefix(h.API)
        key = (request.method, path.split("?")[0])
        if key in self.statuses:
            return HttpResponse(self.statuses[key], {}, b'{"message":"scripted"}')
        if key == ("GET", "/repos/owner/ilmar-disposable"):
            return HttpResponse(200, self.headers, json.dumps(self.repo).encode())
        if key == ("GET", "/repos/owner/Ilmar"):
            return HttpResponse(404, {}, b'{"message":"Not Found"}')
        if key[1].endswith(("/actions/permissions", "/actions/runs", "/actions/secrets", "/hooks")):
            return HttpResponse(403, {}, b'{"message":"Resource not accessible"}')
        if key == ("PUT", f"/repos/owner/ilmar-disposable/contents/{h.PROBE_FILE}"):
            return HttpResponse(201, {}, b'{"content":{}}')
        if key == ("POST", "/repos/owner/ilmar-disposable/pulls"):
            return HttpResponse(201, {}, b'{"number":9}')
        if key[0] == "POST" and (match := re.fullmatch(r".*/issues/(\d+)/labels", key[1])):
            names = self.labelled.setdefault(int(match.group(1)), set())
            if not self.drop_labels:
                names |= set(json.loads(request.body or b"{}")["labels"])
            return HttpResponse(200, {}, json.dumps([{"name": n} for n in sorted(names)]).encode())
        if key[0] == "GET" and key[1].endswith("/issues") and "labels=" in path:
            wanted = urllib.parse.parse_qs(urllib.parse.urlsplit(path).query)["labels"][0]
            if self.unlisted_reads > 0:
                self.unlisted_reads -= 1
                return HttpResponse(200, {}, b"[]")
            listed = [
                {"number": n} for n, names in sorted(self.labelled.items()) if wanted in names
            ]
            return HttpResponse(200, {}, json.dumps(listed).encode())
        if key[0] == "GET" and key[1].endswith(("/issues", "/pulls")):
            return HttpResponse(200, {}, b"[]")
        if key[1].endswith("/git/ref/heads/main"):
            return HttpResponse(200, {}, json.dumps({"object": {"sha": "a" * 40}}).encode())
        if key == ("PUT", f"/repos/owner/ilmar-disposable/contents/{h.PROBE_WORKFLOW}"):
            return HttpResponse(404, {}, b'{"message":"Not Found"}')
        if key[0] == "POST" and key[1].endswith("/git/trees"):
            return HttpResponse(201, {}, b'{"sha":"' + b"t" * 40 + b'"}')
        if key[0] == "POST" and key[1].endswith("/git/commits"):
            return HttpResponse(201, {}, b'{"sha":"' + b"c" * 40 + b'"}')
        if key[0] == "POST" and key[1].endswith("/issues"):
            return HttpResponse(201, {}, b'{"number":7}')
        if key[0] in ("POST", "DELETE", "PATCH"):
            return HttpResponse(201 if key[0] == "POST" else 204, {}, b"")
        if key[0] == "GET" and key[1].endswith("/pulls"):
            return HttpResponse(200, {}, b"[]")
        return HttpResponse(500, {}, b'{"message":"unscripted"}')

    def calls(self, method: str) -> list[str]:
        return [r.url.removeprefix(h.API) for r in self.requests if r.method == method]


def run_preflight(fake: FakeGitHub, **changes: str) -> h.Preflight:
    live = settings(**changes)
    return h.preflight(h.LiveGitHub(live.github_token, fake), live, now=NOW)


# -- secrets -----------------------------------------------------------------------------------


def test_a_secret_never_shows_its_value() -> None:
    secret = h.Secret("ILMAR_LIVE_GITHUB_TOKEN", TOKEN)
    for shown in (repr(secret), str(secret), f"{secret}", repr([secret])):
        assert TOKEN not in shown
        assert "<redacted>" in shown
    assert secret.reveal() == TOKEN


def test_every_encoding_of_a_secret_is_found_and_redacted() -> None:
    secret = h.Secret("ILMAR_LIVE_GITHUB_TOKEN", TOKEN)
    basic = base64.b64encode(f"x-access-token:{TOKEN}".encode())
    for data in (
        TOKEN.encode(),
        base64.b64encode(TOKEN.encode()),
        b"Authorization: Basic " + basic,
    ):
        assert h.find_secrets(b"..." + data + b"...", [secret]) == ["ILMAR_LIVE_GITHUB_TOKEN"]
    text = f"token={TOKEN} header=Basic {basic.decode()}"
    assert h.redact(text, [secret]) == (
        "token=<ILMAR_LIVE_GITHUB_TOKEN> header=Basic <ILMAR_LIVE_GITHUB_TOKEN>"
    )
    assert h.find_secrets(b"nothing here", [secret]) == []


def test_fragments_of_a_secret_are_found() -> None:
    """Tools record keys by fragments: Claude Code by the last 20 characters."""
    key = h.Secret("ANTHROPIC_API_KEY", KEY)
    assert h.find_secrets(f'"approved":["{KEY[-20:]}"]'.encode(), [key]) == ["ANTHROPIC_API_KEY"]
    assert h.find_secrets(b"sk-ant-api03-", [key]) == []  # the shared prefix is no leak


def test_scan_tree_names_files_and_secrets_never_values(tmp_path: Path) -> None:
    secrets = settings().secrets
    (tmp_path / "a").mkdir()
    (tmp_path / "a" / "db.sqlite3").write_bytes(b"\0\0" + KEY.encode() + b"\0")
    (tmp_path / "clean.txt").write_text("fine")
    (tmp_path / "link").symlink_to(tmp_path / "a" / "db.sqlite3")
    found = h.scan_tree(tmp_path, secrets)
    assert found == {"a/db.sqlite3": ["ANTHROPIC_API_KEY"]}  # the symlink is not followed
    assert TOKEN not in repr(found)
    assert KEY not in repr(found)


def test_a_stale_env_file_in_a_temporary_directory_is_found(tmp_path: Path) -> None:
    (tmp_path / "ilmar-env-x1").write_text(f"ANTHROPIC_API_KEY={KEY}\n")
    (tmp_path / "other").write_text("fine")
    (tmp_path / "sub").mkdir()
    assert h.secret_spool_files(tmp_path, settings().secrets) == {
        "ilmar-env-x1": ["ANTHROPIC_API_KEY"]
    }


# -- settings ----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"ILMAR_LIVE_REPO": "owner/other"}, "ilmar-disposable"),
        ({"ILMAR_LIVE_GITHUB_TOKEN": "ghp_" + "x" * 36}, "fine-grained"),
        ({"ANTHROPIC_API_KEY": TOKEN}, "must differ"),
        ({"ILMAR_TEST_CLAUDE_VERSION": "latest"}, "pinned"),
        ({"ILMAR_LIVE_OUTSIDE_REPO": "owner/ilmar-disposable"}, "another"),
        ({"ANTHROPIC_API_KEY": "short"}, "too short"),
    ],
)
def test_settings_refuse_anything_but_the_required_setup(
    changes: dict[str, str], message: str
) -> None:
    with pytest.raises(h.LiveGuardError, match=message) as info:
        settings(**changes)
    assert TOKEN not in str(info.value)
    assert KEY not in str(info.value)


def test_missing_variables_are_named_without_values() -> None:
    env = {k: v for k, v in ENV.items() if k != "ANTHROPIC_API_KEY"}
    assert h.missing_variables(env) == ["ANTHROPIC_API_KEY"]
    with pytest.raises(h.LiveGuardError, match="not set: ANTHROPIC_API_KEY") as info:
        h.settings_from(env, default_report=Path("/tmp/x"))
    assert TOKEN not in str(info.value)


# -- preflight ---------------------------------------------------------------------------------


def test_preflight_accepts_the_required_repository_and_token() -> None:
    fake = FakeGitHub()
    result = run_preflight(fake)
    assert result.default_branch == "main"
    assert result.token_expires == expiry(NOW + timedelta(days=6))[:16] + " UTC"
    assert len(result.checks) == 12
    assert result.checks[-1] == "token: can write issues, labels and comments"
    probe = [p for p in fake.calls("DELETE") if "probe" in p]
    assert probe == [  # the probe branch, then the probe label: nothing stays
        f"/repos/owner/ilmar-disposable/git/refs/heads/ilmar-live/probe-{NOW:%Y%m%d%H%M%S}",
        f"/repos/owner/ilmar-disposable/labels/ilmar-live%3Aprobe-{NOW:%Y%m%d%H%M%S}",
    ]
    for request in fake.requests:  # the token goes only into the Authorization header
        assert TOKEN not in request.url
        assert TOKEN not in (request.body or b"").decode()
        assert request.headers["Authorization"] == f"Bearer {TOKEN}"


@pytest.mark.parametrize(
    ("repo", "message"),
    [
        ({"private": False}, "private"),
        ({"topics": []}, "topic"),
        ({"name": "other"}, "not named"),
        ({"archived": True}, "unarchived"),
    ],
)
def test_preflight_refuses_any_other_repository(repo: dict[str, Any], message: str) -> None:
    fake = FakeGitHub()
    fake.repo.update(repo)
    with pytest.raises(h.LiveGuardError, match=message):
        run_preflight(fake)
    assert fake.calls("PUT") == []  # nothing was written
    assert fake.calls("POST") == []


@pytest.mark.parametrize(
    ("headers", "message"),
    [
        ({"X-OAuth-Scopes": "repo"}, "classic token"),
        ({}, "no expiry"),
        ({"GitHub-Authentication-Token-Expiration": expiry(NOW + timedelta(days=8))}, "7 days"),
        ({"GitHub-Authentication-Token-Expiration": expiry(NOW - timedelta(hours=1))}, "7 days"),
    ],
)
def test_preflight_refuses_a_token_that_is_not_short_lived_and_fine_grained(
    headers: dict[str, str], message: str
) -> None:
    fake = FakeGitHub()
    if "X-OAuth-Scopes" in headers:
        fake.headers.update(headers)
    else:
        fake.headers = headers
    with pytest.raises(h.LiveGuardError, match=message):
        run_preflight(fake)


@pytest.mark.parametrize(
    ("endpoint", "message"),
    [
        (("GET", "/repos/owner/Ilmar"), "can see owner/Ilmar"),
        (("GET", "/repos/owner/ilmar-disposable/actions/permissions"), "Administration"),
        (("GET", "/repos/owner/ilmar-disposable/actions/runs"), "Actions"),
    ],
)
def test_preflight_refuses_a_token_with_more_reach(endpoint: tuple[str, str], message: str) -> None:
    fake = FakeGitHub(statuses={endpoint: 200})
    with pytest.raises(h.LiveGuardError, match=message):
        run_preflight(fake)


def test_preflight_refuses_a_token_that_can_write_workflows_and_removes_the_probe() -> None:
    put = ("PUT", f"/repos/owner/ilmar-disposable/contents/{h.PROBE_WORKFLOW}")
    fake = FakeGitHub(statuses={put: 201})
    with pytest.raises(h.LiveGuardError, match="Workflows permission"):
        run_preflight(fake)
    assert any("probe" in p for p in fake.calls("DELETE"))


def test_preflight_refuses_a_repository_reached_through_a_redirect() -> None:
    fake = FakeGitHub()
    fake.repo["full_name"] = "someone-else/ilmar-disposable"
    with pytest.raises(h.LiveGuardError, match="redirects"):
        run_preflight(fake)


@pytest.mark.parametrize("permission", ["actions/secrets", "hooks"])
def test_preflight_refuses_secrets_or_webhooks_permission(permission: str) -> None:
    fake = FakeGitHub(statuses={("GET", f"/repos/owner/ilmar-disposable/{permission}"): 200})
    with pytest.raises(h.LiveGuardError, match="must not"):
        run_preflight(fake)


@pytest.mark.parametrize("status", [401, 422, 500, 502])
def test_an_unclear_answer_is_never_taken_as_no_permission(status: int) -> None:
    """Review of PR #50: only 403/404 mean "not granted"."""
    for endpoint in (
        ("GET", "/repos/owner/ilmar-disposable/actions/permissions"),
        ("PUT", f"/repos/owner/ilmar-disposable/contents/{h.PROBE_WORKFLOW}"),
    ):
        fake = FakeGitHub(statuses={endpoint: status})
        with pytest.raises(h.LiveGuardError, match="could not be checked"):
            run_preflight(fake)


def test_a_refused_workflow_write_counts_only_after_a_successful_control_write() -> None:
    """Too little Contents permission must not pass for "no Workflows permission"."""
    put = ("PUT", f"/repos/owner/ilmar-disposable/contents/{h.PROBE_FILE}")
    fake = FakeGitHub(statuses={put: 403})
    with pytest.raises(h.LiveGuardError, match="cannot write contents"):
        run_preflight(fake)
    assert any("probe" in p for p in fake.calls("DELETE"))  # the branch still goes
    assert not [r for r in fake.requests if h.PROBE_WORKFLOW in r.url]


@pytest.mark.parametrize(
    ("endpoint", "message"),
    [
        (("GET", "/repos/owner/ilmar-disposable/issues"), "lacks Issues"),
        (("GET", "/repos/owner/ilmar-disposable/pulls"), "lacks Pull requests"),
        (("POST", "/repos/owner/ilmar-disposable/pulls"), "cannot open pull requests"),
    ],
)
def test_preflight_refuses_a_token_with_too_little(endpoint: tuple[str, str], message: str) -> None:
    fake = FakeGitHub(statuses={endpoint: 403})
    with pytest.raises(h.LiveGuardError, match=message):
        run_preflight(fake)


PROBE_LABEL = "/repos/owner/ilmar-disposable/labels/ilmar-live%3Aprobe-20261105200000"


@pytest.mark.parametrize(
    ("endpoint", "message"),
    [
        # Issues read only: creating an issue is refused (Pull requests write
        # alone would still allow the label calls, so this comes first).
        (("POST", "/repos/owner/ilmar-disposable/issues"), "cannot create issues"),
        (("POST", "/repos/owner/ilmar-disposable/labels"), "cannot create labels"),
        (("POST", "/repos/owner/ilmar-disposable/issues/7/labels"), "cannot add labels"),
        (("POST", "/repos/owner/ilmar-disposable/issues/7/comments"), "cannot comment"),
    ],
)
def test_preflight_names_a_missing_issues_write(endpoint: tuple[str, str], message: str) -> None:
    fake = FakeGitHub(statuses={endpoint: 403})
    with pytest.raises(h.LiveGuardError, match=f"{message}.*HTTP 403.*needs Issues write"):
        run_preflight(fake)
    if endpoint[1].endswith("/issues"):
        assert "/repos/owner/ilmar-disposable/issues/7" not in fake.calls("PATCH")  # none made
    else:  # the probe issue and label never stay behind
        assert "/repos/owner/ilmar-disposable/issues/7" in fake.calls("PATCH")
        assert PROBE_LABEL in fake.calls("DELETE")


def test_preflight_refuses_a_label_github_did_not_keep() -> None:
    fake = FakeGitHub(drop_labels=True)
    with pytest.raises(h.LiveGuardError, match="did not keep the label"):
        run_preflight(fake)
    assert PROBE_LABEL in fake.calls("DELETE")


def test_preflight_probes_issues_before_any_run_is_seeded() -> None:
    fake = FakeGitHub()
    run_preflight(fake)
    posts = [r.url.removeprefix(h.API) for r in fake.requests if r.method == "POST"]
    probe = posts.index("/repos/owner/ilmar-disposable/issues")
    assert posts[probe:] == [
        "/repos/owner/ilmar-disposable/issues",
        "/repos/owner/ilmar-disposable/labels",
        "/repos/owner/ilmar-disposable/issues/7/labels",
        "/repos/owner/ilmar-disposable/issues/7/comments",
    ]


def test_the_probe_pull_request_is_closed_and_its_branch_deleted() -> None:
    fake = FakeGitHub()
    run_preflight(fake)
    assert fake.calls("PATCH") == [
        "/repos/owner/ilmar-disposable/pulls/9",
        "/repos/owner/ilmar-disposable/issues/7",  # the Issues probe, closed too
    ]
    assert [p for p in fake.calls("DELETE") if "probe" in p]


@pytest.mark.parametrize(
    "text",
    ["2026-11-12 10:00:00 UTC", "2026-11-12 11:00:00 +0100", "2026-11-12T10:00:00+0000"],
)
def test_token_expiry_formats(text: str) -> None:
    assert h.token_expiry(text) == datetime(2026, 11, 12, 10, tzinfo=UTC)


def test_an_unreadable_expiry_is_no_expiry() -> None:
    assert h.token_expiry("") is None
    assert h.token_expiry("next week") is None


# -- setting up a run --------------------------------------------------------------------------


def seed(fake: FakeGitHub, *, clock: Any = None) -> h.LiveRun:
    live = settings()
    ticks = iter(range(10_000))
    return h.seed_run(
        h.LiveGitHub(live.github_token, fake), live.repository, "main", "r-0001ab",
        clock=clock or (lambda: float(next(ticks))), sleep=lambda _: None,
    )  # fmt: skip


def test_a_run_gets_a_new_base_branch_and_issue_and_rewrites_nothing() -> None:
    fake = FakeGitHub()
    run = seed(fake)
    assert run == h.LiveRun("r-0001ab", "ilmar-live/r-0001ab", "ilmar-live:r-0001ab", 7)
    refs = [json.loads(r.body or b"{}") for r in fake.requests if r.url.endswith("/git/refs")]
    assert refs == [{"ref": "refs/heads/ilmar-live/r-0001ab", "sha": "c" * 40}]
    assert fake.calls("PATCH") == []  # no existing ref is moved, nothing is forced
    commit = next(r for r in fake.requests if r.url.endswith("/git/commits"))
    assert json.loads(commit.body or b"{}")["parents"] == ["a" * 40]


LABEL = "ilmar-live:r-0001ab"
LISTING = (
    "/repos/owner/ilmar-disposable/issues?state=open&labels=ilmar-live%3Ar-0001ab&per_page=100"
)


def test_the_seeded_issue_is_labelled_loudly_and_listed_before_the_run() -> None:
    """Regression (first live run): the coordinator found no task, because nothing
    proved the seeded issue was listed under its label. Now the label is created
    and added by endpoints that fail loudly, and the issue must be in the listing."""
    fake = FakeGitHub()
    run = seed(fake)
    posts = [(r.url.removeprefix(h.API), json.loads(r.body or b"{}"))
             for r in fake.requests if r.method == "POST"]  # fmt: skip
    label_post = ("/repos/owner/ilmar-disposable/labels",
                  {"name": LABEL, "color": "c5def5",
                   "description": "Ilmar live test run; safe to delete"})  # fmt: skip
    assert label_post in posts
    (issue,) = [b for u, b in posts if u == "/repos/owner/ilmar-disposable/issues"]
    assert "labels" not in issue  # never the silently dropped form
    assert ("/repos/owner/ilmar-disposable/issues/7/labels", {"labels": [LABEL]}) in posts
    assert fake.calls("GET")[-1] == LISTING
    assert run.issue == 7


def test_the_harness_checks_exactly_the_listing_the_coordinator_reads() -> None:
    from ilmar.adapters.github import GitHubClient, GitHubTaskProvider

    seen: list[str] = []

    def transport(request: HttpRequest) -> HttpResponse:
        seen.append(request.url.removeprefix(h.API))
        return HttpResponse(200, {}, b"[]")

    provider = GitHubTaskProvider(GitHubClient("t" * 40, transport=transport),
                                  repository="owner/ilmar-disposable", instance_id="live-x",
                                  eligible_label=LABEL)  # fmt: skip
    assert provider.list_eligible() == ()
    assert seen == [h.eligible_issues_path("owner/ilmar-disposable", LABEL)] == [LISTING]


def test_a_listing_that_lags_is_waited_for_within_the_bound() -> None:
    fake = FakeGitHub(unlisted_reads=3)
    assert seed(fake).issue == 7
    assert fake.calls("GET").count(LISTING) == 4


def made_and_removed(fake: FakeGitHub) -> None:
    deleted = fake.calls("DELETE")
    assert "/repos/owner/ilmar-disposable/git/refs/heads/ilmar-live/r-0001ab" in deleted
    assert "/repos/owner/ilmar-disposable/labels/ilmar-live%3Ar-0001ab" in deleted


def test_an_issue_github_never_lists_stops_the_run_and_is_removed() -> None:
    fake = FakeGitHub(unlisted_reads=10_000)
    with pytest.raises(h.LiveGuardError, match="did not list the seeded issue"):
        seed(fake)
    assert fake.calls("GET").count(LISTING) <= h.SEED_LISTING_TIMEOUT + 2  # bounded
    assert "/repos/owner/ilmar-disposable/issues/7" in fake.calls("PATCH")  # closed
    made_and_removed(fake)


def test_a_label_github_drops_stops_the_run_and_is_removed() -> None:
    fake = FakeGitHub(drop_labels=True)
    with pytest.raises(h.LiveGuardError, match="did not put the run's label"):
        seed(fake)
    assert LISTING not in fake.calls("GET")  # stopped before waiting on it
    assert "/repos/owner/ilmar-disposable/issues/7" in fake.calls("PATCH")
    made_and_removed(fake)


@pytest.mark.parametrize(
    ("endpoint", "message"),
    [
        (("POST", "/repos/owner/ilmar-disposable/labels"), "label could not be created"),
        (("POST", "/repos/owner/ilmar-disposable/issues/7/labels"), "did not put the run's label"),
    ],
)
def test_a_refused_label_call_stops_the_run(endpoint: tuple[str, str], message: str) -> None:
    fake = FakeGitHub(statuses={endpoint: 403})
    with pytest.raises(h.LiveGuardError, match=message):
        seed(fake)
    made_and_removed(fake)


# -- waiting on a running process ---------------------------------------------------------------


def test_a_wait_ends_as_soon_as_the_process_has_exited() -> None:
    """Regression (first live run): the crash drill waited the full 600 s for a
    coordinator that had already exited, then said only "never reached the model"."""
    polls: list[int] = []

    def never() -> bool:
        polls.append(1)
        return False

    ticks = iter(range(10_000))
    outcome = h.wait_while_running(
        lambda: len(polls) < 3, never,
        timeout=600, clock=lambda: float(next(ticks)), sleep=lambda _: None,
    )  # fmt: skip
    assert outcome == "exited"
    assert len(polls) == 3  # stopped at the check that saw the exit, not after 600 s


def test_a_wait_reports_the_condition_and_the_timeout() -> None:
    ticks = iter(range(10_000))
    assert h.wait_while_running(lambda: True, lambda: True, timeout=5) == "met"
    assert h.wait_while_running(lambda: True, lambda: False, timeout=3,
                                clock=lambda: float(next(ticks)),
                                sleep=lambda _: None) == "timeout"  # fmt: skip
    # Met in the same check that sees the exit: the condition wins.
    assert h.wait_while_running(lambda: False, lambda: True, timeout=5) == "met"


def test_cleanup_closes_and_deletes_only_what_the_run_made() -> None:
    fake = FakeGitHub()
    live = settings()
    run = h.LiveRun("r-0001ab", "ilmar-live/r-0001ab", "ilmar-live:r-0001ab", 7)
    assert h.cleanup_run(h.LiveGitHub(live.github_token, fake), live.repository, run) == []
    assert fake.calls("DELETE") == [
        "/repos/owner/ilmar-disposable/git/refs/heads/ilmar-live/r-0001ab",
        "/repos/owner/ilmar-disposable/labels/ilmar-live%3Ar-0001ab",
    ]
    assert fake.calls("PATCH") == ["/repos/owner/ilmar-disposable/issues/7"]


def test_the_configuration_names_variables_and_is_valid(tmp_path: Path) -> None:
    live = settings()
    run = h.LiveRun("r-0001ab", "ilmar-live/r-0001ab", "ilmar-live:r-0001ab", 7)
    state = tmp_path / "state"
    text = h.live_config(live, run, state)
    assert TOKEN not in text
    assert KEY not in text
    assert 'token_env = "ILMAR_LIVE_GITHUB_TOKEN"' in text
    path = h.write_config(tmp_path / "cfg" / "live.toml", text)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    with pytest.raises(FileExistsError):
        h.write_config(path, text)  # never overwrites
    config = load_config(path)
    assert config.state_dir == state
    assert config.limits.max_attempts == 3


def test_the_child_environment_carries_exactly_two_credentials() -> None:
    live = settings()
    base = {"PATH": "/usr/bin", "HOME": "/home/x", "GITHUB_TOKEN": "other", "AWS_SECRET": "x"}
    env = h.child_environment(live, base)
    assert env == {
        "PATH": "/usr/bin",
        "HOME": "/home/x",
        "ILMAR_LIVE_GITHUB_TOKEN": TOKEN,
        "ANTHROPIC_API_KEY": KEY,
    }


# -- watching ----------------------------------------------------------------------------------


def inspected(name: str, role: str, *, cmd: list[str] | None = None, env: list[str] | None = None,
              network: str = "none") -> dict[str, Any]:  # fmt: skip
    return {
        "Name": name,
        "Config": {"Labels": {"io.ilmar.role": role}, "Cmd": cmd or [], "Env": env or []},
        "HostConfig": {"NetworkMode": network},
    }


def test_describe_reads_the_allow_list_and_finds_secrets_in_a_definition() -> None:
    secrets = settings().secrets
    proxy = h.describe(
        inspected(
            "x-egress", "egress-proxy", cmd=["python3", "p.py", "--allow", "api.anthropic.com"]
        ),
        secrets,
    )
    assert (proxy.role, proxy.allowed_hosts, proxy.secrets_in_definition) == (
        "egress-proxy", ("api.anthropic.com",), [],
    )  # fmt: skip
    leaky = h.describe(inspected("x", "sandbox", env=[f"GH={TOKEN}"]), secrets)
    assert leaky.secrets_in_definition == ["ILMAR_LIVE_GITHUB_TOKEN"]
    assert TOKEN not in repr(leaky)


def test_parse_egress_separates_allowed_hosts_from_denials() -> None:
    log = (
        "ilmar-egress ALLOW host=api.anthropic.com address=1.2.3.4\n"
        "ilmar-egress ALLOW host=api.anthropic.com address=1.2.3.5\n"
        "ilmar-egress DENY reason=host not allowed target=statsig.anthropic.com:443\n"
    )
    allowed, denied = h.parse_egress(log)
    assert allowed == ["api.anthropic.com"]
    assert denied == ["ilmar-egress DENY reason=host not allowed target=statsig.anthropic.com:443"]


def test_processes_with_secrets_in_their_argv_are_found(tmp_path: Path) -> None:
    for pid, argv in (("10", [b"/usr/bin/git", b"push"]), ("11", [b"/bin/curl", TOKEN.encode()])):
        (tmp_path / pid).mkdir()
        (tmp_path / pid / "cmdline").write_bytes(b"\0".join(argv) + b"\0")
    (tmp_path / "self").mkdir()
    assert h.processes_with_secrets(settings().secrets, tmp_path) == [
        "curl: ILMAR_LIVE_GITHUB_TOKEN"
    ]


class FakeFollower:
    def __init__(self, text: str) -> None:
        self._text, self.stopped = text, False

    def text(self) -> str:
        return self._text

    def stop(self) -> None:
        self.stopped = True


@dataclass
class FakeView:
    present: dict[str, dict[str, Any]] = field(default_factory=dict)
    followers: dict[str, FakeFollower] = field(default_factory=dict)

    def containers(self, instance: str) -> list[str]:
        return list(self.present)

    def inspect(self, container_id: str) -> dict[str, Any] | None:
        return self.present.get(container_id)

    def network_internal(self, network: str) -> bool | None:
        return True

    def follow_logs(self, container_id: str) -> FakeFollower:
        follower = FakeFollower("ilmar-egress ALLOW host=api.anthropic.com address=1\n")
        self.followers[container_id] = follower
        return follower


def test_the_watcher_records_each_container_once_and_follows_proxy_logs() -> None:
    view = FakeView()
    watcher = h.Watcher(view, "live", settings().secrets, proc=None)
    view.present["s1"] = inspected("ilmar-live-e1-1", "sandbox", network="ilmar-live-e1-1-net")
    watcher.poll()
    view.present["p1"] = inspected("ilmar-live-e1-1-egress", "egress-proxy",
                                   cmd=["--allow", "api.anthropic.com"])  # fmt: skip
    watcher.poll()
    watcher.poll()
    del view.present["s1"], view.present["p1"]  # removed by the coordinator
    watcher.poll()
    watcher.stop()
    assert [s.name for s in watcher.sandboxes()] == ["ilmar-live-e1-1"]
    (proxy,) = watcher.proxies()
    assert proxy.log.startswith("ilmar-egress ALLOW")
    assert view.followers["p1"].stopped
    assert watcher.argv_leaks == set()


def test_the_watcher_thread_reports_errors_instead_of_hiding_them() -> None:
    class Broken(FakeView):
        def containers(self, instance: str) -> list[str]:
            raise RuntimeError("podman is gone")

    with h.Watcher(Broken(), "live", settings().secrets, interval=0.01, proc=None) as watcher:
        assert h.wait_for(lambda: bool(watcher.errors), timeout=2, interval=0.01)
    assert watcher.errors[0] == "RuntimeError"


# -- state, waiting and reports ----------------------------------------------------------------


def test_read_operations_is_read_only_and_tolerates_a_missing_store(tmp_path: Path) -> None:
    from ilmar.adapters.sqlite import SQLiteExecutionStore

    assert h.read_operations(tmp_path / "missing.sqlite3") == []
    database = tmp_path / "executions.sqlite3"
    SQLiteExecutionStore(database).close()
    before = database.stat().st_mtime_ns
    assert h.read_operations(database) == []
    assert database.stat().st_mtime_ns == before


def test_wait_for_uses_a_monotonic_deadline() -> None:
    ticks = iter(range(100))
    calls: list[float] = []
    assert not h.wait_for(lambda: False, timeout=3, clock=lambda: float(next(ticks)),
                          sleep=calls.append)  # fmt: skip
    assert len(calls) == 2  # slept at t=1 and t=2; at t=3 the deadline has passed
    assert h.wait_for(lambda: True, timeout=0, sleep=calls.append)


def test_a_report_with_a_credential_is_never_written(tmp_path: Path) -> None:
    secrets = settings().secrets
    with pytest.raises(h.LiveGuardError):
        h.write_report(tmp_path, "r", {"note": f"oops {base64.b64encode(KEY.encode()).decode()}"},
                       secrets)  # fmt: skip
    assert list(tmp_path.iterdir()) == []
    path = h.write_report(tmp_path, "r", {"hosts": ["api.anthropic.com"]}, secrets)
    assert json.loads(path.read_text()) == {"hosts": ["api.anthropic.com"]}


def test_the_harness_never_reads_credentials_from_anywhere_but_the_environment() -> None:
    source = Path(h.__file__).read_text()
    assert "getpass" not in source
    assert "input(" not in source
    assert ".netrc" not in source
    assert "keyring" not in source


# -- the live tests themselves ------------------------------------------------------------


LIVE_FILES = ("test_live_end_to_end.py", "test_claude_code_live.py")
INTEGRATION = Path(__file__).resolve().parents[1] / "integration"
SENSITIVE_CALLS = {"find_secrets", "scan_tree", "redact", "reveal", "child_environment",
                   "environment"}  # fmt: skip
SENSITIVE_ATTRIBUTES = {"stdout", "stderr", "outputs"}


def _assert_problems(source: str) -> list[str]:
    import ast

    problems = []
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Assert):
            continue
        for inner in ast.walk(node.test):
            name = ""
            if isinstance(inner, ast.Call):
                func = inner.func
                name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
                if name not in SENSITIVE_CALLS:
                    continue
            elif isinstance(inner, ast.Attribute) and inner.attr in SENSITIVE_ATTRIBUTES:
                name = inner.attr
            else:
                continue
            problems.append(f"line {node.lineno}: {name}")
    return problems


@pytest.mark.parametrize("name", LIVE_FILES)
def test_no_assertion_in_a_live_test_can_print_secret_bearing_data(name: str) -> None:
    """pytest shows the values and call arguments inside a failing ``assert``."""
    assert _assert_problems((INTEGRATION / name).read_text()) == []


def test_the_assertion_guard_finds_what_it_looks_for() -> None:
    sample = "assert h.find_secrets(out, s) == []\nassert 'x' in done.stdout\nassert ok\n"
    assert _assert_problems(sample) == ["line 1: find_secrets", "line 2: stdout"]


def test_live_tests_are_opt_in_and_never_in_ci() -> None:
    """Never in the CI that runs on every push and pull request; only in the manual
    ``live`` workflow, which test_live_workflow.py checks."""
    ci = (Path(__file__).resolve().parents[2] / ".github" / "workflows" / "ci.yml").read_text()
    for name in LIVE_FILES:
        source = (INTEGRATION / name).read_text()
        assert "pytestmark = pytest.mark.integration" in source
        assert name not in ci


def test_only_the_coordinator_and_git_may_hold_the_token_in_their_environment(
    tmp_path: Path,
) -> None:
    secrets = [h.Secret("ILMAR_LIVE_GITHUB_TOKEN", TOKEN)]
    for pid, program in (("1", "python3"), ("2", "git-remote-http"), ("3", "conmon")):
        (tmp_path / pid).mkdir()
        (tmp_path / pid / "comm").write_text(program + "\n")
        (tmp_path / pid / "environ").write_bytes(f"ILMAR_LIVE_GITHUB_TOKEN={TOKEN}\0".encode())
    found = h.environments_with_secrets(secrets, tmp_path, trusted=h.TRUSTED_WITH_TOKEN)
    assert found == ["conmon: ILMAR_LIVE_GITHUB_TOKEN"]


def test_orphaned_processes_are_found_by_container_id(tmp_path: Path) -> None:
    (tmp_path / "5").mkdir()
    (tmp_path / "5" / "cmdline").write_bytes(b"/usr/bin/podman\0exec\0" + b"f" * 64 + b"\0")
    assert h.processes_mentioning(["f" * 64], tmp_path) == ["podman"]
    assert h.processes_mentioning(["e" * 64], tmp_path) == []


def test_leaked_files_are_wiped_and_named(tmp_path: Path) -> None:
    (tmp_path / "keep.txt").write_text("fine")
    (tmp_path / "leak.json").write_text(f'{{"k": "{KEY}"}}')
    assert h.wipe_leaks(tmp_path, settings().secrets) == ["leak.json"]
    assert sorted(p.name for p in tmp_path.iterdir()) == ["keep.txt"]


def test_the_watcher_checks_each_sandbox_network() -> None:
    view = FakeView()
    view.present["s1"] = {
        **inspected("ilmar-live-e1-1", "sandbox"),
        "NetworkSettings": {"Networks": {"ilmar-live-e1-1-net": {}}},
    }
    watcher = h.Watcher(view, "live", settings().secrets, proc=None)
    watcher.poll()
    assert watcher.sandboxes()[0].networks == ("ilmar-live-e1-1-net",)
    assert watcher.internal_networks == {"ilmar-live-e1-1-net": True}


def test_the_harness_never_hands_a_credential_to_podman_or_git(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Review of PR #50: the test process holds both credentials; its children must not."""
    monkeypatch.setenv("ILMAR_LIVE_GITHUB_TOKEN", TOKEN)
    monkeypatch.setenv("ANTHROPIC_API_KEY", KEY)
    monkeypatch.setenv("XDG_RUNTIME_DIR", "/run/user/1000")
    env = h.host_environment()
    assert "ILMAR_LIVE_GITHUB_TOKEN" not in env
    assert "ANTHROPIC_API_KEY" not in env
    assert env["XDG_RUNTIME_DIR"] == "/run/user/1000"
    seen: list[dict[str, str]] = []

    def capture(argv: list[str], **kwargs: Any) -> Any:
        seen.append(kwargs.get("env") or dict(os.environ))
        return __import__("subprocess").CompletedProcess(argv, 0, "[]", "")

    monkeypatch.setattr("subprocess.run", capture)
    view = h.SubprocessPodman("/usr/bin/podman")
    view.containers("live")
    view.network_internal("n")
    view.remove_instance("live")
    assert seen
    for env in seen:
        assert TOKEN not in str(env)
        assert KEY not in str(env)


def test_every_subprocess_in_the_live_files_sets_its_environment() -> None:
    """A subprocess without env= would inherit both credentials from pytest."""
    import ast

    for path in (Path(h.__file__), INTEGRATION / "test_live_end_to_end.py"):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr in {"run", "Popen"}
                and getattr(node.func.value, "id", "") == "subprocess"
            ):
                keywords = {k.arg for k in node.keywords}
                assert "env" in keywords, f"{path.name}:{node.lineno}"


# -- following a container's log ----------------------------------------------------------------


@pytest.mark.parametrize(
    "script",
    [
        "print('ALLOW host=api.anthropic.com')",  # ends by itself, like a removed proxy
        "import time; print('ALLOW host=x', flush=True); time.sleep(60)",  # never ends
    ],
)
def test_a_stopped_follower_closes_its_pipe(script: str) -> None:
    """Regression (second live run): each `podman logs -f` follower left its stdout
    pipe open; the garbage collector later raised a ResourceWarning (an error
    here) inside whichever test was running, so the crash drill failed for two
    proxies' followers of the test before it."""
    import gc
    import sys
    import warnings

    def follow_and_stop() -> tuple[bool, bool]:
        follower = h._Follower([sys.executable, "-c", script])
        h.wait_for(lambda: "ALLOW host=" in follower.text(), timeout=10, interval=0.05)
        follower.stop()
        process = follower._process
        stdout = process.stdout
        return process.returncode is not None, stdout is not None and stdout.closed

    with warnings.catch_warnings():
        warnings.simplefilter("error")
        ended, closed = follow_and_stop()
        gc.collect()  # the follower is unreferenced now: nothing may warn
    assert ended  # the process ended (or was ended)
    assert closed
