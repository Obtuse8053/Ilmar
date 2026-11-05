# SPDX-License-Identifier: Apache-2.0
"""The two commands of the live procedure, offline (review of PR #50)."""

from __future__ import annotations

import json
import stat
import subprocess
import urllib.parse
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest

import live_run
import live_setup

TOKEN = "github_pat_" + "A1b2C3d4" * 8
KEY = "sk-ant-api03-" + "Zy9Xw8Vu" * 8


class Recorder:
    """A fake for subprocess: scripted answers by argv prefix, every call recorded."""

    def __init__(self, answers: dict[tuple[str, ...], tuple[int, str, str]] | None = None) -> None:
        self.answers = answers or {}
        self.calls: list[list[str]] = []
        self.kwargs: list[dict[str, Any]] = []

    def __call__(self, argv: Sequence[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        args = [a.rsplit("/", 1)[-1] if i == 0 else a for i, a in enumerate(argv)]
        self.calls.append(args)
        self.kwargs.append(kwargs)
        for prefix, (code, out, err) in self.answers.items():
            if tuple(args[: len(prefix)]) == prefix:
                return subprocess.CompletedProcess(argv, code, out, err)
        return subprocess.CompletedProcess(argv, 0, "", "")


# -- setup ---------------------------------------------------------------------------------------


def view(private: bool, topics: Sequence[str] = ()) -> tuple[int, str, str]:
    data = {"isPrivate": private, "repositoryTopics": [{"name": t} for t in topics]}
    return (0, json.dumps(data), "")


MISSING = (1, "", "GraphQL: Could not resolve to a Repository")


def test_a_missing_repository_is_created_private_with_its_topic() -> None:
    run = Recorder({("gh", "repo", "view"): MISSING})
    actions = live_setup.ensure_repository(run, "gh", "me/ilmar-disposable", description="d",
                                           topic="ilmar-disposable")  # fmt: skip
    assert actions == ["created me/ilmar-disposable (private)",
                       "added topic ilmar-disposable to me/ilmar-disposable"]  # fmt: skip
    create = next(c for c in run.calls if c[1:3] == ["repo", "create"])
    assert "--private" in create
    assert "--public" not in create


def test_an_existing_public_repository_is_refused_and_never_changed() -> None:
    run = Recorder({("gh", "repo", "view"): view(private=False, topics=["ilmar-disposable"])})
    with pytest.raises(live_setup.SetupError, match="public"):
        live_setup.ensure_repository(run, "gh", "me/ilmar-disposable", description="d",
                                     topic="ilmar-disposable")  # fmt: skip
    assert [c[1:3] for c in run.calls] == [["repo", "view"]]  # nothing was edited


def test_an_existing_private_repository_only_gets_the_missing_topic() -> None:
    run = Recorder({("gh", "repo", "view"): view(private=True)})
    actions = live_setup.ensure_repository(run, "gh", "me/ilmar-disposable", description="d",
                                           topic="ilmar-disposable")  # fmt: skip
    assert actions == ["added topic ilmar-disposable to me/ilmar-disposable"]
    assert not [c for c in run.calls if c[1:3] == ["repo", "create"]]


def test_the_token_link_prefills_exactly_the_needed_permissions() -> None:
    link = live_setup.token_link("me")
    query = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(link).query))
    assert link.startswith("https://github.com/settings/personal-access-tokens/new?")
    assert query == {
        "name": "ilmar-live",
        "description": "Ilmar live tests: only me/ilmar-disposable",
        "target_name": "me",
        "expires_in": "7",
        "contents": "write",
        "issues": "write",
        "pull_requests": "write",
    }


def test_the_settings_file_is_private_and_holds_no_secret(tmp_path: Path) -> None:
    path = tmp_path / "cfg" / "settings.env"
    live_setup.write_settings(path, live_setup.settings_text("me", "2.1.0", tmp_path / "r"))
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    settings = live_run.read_settings(path)
    assert settings["ILMAR_LIVE_REPO"] == "me/ilmar-disposable"
    assert settings["ILMAR_LIVE_OUTSIDE_REPO"] == "me/ilmar-outside-canary"
    assert settings["ILMAR_TEST_CLAUDE_IMAGE"] == "localhost/ilmar-claude-code:2.1.0"
    assert set(settings) == set(live_setup.SETTING_NAMES)


def test_setup_refuses_root_podman() -> None:
    run = Recorder({("podman", "info"): (0, "false\n", "")})
    with pytest.raises(live_setup.SetupError, match="rootless"):
        live_setup.check_host(run, which=lambda name: f"/usr/bin/{name}")


# -- run ------------------------------------------------------------------------------------------


