# SPDX-License-Identifier: Apache-2.0
"""The live-test harness, offline (#11d): guards, leak detection, watching, configuration.

No network, no Podman, no credentials: GitHub is a scripted transport, Podman a
fake view, and every "secret" below is made up.
"""

from __future__ import annotations

import base64
import json
import stat
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
        }
    )
    headers: dict[str, str] = field(
        default_factory=lambda: {
            "GitHub-Authentication-Token-Expiration": expiry(NOW + timedelta(days=6))
        }
    )
    statuses: dict[tuple[str, str], int] = field(default_factory=dict)
    requests: list[HttpRequest] = field(default_factory=list)

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
        if key[1].endswith(("/actions/permissions", "/actions/runs")):
            return HttpResponse(403, {}, b'{"message":"Resource not accessible"}')
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
    assert len(result.checks) == 6
    probe = [p for p in fake.calls("DELETE") if "probe" in p]
    assert probe == [
        f"/repos/owner/ilmar-disposable/git/refs/heads/ilmar-live/probe-{NOW:%Y%m%d%H%M%S}"
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
    with pytest.raises(h.LiveGuardError, match="workflow"):
        run_preflight(fake)
    assert any("probe" in p for p in fake.calls("DELETE"))


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


def test_a_run_gets_a_new_base_branch_and_issue_and_rewrites_nothing() -> None:
    fake = FakeGitHub()
    live = settings()
    run = h.seed_run(h.LiveGitHub(live.github_token, fake), live.repository, "main", "r-0001ab")
    assert run == h.LiveRun("r-0001ab", "ilmar-live/r-0001ab", "ilmar-live:r-0001ab", 7)
    refs = [json.loads(r.body or b"{}") for r in fake.requests if r.url.endswith("/git/refs")]
    assert refs == [{"ref": "refs/heads/ilmar-live/r-0001ab", "sha": "c" * 40}]
    assert fake.calls("PATCH") == []  # no existing ref is moved, nothing is forced
    commit = next(r for r in fake.requests if r.url.endswith("/git/commits"))
    assert json.loads(commit.body or b"{}")["parents"] == ["a" * 40]


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
    workflows = Path(__file__).resolve().parents[2] / ".github" / "workflows"
    ci = "\n".join(p.read_text() for p in workflows.glob("*.yml"))
    for name in LIVE_FILES:
        source = (INTEGRATION / name).read_text()
        assert "pytestmark = pytest.mark.integration" in source
        assert name not in ci
