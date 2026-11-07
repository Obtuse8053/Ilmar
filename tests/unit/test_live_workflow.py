# SPDX-License-Identifier: Apache-2.0
"""The manual live workflow (.github/workflows/live.yml) and scripts/live_ci.py.

The workflow is the only place CI may use real credentials. These rules keep it
that way: a manual trigger only, a protected environment, no permissions by
default, the secrets in exactly one step, no untrusted input in a shell
script, and a post job that never sees the secrets.
"""

from __future__ import annotations

import json
import re
import subprocess
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest

import live_ci
import live_run

ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = (ROOT / ".github" / "workflows" / "live.yml").read_text()
TOKEN = "github_pat_" + "A1b2C3d4" * 8
KEY = "sk-ant-api03-" + "Zy9Xw8Vu" * 8
SECRET = re.compile(r"\$\{\{\s*secrets\.")


def jobs() -> dict[str, str]:
    """Each job's block of the workflow, by name."""
    body = WORKFLOW.split("\njobs:\n", 1)[1]
    parts = re.split(r"^  ([a-z][a-z0-9_-]*):\n", body, flags=re.MULTILINE)
    return dict(zip(parts[1::2], parts[2::2], strict=True))


def steps(job: str) -> list[str]:
    return re.split(r"^      - ", jobs()[job], flags=re.MULTILINE)[1:]


def test_it_runs_only_when_started_by_hand() -> None:
    triggers = WORKFLOW.split("\non:\n", 1)[1].split("\n\n", 1)[0]
    assert re.findall(r"^  ([a-z_]+):", triggers, flags=re.MULTILINE) == ["workflow_dispatch"]


def test_nothing_is_permitted_unless_a_job_asks() -> None:
    assert re.search(r"^permissions: \{\}$", WORKFLOW, flags=re.MULTILINE)
    assert "concurrency:\n  group: live-tests\n  cancel-in-progress: false" in WORKFLOW


def test_the_secrets_are_behind_the_protected_environment_in_two_steps() -> None:
    """The live tests and their cleanup; both exec into Python, so no shell keeps them."""
    assert set(jobs()) == {"guard", "live", "post"}
    live = jobs()["live"]
    assert "    environment: live\n" in live
    assert "    needs: guard\n" in live
    assert "    permissions:\n      contents: read\n    steps:" in live
    holders = [step for step in steps("live") if SECRET.search(step)]
    assert [h.splitlines()[0] for h in holders] == [
        "name: Live tests", "name: Clean up after a failure or cancellation",
    ]  # fmt: skip
    for step in holders:
        assert re.search(r"run: exec python scripts/live_ci\.py( --cleanup)?\n?$", step.rstrip())
    assert sorted(set(re.findall(r"secrets\.([A-Z_]+)", WORKFLOW))) == [
        "ANTHROPIC_API_KEY", "ILMAR_LIVE_GITHUB_TOKEN",
    ]  # fmt: skip


def test_the_commit_is_checked_before_any_secret() -> None:
    names = [s.splitlines()[0] for s in steps("live")]
    check = names.index("name: Check the context and the commit again (before any secret)")
    first_secret = next(i for i, s in enumerate(steps("live")) if SECRET.search(s))
    assert check < first_secret
    assert "run: python scripts/live_ci.py --guard-context" in steps("live")[check]


def test_the_guard_runs_first_and_never_sees_a_secret() -> None:
    guard = jobs()["guard"]
    assert "environment:" not in guard
    assert not SECRET.search(guard)
    assert "if: github.ref == 'refs/heads/main'" in guard
    assert "      actions: read\n      contents: read\n" in guard
    assert "run: python3 scripts/live_ci.py --guard" in guard


def test_every_job_runs_only_on_main() -> None:
    for name, job in jobs().items():
        assert "github.ref == 'refs/heads/main'" in job, name


def test_the_post_job_never_sees_the_secrets_and_needs_a_passed_run() -> None:
    post = jobs()["post"]
    assert "environment:" not in post
    assert not SECRET.search(post)
    assert "      contents: read\n      issues: write\n" in post
    assert "needs: live" in post
    assert "if: success() && inputs.post_records" in post


def test_every_action_is_pinned_to_a_full_commit() -> None:
    uses = re.findall(r"uses: (\S+)", WORKFLOW)
    assert uses
    for use in uses:
        assert re.fullmatch(r"actions/[a-z-]+@[0-9a-f]{40}", use), use


