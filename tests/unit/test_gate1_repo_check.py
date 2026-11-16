# SPDX-License-Identifier: Apache-2.0
"""``scripts/gate1_repo_check.py``: the disposable repository's pre-trial check (#93).

Every path of every commit is inspected, the mirror must be of the live
repository as it is now, settings answers that are missing or malformed fail
closed, and there is no way to skip a check.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

import pytest

import gate1_repo_check as check
from ilmar.domain.events import looks_like_a_credential

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="needs git")
TEMPLATE = Path(__file__).resolve().parents[2] / "deploy" / "gate1" / "disposable"
GIT = shutil.which("git") or "git"
REPO = "o/d"


def git(cwd: Path, *args: str) -> str:
    done = subprocess.run(  # noqa: S603 - fixed argv: git and test arguments
        [GIT, "-c", "user.name=t", "-c", "user.email=t@example.com",
         "-c", "init.defaultBranch=main", *args],
        cwd=cwd, check=True, capture_output=True, text=True,
    )  # fmt: skip
    return done.stdout.strip()


def repository(tmp: Path, *commits: dict[str, str | None]) -> tuple[Path, Path]:
    """A work repository with one commit per mapping (``None`` removes a file),
    and a fresh mirror of it whose origin looks like GitHub's."""
    work = tmp / "work"
    work.mkdir()
    git(work, "init", "-q")
    for files in commits:
        for name, text in files.items():
            target = work / name
            if text is None:
                target.unlink()
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text)
        git(work, "add", "-A")
        git(work, "commit", "-q", "--allow-empty", "-m", "c")
    mirror = tmp / "mirror.git"
    git(tmp, "clone", "-q", "--mirror", str(work), str(mirror))
    git(tmp, "--git-dir", str(mirror), "config", "remote.origin.url",
        f"https://github.com/{REPO}.git")  # fmt: skip
    return work, mirror


def template() -> dict[str, str | None]:
    files = [p for p in TEMPLATE.rglob("*") if p.is_file() and "__pycache__" not in p.parts]
    return {str(p.relative_to(TEMPLATE)): p.read_text() for p in files}


def history(mirror: Path) -> list[dict[str, str]]:
    return check.check_history(mirror, looks_like=looks_like_a_credential)


# -- history ------------------------------------------------------------------------------


def test_the_shipped_template_passes(tmp_path: Path) -> None:
    _, mirror = repository(tmp_path, template())
    assert history(mirror) == []


def test_a_credential_anywhere_in_history_is_found_and_never_echoed(tmp_path: Path) -> None:
    secret = "sk-ant-api03-" + "x" * 40
    _, mirror = repository(tmp_path, {"calc.py": "x = 1\n", "old.env": f"KEY={secret}\n"},
                           {"old.env": None})  # removed at the tip, still in history  # fmt: skip
    findings = history(mirror)
    assert [(f["path"], f["kind"]) for f in findings] == [("old.env", "credential shape")]
    assert secret not in json.dumps(findings)


@pytest.mark.parametrize(
    "path", [".github/workflows/ci.yml", ".claude/settings.json", ".mcp.json", ".gitmodules",
             ".gitattributes", "CLAUDE.md", "AGENTS.md", "sub/.claude/x.json"],
)  # fmt: skip
def test_workflows_agent_settings_submodules_and_filters_are_refused(
    tmp_path: Path, path: str
) -> None:
    _, mirror = repository(tmp_path, {"calc.py": "x = 1\n", path: "{}\n"})
    assert ("forbidden path", path) in [(f["kind"], f["path"]) for f in history(mirror)]


def test_a_forbidden_path_sharing_an_object_with_an_allowed_one_is_found(
    tmp_path: Path,
) -> None:
    """One object, two paths: listing objects would name it once, under either."""
    same = "name: x\non: push\n"
    _, mirror = repository(tmp_path, {"notes/a.yml": same, ".github/workflows/ci.yml": same})
    assert ("forbidden path", ".github/workflows/ci.yml") in [
        (f["kind"], f["path"]) for f in history(mirror)
    ]


def test_a_workflow_renamed_away_is_still_found_in_history(tmp_path: Path) -> None:
    workflow = "on: push\njobs: {}\n"
    _, mirror = repository(tmp_path, {".github/workflows/ci.yml": workflow},
                           {".github/workflows/ci.yml": None, "docs/ci.yml": workflow})  # fmt: skip
    assert ("forbidden path", ".github/workflows/ci.yml") in [
        (f["kind"], f["path"]) for f in history(mirror)
    ]


def test_a_submodule_entry_is_refused(tmp_path: Path) -> None:
    work, _ = repository(tmp_path, {"calc.py": "x = 1\n"})
    head = git(work, "rev-parse", "HEAD")
    git(work, "update-index", "--add", "--cacheinfo", f"160000,{head},vendor/lib")
    git(work, "commit", "-q", "-m", "gitlink")
    mirror = tmp_path / "m2.git"
    git(tmp_path, "clone", "-q", "--mirror", str(work), str(mirror))
    assert ("submodule", "vendor/lib") in [(f["kind"], f["path"]) for f in history(mirror)]


# -- settings -----------------------------------------------------------------------------

GOOD: dict[str, Any] = {
    f"repos/{REPO}": {"full_name": REPO, "id": 42, "private": True, "fork": False,
                      "topics": ["ilmar-disposable"], "default_branch": "main"},
    f"repos/{REPO}/actions/secrets": {"total_count": 0},
    f"repos/{REPO}/actions/variables": {"total_count": 0},
    f"repos/{REPO}/environments": {"total_count": 0},
}  # fmt: skip