@pytest.fixture
def settings_file(tmp_path: Path) -> Path:
    path = tmp_path / "settings.env"
    live_setup.write_settings(path, live_setup.settings_text("me", "2.1.0", tmp_path / "r"))
    return path


def answers(*values: str) -> Any:
    queue = list(values)
    return lambda prompt: queue.pop(0)


def test_run_refuses_settings_that_are_not_private_or_carry_more(settings_file: Path) -> None:
    settings_file.chmod(0o644)
    with pytest.raises(live_run.RunError, match="private"):
        live_run.read_settings(settings_file)
    settings_file.chmod(0o600)
    settings_file.write_text(settings_file.read_text() + f"ANTHROPIC_API_KEY={KEY}\n")
    with pytest.raises(live_run.RunError, match="unexpected setting"):
        live_run.read_settings(settings_file)


@pytest.mark.parametrize(
    ("given", "message"),
    [((f"ghp_{'x' * 36}", KEY), "fine-grained"), ((TOKEN, "not-a-key"), "Anthropic"),
     ((TOKEN, TOKEN), "Anthropic")],
)  # fmt: skip
def test_run_checks_the_shape_of_both_credentials(given: tuple[str, str], message: str) -> None:
    with pytest.raises(live_run.RunError, match=message) as info:
        live_run.ask_credentials(answers(*given))
    assert TOKEN not in str(info.value)
    assert KEY not in str(info.value)


def test_credentials_reach_the_tests_only_through_their_environment(
    settings_file: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PYTEST_ADDOPTS", "--showlocals")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-some-other-key-in-the-shell-xxxxxxxx")
    run = Recorder()
    code = live_run.main(["--settings", str(settings_file)], ask=answers(TOKEN, KEY), run=run)
    assert code == 0
    assert [c[-1] for c in run.calls] == [live_run.SMOKE, live_run.LIVE]
    for argv in run.calls:
        assert not [a for a in argv if TOKEN in a or KEY in a]  # never in argv
        assert "-l" not in argv
        assert "--showlocals" not in argv
        assert "addopts=" in argv
    env = run.kwargs[0]["env"]
    assert env is run.kwargs[1]["env"]
    output = capsys.readouterr()
    assert TOKEN not in output.out + output.err
    assert KEY not in output.out + output.err


def test_the_environment_drops_pytest_options_and_overrides_shell_credentials() -> None:
    base = {"PATH": "/bin", "PYTEST_ADDOPTS": "-l", "ANTHROPIC_API_KEY": "old"}
    credentials = {"ANTHROPIC_API_KEY": KEY, "ILMAR_LIVE_GITHUB_TOKEN": TOKEN}
    env = live_run.test_environment(base, {"ILMAR_LIVE_REPO": "me/ilmar-disposable"}, credentials)
    assert "PYTEST_ADDOPTS" not in env
    assert env["ANTHROPIC_API_KEY"] == KEY
    assert env["ILMAR_LIVE_REQUIRED"] == "1"


def test_a_failing_smoke_test_stops_before_the_live_tests(settings_file: Path) -> None:
    run = Recorder()

    def failing(argv: Sequence[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        run.calls.append(list(argv))
        return subprocess.CompletedProcess(argv, 1, "", "")

    code = live_run.main(["--settings", str(settings_file)], ask=answers(TOKEN, KEY), run=failing)
    assert code == 1
    assert len(run.calls) == 1
    assert run.calls[0][-1] == live_run.SMOKE


def test_records_are_posted_on_the_right_issues_without_credentials_in_reach(
    settings_file: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    reports = tmp_path / "r"
    run = Recorder()

    def tests_write_records(argv: Sequence[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        if argv[-1] == live_run.SMOKE:
            reports.mkdir(exist_ok=True)
            (reports / "issue-44-smoke-test.json").write_text('{"hosts": ["api.anthropic.com"]}')
        elif argv[-1] == live_run.LIVE:
            (reports / "end-to-end-r1.json").write_text('{"pull_request": "u"}')
        return run(argv, **kwargs)

    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/gh")
    code = live_run.main(["--settings", str(settings_file), "--post"], ask=answers(TOKEN, KEY),
                         run=tests_write_records)  # fmt: skip
    assert code == 0
    posts = [(c, k) for c, k in zip(run.calls, run.kwargs, strict=True) if c[0] == "gh"]
    assert [c[3] for c, _ in posts] == ["11", "44"]
    for argv, kwargs in posts:
        assert "--body-file" in argv  # the body goes through stdin
        assert TOKEN not in kwargs["input"]
        assert "ILMAR_LIVE_GITHUB_TOKEN" not in kwargs["env"]
        assert "ANTHROPIC_API_KEY" not in kwargs["env"]