def test_the_live_job_installs_only_exact_versions_without_resolving() -> None:
    (install,) = [s for s in steps("live") if s.startswith("name: Install")]
    assert '".[dev]"' not in install
    packages = re.findall(r"([a-z][a-z0-9_-]*)==([0-9.]+)", install)
    assert {name for name, _ in packages} == {"pytest", "pluggy", "iniconfig", "packaging",
                                              "pygments"}  # fmt: skip
    assert install.count("--no-deps") == 2


def test_images_from_registries_are_pinned_by_digest() -> None:
    assert f"podman pull {live_ci.PROXY_IMAGE}" in WORKFLOW
    assert re.fullmatch(r"[a-z0-9./:-]+@sha256:[0-9a-f]{64}", live_ci.PROXY_IMAGE)


def test_cleanup_runs_after_failures_and_cancellations() -> None:
    names = {s.splitlines()[0]: s for s in steps("live")}
    assert "if: failure() || cancelled()" in names["name: Clean up after a failure or cancellation"]
    assert "if: always()" in names["name: Remove every container (no secrets)"]
    assert "if: always()" in names["name: Keep the records (no credentials)"]


def test_inputs_are_only_expanded_in_the_environment() -> None:
    assert WORKFLOW.count("${{ inputs.") == 2  # the two env entries, nowhere else
    assert "ILMAR_EXPECTED_SHA: ${{ inputs.expected_sha }}" in WORKFLOW
    assert "ILMAR_TEST_CLAUDE_VERSION: ${{ inputs.claude_version }}" in WORKFLOW


# -- scripts/live_ci.py ------------------------------------------------------------------


SHA = "a" * 40


def actions(**extra: str) -> dict[str, str]:
    return {
        "GITHUB_ACTIONS": "true",
        "GITHUB_EVENT_NAME": "workflow_dispatch",
        "GITHUB_REF": "refs/heads/main",
        "GITHUB_SHA": SHA,
        "ILMAR_EXPECTED_SHA": SHA,
        "GITHUB_REPOSITORY": "Obtuse8053/Ilmar",
        "GITHUB_REPOSITORY_OWNER": "Obtuse8053",
        "GITHUB_RUN_ID": "77",
        "ILMAR_TEST_CLAUDE_VERSION": "2.1.0",
        "RUNNER_TEMP": "/tmp/runner",
        "PATH": "/usr/bin",
        **extra,
    }


class Recorder:
    def __init__(self, head: str = SHA) -> None:
        self.calls: list[list[str]] = []
        self.kwargs: list[dict[str, Any]] = []
        self.head = head

    def __call__(self, argv: Sequence[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        self.calls.append(list(argv))
        # A copy: the script clears its environment (the credentials) after the run.
        self.kwargs.append({**kwargs, "env": dict(kwargs["env"])} if "env" in kwargs else kwargs)
        out = ""
        if list(argv[1:3]) == ["info", "--format"]:
            out = "true\n"
        elif "rev-parse" in argv:
            out = self.head + "\n"
        return subprocess.CompletedProcess(argv, 0, out, "")


@pytest.fixture(autouse=True)
def _tools(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("shutil.which", lambda name: f"/usr/bin/{name}")


GOOD_ENVIRONMENT = {
    "name": "live",
    "can_admins_bypass": False,
    "protection_rules": [
        {"type": "required_reviewers", "prevent_self_review": False,
         "reviewers": [{"type": "User", "reviewer": {"login": "Obtuse8053"}}]},
        {"type": "branch_policy"},
    ],
    "deployment_branch_policy": {"protected_branches": False, "custom_branch_policies": True},
}  # fmt: skip
GOOD_POLICIES = {"total_count": 1, "branch_policies": [{"name": "main", "type": "branch"}]}


GOOD_RULES = [{"type": "pull_request"}, {"type": "non_fast_forward"}, {"type": "deletion"}]


def fake_api(environment: dict[str, Any], policies: dict[str, Any], rules: Any = None) -> Any:
    def get(path: str, token: str) -> Any:
        assert token == "actions-token"
        if path.endswith("/rules/branches/main"):
            return GOOD_RULES if rules is None else rules
        return policies if path.endswith("/deployment-branch-policies") else environment

    return get


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"GITHUB_EVENT_NAME": "push"}, "workflow_dispatch"),
        ({"GITHUB_REF": "refs/heads/attacker"}, "refs/heads/main"),
        ({"GITHUB_SHA": "b" * 40}, "expected commit"),
        ({"ILMAR_EXPECTED_SHA": "main"}, "40-character"),
        ({"ILMAR_TEST_CLAUDE_VERSION": "latest"}, "x.y.z"),
        ({"GITHUB_ACTIONS": "false"}, "GitHub Actions"),
    ],
)
def test_the_context_must_be_a_manual_run_of_the_expected_commit_on_main(
    change: dict[str, str], message: str
) -> None:
    problems = live_ci.check_context(actions(**change))
    assert any(message in p for p in problems), problems