def gh(answers: dict[str, Any], fallback: Callable[..., Any] | None = None) -> Callable[..., Any]:
    def run(argv: Sequence[str], data: bytes | None = None) -> subprocess.CompletedProcess[bytes]:
        if Path(argv[0]).name != "gh":
            assert fallback is not None
            return fallback(argv, data)  # type: ignore[no-any-return]
        answer = answers.get(argv[-1], "missing")
        if answer == "missing":
            return subprocess.CompletedProcess(argv, 1, b"", b"HTTP 404")
        body = answer if isinstance(answer, bytes) else json.dumps(answer).encode()
        return subprocess.CompletedProcess(argv, 0, body, b"")

    return run


def test_good_settings_pass_and_give_the_immutable_id() -> None:
    facts, failures = check.check_settings(REPO, run=gh(GOOD))
    assert (facts["repository_id"], failures) == (42, [])


def test_bad_settings_are_reported() -> None:
    answers = {**GOOD, f"repos/{REPO}": {**GOOD[f"repos/{REPO}"], "private": False,
                                          "topics": []},
               f"repos/{REPO}/actions/secrets": {"total_count": 1}}  # fmt: skip
    _, failures = check.check_settings(REPO, run=gh(answers))
    assert len(failures) == 3


@pytest.mark.parametrize(
    ("path", "answer"),
    [
        (f"repos/{REPO}/actions/secrets", {}),  # no total_count: never taken as none
        (f"repos/{REPO}/actions/variables", {"total_count": "0"}),
        (f"repos/{REPO}/environments", b"<html>"),
        (f"repos/{REPO}/environments", "missing"),  # the call failed
        (f"repos/{REPO}", {"full_name": REPO}),  # no ID
        (f"repos/{REPO}", {**GOOD[f"repos/{REPO}"], "full_name": "o/other"}),
    ],
)
def test_a_missing_or_malformed_answer_fails_closed(path: str, answer: Any) -> None:
    with pytest.raises(check.CheckError):
        check.check_settings(REPO, run=gh({**GOOD, path: answer}))


def test_missing_visibility_or_topics_is_a_failure_not_a_pass() -> None:
    repo = {k: v for k, v in GOOD[f"repos/{REPO}"].items() if k not in ("private", "topics")}
    _, failures = check.check_settings(REPO, run=gh({**GOOD, f"repos/{REPO}": repo}))
    assert len(failures) == 2


# -- provenance and the whole command -----------------------------------------------------


def refs_of(work: Path) -> list[dict[str, Any]]:
    head = git(work, "rev-parse", "HEAD")
    return [{"ref": "refs/heads/main", "object": {"sha": head}}]


def answers_for(work: Path, **changes: Any) -> dict[str, Any]:
    return {**GOOD, f"repos/{REPO}/git/matching-refs/heads?per_page=100": refs_of(work),
            **changes}  # fmt: skip


def test_a_matching_fresh_mirror_passes_end_to_end(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    work, mirror = repository(tmp_path, template())
    code = check.main(["--mirror", str(mirror), "--repository", REPO,
                       "--report", str(tmp_path / "r.json")],
                      run=gh(answers_for(work), check._run))  # fmt: skip
    report = json.loads((tmp_path / "r.json").read_text())
    assert (code, report["passed"], report["repository_id"]) == (0, True, 42)
    assert "cannot prove the absence of arbitrary secrets" in report["limitations"]


def test_a_stale_mirror_fails(tmp_path: Path) -> None:
    work, mirror = repository(tmp_path, template())
    git(work, "commit", "-q", "--allow-empty", "-m", "newer")  # the repository moved on
    code = check.main(["--mirror", str(mirror), "--repository", REPO],
                      run=gh(answers_for(work), check._run))  # fmt: skip
    assert code == 1


def test_a_mirror_of_another_repository_fails(tmp_path: Path) -> None:
    work, mirror = repository(tmp_path, template())
    git(tmp_path, "--git-dir", str(mirror), "config", "remote.origin.url",
        "https://github.com/someone/else.git")  # fmt: skip
    _, failures = check.check_provenance(mirror, REPO, run=gh(answers_for(work), check._run))
    assert failures
    assert "origin" in failures[0]


@pytest.mark.parametrize(
    "refs",
    [[{"ref": f"refs/heads/b{n}", "object": {"sha": "0" * 40}} for n in range(100)],
     [{"ref": "refs/heads/main"}], {"message": "Not Found"}, []],
)  # fmt: skip
def test_an_incomplete_or_malformed_branch_list_cannot_pass(tmp_path: Path, refs: Any) -> None:
    work, mirror = repository(tmp_path, template())
    answers = answers_for(work, **{f"repos/{REPO}/git/matching-refs/heads?per_page=100": refs})
    code = check.main(["--mirror", str(mirror), "--repository", REPO],
                      run=gh(answers, check._run))  # fmt: skip
    assert code == 2


def test_there_is_no_option_to_skip_a_check(tmp_path: Path) -> None:
    for flag in ("--skip-settings", "--skip-history", "--skip-provenance", "--force"):
        with pytest.raises(SystemExit):
            check.main(["--mirror", str(tmp_path), "--repository", REPO, flag])
