# SPDX-License-Identifier: Apache-2.0
"""The manual live workflow (.github/workflows/live.yml) and scripts/live_ci.py.

The workflow is the only place CI may use real credentials. These rules keep it
that way: a manual trigger only, a protected environment, no permissions by
default, the secrets in exactly one step, no untrusted input in a shell
script, and a post job that never sees the secrets.
"""

from __future__ import annotations

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


def test_the_secrets_are_behind_the_protected_environment_in_one_step() -> None:
    assert set(jobs()) == {"live", "post"}
    live = jobs()["live"]
    assert "    environment: live\n" in live
    assert "    permissions:\n      contents: read\n    env:" in live
    holders = [step for step in steps("live") if SECRET.search(step)]
    assert len(holders) == 1
    (step,) = holders
    assert step.startswith("name: Live tests\n")
    assert sorted(re.findall(r"secrets\.([A-Z_]+)", WORKFLOW)) == [
        "ANTHROPIC_API_KEY", "ILMAR_LIVE_GITHUB_TOKEN",
    ]  # fmt: skip
    assert step.rstrip().endswith("run: exec python scripts/live_ci.py")  # no shell keeps them


def test_the_post_job_never_sees_the_secrets() -> None:
    post = jobs()["post"]
    assert "environment:" not in post
    assert not SECRET.search(post)
    assert "      contents: read\n      issues: write\n" in post
    assert "needs: live" in post


def test_no_untrusted_input_is_expanded_inside_a_shell_script() -> None:
    """``${{ inputs.* }}`` in ``run:`` is a script injection; only ``env:`` may hold it."""
    for job in jobs():
        for step in steps(job):
            run = step.split("run:", 1)[1] if "run:" in step else ""
            assert "${{" not in run.replace("${{ runner.temp }}", ""), step.splitlines()[0]


def test_no_job_persists_the_checkout_token() -> None:
    for job in jobs():
        for step in steps(job):
            if "actions/checkout@" in step:
                assert "persist-credentials: false" in step


def test_only_records_leave_the_live_job() -> None:
    (upload,) = [s for s in steps("live") if "actions/upload-artifact@" in s]
    assert "path: ${{ runner.temp }}/live-reports/*.json" in upload


# -- scripts/live_ci.py ------------------------------------------------------------------


def actions(**extra: str) -> dict[str, str]:
    return {
        "GITHUB_ACTIONS": "true",
        "GITHUB_REPOSITORY_OWNER": "Obtuse8053",
        "ILMAR_TEST_CLAUDE_VERSION": "2.1.0",
        "RUNNER_TEMP": "/tmp/runner",
        "PATH": "/usr/bin",
        **extra,
    }


class Recorder:
    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        self.kwargs: list[dict[str, Any]] = []

    def __call__(self, argv: Sequence[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        self.calls.append(list(argv))
        # A copy: the script clears its environment (the credentials) after the run.
        self.kwargs.append({**kwargs, "env": dict(kwargs["env"])} if "env" in kwargs else kwargs)
        out = "true\n" if list(argv[1:3]) == ["info", "--format"] else ""
        return subprocess.CompletedProcess(argv, 0, out, "")


@pytest.fixture(autouse=True)
def _tools(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("shutil.which", lambda name: f"/usr/bin/{name}")


def test_it_refuses_to_run_outside_github_actions(capsys: pytest.CaptureFixture[str]) -> None:
    env = actions(ILMAR_LIVE_GITHUB_TOKEN=TOKEN, ANTHROPIC_API_KEY=KEY)
    del env["GITHUB_ACTIONS"]
    run = Recorder()
    assert live_ci.main([], environ=env, run=run) == 1
    assert run.calls == []
    assert "scripts/live_run.py" in capsys.readouterr().err


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
    with pytest.raises(live_run.RunError, match=r"x\.y\.z"):
        live_ci.ci_settings(actions(ILMAR_TEST_CLAUDE_VERSION="latest; rm -rf /"))


def test_it_runs_the_same_stages_as_live_run_and_keeps_secrets_out_of_argv(
    capsys: pytest.CaptureFixture[str],
) -> None:
    run = Recorder()
    env = actions(ILMAR_LIVE_GITHUB_TOKEN=TOKEN, ANTHROPIC_API_KEY=KEY, PYTEST_ADDOPTS="-l")
    code = live_ci.main([], environ=env, run=run)
    assert code == 1  # the fake tests write no records: a skip is never a pass
    tests = [c for c in run.calls if c[0].endswith(("python", "python3")) or "pytest" in c]
    assert [c[-1] for c in tests] == [live_run.SMOKE]
    for argv in run.calls:
        assert not [a for a in argv if TOKEN in a or KEY in a]
    test_env = run.kwargs[run.calls.index(tests[0])]["env"]
    assert "PYTEST_ADDOPTS" not in test_env
    assert test_env["ILMAR_LIVE_REQUIRED"] == "1"
    output = capsys.readouterr()
    assert TOKEN not in output.out + output.err
    assert KEY not in output.out + output.err


def test_post_mode_needs_records_and_actions(tmp_path: Path) -> None:
    assert live_ci.main(["--post", str(tmp_path)], environ=actions(), run=Recorder()) == 1
    (tmp_path / "issue-44-smoke-test.json").write_text('{"ok": true}')
    outside = actions()
    del outside["GITHUB_ACTIONS"]
    assert live_ci.main(["--post", str(tmp_path)], environ=outside, run=Recorder()) == 1