def test_a_correctly_protected_environment_passes_the_guard(
    capsys: pytest.CaptureFixture[str],
) -> None:
    get = fake_api(GOOD_ENVIRONMENT, GOOD_POLICIES)
    assert live_ci.main(["--guard"], environ=actions(GH_TOKEN="actions-token"), get=get) == 0
    assert "as required" in capsys.readouterr().out


def _without_reviewers() -> dict[str, Any]:
    return {**GOOD_ENVIRONMENT, "protection_rules": [{"type": "branch_policy"}]}


@pytest.mark.parametrize(
    ("environment", "policies", "message"),
    [
        (_without_reviewers(), GOOD_POLICIES, "no required reviewers"),
        ({**GOOD_ENVIRONMENT, "protection_rules": [{"type": "required_reviewers",
                                                     "reviewers": []}]},
         GOOD_POLICIES, "no required reviewers"),
        ({**GOOD_ENVIRONMENT, "can_admins_bypass": True}, GOOD_POLICIES, "bypass"),
        ({k: v for k, v in GOOD_ENVIRONMENT.items() if k != "can_admins_bypass"},
         GOOD_POLICIES, "bypass"),
        ({**GOOD_ENVIRONMENT, "deployment_branch_policy": None}, GOOD_POLICIES,
         "selected branches"),
        ({**GOOD_ENVIRONMENT, "deployment_branch_policy": {"protected_branches": True,
                                                           "custom_branch_policies": False}},
         GOOD_POLICIES, "selected branches"),
        (GOOD_ENVIRONMENT, {"branch_policies": [{"name": "*", "type": "branch"}]}, "exactly main"),
        (GOOD_ENVIRONMENT, {"branch_policies": [{"name": "main", "type": "branch"},
                                                {"name": "dev", "type": "branch"}]},
         "exactly main"),
        (GOOD_ENVIRONMENT, {"branch_policies": [{"name": "main", "type": "tag"}]},
         "exactly main"),
    ],
)  # fmt: skip
def test_a_weaker_environment_is_refused(
    environment: dict[str, Any], policies: dict[str, Any], message: str,
    capsys: pytest.CaptureFixture[str],
) -> None:  # fmt: skip
    get = fake_api(environment, policies)
    assert live_ci.main(["--guard"], environ=actions(GH_TOKEN="actions-token"), get=get) == 1
    assert message in capsys.readouterr().err


