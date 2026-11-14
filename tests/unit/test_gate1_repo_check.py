# SPDX-License-Identifier: Apache-2.0
"""``scripts/gate1_repo_check.py``: the disposable repository's pre-trial check (#93)."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

import gate1_repo_check as check
from ilmar.domain.events import looks_like_a_credential

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="needs git")
TEMPLATE = Path(__file__).resolve().parents[2] / "deploy" / "gate1" / "disposable"


GIT = shutil.which("git") or "git"


def git(cwd: Path, *args: str) -> None:
    subprocess.run(  # noqa: S603 - fixed argv: git and test arguments
        [GIT, "-c", "user.name=t", "-c", "user.email=t@example.com",
         "-c", "init.defaultBranch=main", *args],
        cwd=cwd, check=True, capture_output=True,
    )  # fmt: skip


def mirror_of(tmp: Path, files: dict[str, str], *, later: dict[str, str] | None = None) -> Path:
    work = tmp / "work"
    work.mkdir()
    git(work, "init", "-q")
    for name, text in files.items():
        (work / name).parent.mkdir(parents=True, exist_ok=True)
        (work / name).write_text(text)
    git(work, "add", "-A")
    git(work, "commit", "-q", "-m", "seed")
    if later is not None:  # a second commit: what history holds, not the tip
        for name in later:
            (work / name).unlink()
        git(work, "add", "-A")
        git(work, "commit", "-q", "-m", "remove")
    mirror = tmp / "mirror.git"
    git(tmp, "clone", "-q", "--mirror", str(work), str(mirror))
    return mirror


def template_files() -> dict[str, str]:
    files = [p for p in TEMPLATE.rglob("*") if p.is_file() and "__pycache__" not in p.parts]
    return {str(p.relative_to(TEMPLATE)): p.read_text() for p in files}


def test_the_shipped_template_passes(tmp_path: Path) -> None:
    mirror = mirror_of(tmp_path, template_files())
    assert check.check_history(mirror, looks_like=looks_like_a_credential) == []


def test_a_credential_anywhere_in_history_is_found_and_never_echoed(tmp_path: Path) -> None:
    secret = "sk-ant-api03-" + "x" * 40
    mirror = mirror_of(
        tmp_path, {"calc.py": "x = 1\n", "old.env": f"KEY={secret}\n"}, later={"old.env": ""}
    )  # removed at the tip, still in history
    findings = check.check_history(mirror, looks_like=looks_like_a_credential)
    assert [(f["path"], f["kind"]) for f in findings] == [("old.env", "credential shape")]
    assert secret not in json.dumps(findings)


@pytest.mark.parametrize(
    "path", [".github/workflows/ci.yml", ".claude/settings.json", ".mcp.json", ".gitmodules",
             ".gitattributes", "CLAUDE.md", "AGENTS.md", "sub/.claude/x.json"],
)  # fmt: skip
def test_workflows_agent_settings_submodules_and_filters_are_refused(
    tmp_path: Path, path: str
) -> None:
    mirror = mirror_of(tmp_path, {"calc.py": "x = 1\n", path: "{}\n"})
    findings = check.check_history(mirror, looks_like=looks_like_a_credential)
    assert {"path": path, "kind": "forbidden path"} in [
        {k: f[k] for k in ("path", "kind")} for f in findings
    ]


def test_settings_must_be_private_topical_and_free_of_secrets() -> None:
    answers = {
        "repos/o/d": {"full_name": "o/d", "id": 42, "private": False, "topics": [],
                      "default_branch": "main"},
        "repos/o/d/actions/secrets": {"total_count": 1},
        "repos/o/d/actions/variables": {"total_count": 0},
        "repos/o/d/environments": {"total_count": 0},
    }  # fmt: skip

    def run(argv: list[str], data: bytes | None) -> subprocess.CompletedProcess[bytes]:
        path = argv[-1]
        return subprocess.CompletedProcess(argv, 0, json.dumps(answers[path]).encode(), b"")

    facts, failures = check.check_settings("o/d", run=run)
    assert facts["repository_id"] == 42
    assert failures == ["the repository is not private",
                        "the repository lacks the 'ilmar-disposable' topic",
                        "secrets: 1 defined (expected none)"]  # fmt: skip