@pytest.mark.parametrize(
    "rules",
    [[], [{"type": "pull_request"}], [{"type": "non_fast_forward"}, {"type": "deletion"}],
     {"message": "not a list"}],
)  # fmt: skip
def test_main_must_change_only_through_pull_requests(
    rules: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    get = fake_api(GOOD_ENVIRONMENT, GOOD_POLICIES, rules)
    assert live_ci.main(["--guard"], environ=actions(GH_TOKEN="actions-token"), get=get) == 1
    assert "main" in capsys.readouterr().err


def test_an_unreadable_environment_is_refused(capsys: pytest.CaptureFixture[str]) -> None:
    def missing(path: str, token: str) -> Any:
        raise live_run.RunError(f"GET {path}: HTTP 404")

    assert live_ci.main(["--guard"], environ=actions(GH_TOKEN="actions-token"), get=missing) == 1
    assert "cannot read the 'live' environment" in capsys.readouterr().err


def test_a_redirect_is_never_followed_with_the_token() -> None:
    handler = live_ci._NoRedirects()
    assert handler.redirect_request(None, None, 302, "Found", {}, "https://evil.example/") is None


def test_it_refuses_to_run_outside_github_actions(capsys: pytest.CaptureFixture[str]) -> None:
    env = actions(ILMAR_LIVE_GITHUB_TOKEN=TOKEN, ANTHROPIC_API_KEY=KEY)
    del env["GITHUB_ACTIONS"]
    run = Recorder()
    assert live_ci.main([], environ=env, run=run) == 1
    assert run.calls == []
    assert "GitHub Actions" in capsys.readouterr().err


def test_a_different_checked_out_tree_is_refused_before_any_secret_is_read() -> None:
    run = Recorder(head="b" * 40)
    env = actions(ILMAR_LIVE_GITHUB_TOKEN=TOKEN, ANTHROPIC_API_KEY=KEY)
    assert live_ci.main([], environ=env, run=run) == 1
    assert not [c for c in run.calls if "pytest" in c]
    assert live_ci.main(["--guard-context"], environ=env, run=run) == 1


def test_missing_secrets_are_named_never_shown(capsys: pytest.CaptureFixture[str]) -> None:
    assert live_ci.main([], environ=actions(ILMAR_LIVE_GITHUB_TOKEN=TOKEN), run=Recorder()) == 1
    err = capsys.readouterr().err
    assert "ANTHROPIC_API_KEY" in err
    assert TOKEN not in err


def test_the_settings_follow_the_repository_owner() -> None:
    settings = live_ci.ci_settings(actions())
    assert settings["ILMAR_LIVE_REPO"] == "Obtuse8053/ilmar-disposable"
    assert settings["ILMAR_LIVE_OUTSIDE_REPO"] == "Obtuse8053/ilmar-outside-canary"
    assert settings["ILMAR_LIVE_REPORT_DIR"] == "/tmp/runner/live-reports"
    assert settings["ILMAR_TEST_PODMAN_PROXY_IMAGE"] == live_ci.PROXY_IMAGE
    with pytest.raises(live_run.RunError, match=r"x\.y\.z"):
        live_ci.ci_settings(actions(ILMAR_TEST_CLAUDE_VERSION="latest; rm -rf /"))


def test_a_failed_run_writes_a_failed_summary_bound_to_its_commit(
    tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:  # fmt: skip
    run = Recorder()
    env = actions(ILMAR_LIVE_GITHUB_TOKEN=TOKEN, ANTHROPIC_API_KEY=KEY, PYTEST_ADDOPTS="-l",
                  RUNNER_TEMP=str(tmp_path))  # fmt: skip
    code = live_ci.main([], environ=env, run=run)
    assert code == 1  # the fake tests write no records: a skip is never a pass
    tests = [c for c in run.calls if "pytest" in c]
    assert [c[-1] for c in tests] == [live_run.SMOKE]
    for argv in run.calls:
        assert not [a for a in argv if TOKEN in a or KEY in a]
    test_env = run.kwargs[run.calls.index(tests[0])]["env"]
    assert "PYTEST_ADDOPTS" not in test_env
    assert test_env["ILMAR_LIVE_REQUIRED"] == "1"
    summary = json.loads((tmp_path / "live-reports" / "run-summary.json").read_text())
    assert summary["outcome"] == "failed"
    assert summary["commit"] == SHA
    output = capsys.readouterr()
    assert TOKEN not in output.out + output.err
    assert KEY not in output.out + output.err


def test_a_summary_holding_a_credential_is_never_written(tmp_path: Path) -> None:
    with pytest.raises(live_run.RunError):
        live_ci.write_summary(tmp_path, {"note": KEY[-30:] + "x"}, [KEY[-30:] + "x"])
    with pytest.raises(live_run.RunError):
        live_ci.write_summary(tmp_path, {"note": TOKEN}, [])  # by its shape alone
    assert list(tmp_path.iterdir()) == []


def records(directory: Path, *, outcome: str = "passed", commit: str = SHA) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "issue-44-smoke-test.json").write_text('{"hosts": ["api.anthropic.com"]}')
    (directory / "end-to-end-1107-ab12cd.json").write_text('{"pull_request": "u"}')
    (directory / "run-summary.json").write_text(json.dumps({"outcome": outcome, "commit": commit}))
    return directory


def test_only_checked_records_of_a_passed_run_of_this_commit_are_posted(tmp_path: Path) -> None:
    good = records(tmp_path / "good")
    assert [p.name for p in live_ci.checked_records(good, SHA)] == [
        "end-to-end-1107-ab12cd.json", "issue-44-smoke-test.json", "run-summary.json",
    ]  # fmt: skip


@pytest.mark.parametrize(
    ("damage", "message"),
    [
        (lambda d: (d / "run-summary.json").unlink(), "did not pass"),
        (lambda d: (d / "run-summary.json").write_text('{"outcome": "failed", "commit": "'
                                                       + SHA + '"}'), "did not pass"),
        (lambda d: (d / "run-summary.json").write_text('{"outcome": "passed", "commit": "'
                                                       + "b" * 40 + '"}'), "not from this commit"),
        (lambda d: (d / "notes.txt").write_text("x"), "unexpected file"),
        (lambda d: (d / "preflight.json").write_text(f'{{"t": "{TOKEN}"}}'), "credential"),
        (lambda d: (d / "preflight.json").write_text('{"x": "' + "y" * 70000 + '"}'),
         "too large"),
        (lambda d: (d / "preflight.json").write_text("not json"), "Expecting value"),
    ],
)  # fmt: skip
def test_anything_else_in_the_records_stops_the_post(
    tmp_path: Path, damage: Any, message: str
) -> None:
    directory = records(tmp_path / "r")
    damage(directory)
    with pytest.raises((live_run.RunError, ValueError), match=message):
        live_ci.checked_records(directory, SHA)


def test_post_mode_needs_the_context_and_records(tmp_path: Path) -> None:
    directory = records(tmp_path / "r")
    run = Recorder()
    outside = actions()
    del outside["GITHUB_ACTIONS"]
    assert live_ci.main(["--post", str(directory)], environ=outside, run=run) == 1
    assert live_ci.main(["--post", str(tmp_path / "empty")], environ=actions(), run=run) == 1
    assert [c for c in run.calls if c[0].endswith("gh")] == []


class FakeRepo:
    """Answers the cleanup's GitHub calls; records what it changed."""

    def __init__(self, repo: dict[str, Any]) -> None:
        self.repo = repo
        self.changed: list[str] = []

    def __call__(self, request: Any) -> Any:
        from ilmar.adapters.github.client import HttpResponse

        path = request.url.removeprefix("https://api.github.com")
        if request.method != "GET":
            self.changed.append(f"{request.method} {path}")
            return HttpResponse(204, {}, b"")
        body: Any = {}
        if path == "/repos/Obtuse8053/ilmar-disposable":
            body = self.repo
        elif "/git/matching-refs/heads/ilmar-live/" in path:
            body = [{"ref": "refs/heads/ilmar-live/1107-ab12cd"}]
        elif path.startswith("/repos/Obtuse8053/ilmar-disposable/labels"):
            body = [{"name": "ilmar-live:1107-ab12cd"}, {"name": "bug"}]
        elif "/issues?" in path:
            body = [{"number": 5}]
        elif "/pulls?" in path:
            body = [{"number": 6, "state": "open", "head": {"ref": "ilmar/task-5"}}]
        return HttpResponse(200, {}, json.dumps(body).encode())


DISPOSABLE = {"name": "ilmar-disposable", "private": True, "topics": ["ilmar-disposable"]}


def test_cleanup_removes_only_live_run_leftovers(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeRepo(DISPOSABLE)
    monkeypatch.setattr("ilmar.adapters.github.client.urllib_transport", fake)
    env = actions(ILMAR_LIVE_GITHUB_TOKEN=TOKEN, ANTHROPIC_API_KEY=KEY)
    assert live_ci.main(["--cleanup"], environ=env, run=Recorder()) == 0
    assert fake.changed == [
        "PATCH /repos/Obtuse8053/ilmar-disposable/issues/5",
        "DELETE /repos/Obtuse8053/ilmar-disposable/labels/ilmar-live%3A1107-ab12cd",
        "PATCH /repos/Obtuse8053/ilmar-disposable/pulls/6",
        "DELETE /repos/Obtuse8053/ilmar-disposable/git/refs/heads/ilmar/task-5",
        "DELETE /repos/Obtuse8053/ilmar-disposable/git/refs/heads/ilmar-live/1107-ab12cd",
    ]
    assert not [c for c in fake.changed if "/bug" in c]


@pytest.mark.parametrize("change", [{"private": False}, {"topics": []}, {"name": "Ilmar"}])
def test_cleanup_touches_nothing_outside_the_disposable_repository(
    monkeypatch: pytest.MonkeyPatch, change: dict[str, Any]
) -> None:
    fake = FakeRepo({**DISPOSABLE, **change})
    monkeypatch.setattr("ilmar.adapters.github.client.urllib_transport", fake)
    env = actions(ILMAR_LIVE_GITHUB_TOKEN=TOKEN, ANTHROPIC_API_KEY=KEY)
    assert live_ci.main(["--cleanup"], environ=env, run=Recorder()) == 1
    assert fake.changed == []
