# SPDX-License-Identifier: Apache-2.0
"""GitHubSourceControl beyond the shared contract: hardening, credentials, CI guard (#7)."""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from ilmar.adapters.git.runner import HardenedGit
from ilmar.adapters.github import GitHubClient
from ilmar.adapters.github.source_control import MARKER, GitHubSourceControl
from ilmar.domain.changes import ChangeRequestSpec, CheckState, RepositoryRef
from ilmar.domain.errors import (
    CandidateRejectedError,
    LeaseConflictError,
    OwnershipConflictError,
    ProviderError,
)
from ilmar.domain.tasks import TaskRef

from fake_github import TOKEN, FakeGitHub
from git_support import PlainGit, write_files

REPO = RepositoryRef("github", "owner/repo")
BRANCH = "ilmar/task-7"


@dataclass
class World:
    tmp: Path
    api: FakeGitHub
    human: PlainGit
    bare: Path
    provider: GitHubSourceControl

    def push(self, branch: str, files: dict[str, str], *, start: str = "main") -> str:
        clone = self.tmp / f"human-{os.urandom(4).hex()}"
        self.human("clone", "--quiet", "--branch", start, str(self.bare), str(clone), cwd=self.tmp)
        write_files(clone, files)
        self.human("add", "-A", cwd=clone)
        self.human("commit", "--quiet", "-m", "human", cwd=clone)
        self.human("push", "--quiet", "origin", f"HEAD:refs/heads/{branch}", cwd=clone)
        return self.human("rev-parse", "HEAD", cwd=clone)

    def workspace(self, name: str = "ws") -> tuple[Path, str]:
        path = self.tmp / name
        base = self.provider.checkout(REPO, base_branch="main", branch=BRANCH, destination=path)
        return path, base


def make_provider(tmp: Path, api: FakeGitHub, **kwargs: Any) -> GitHubSourceControl:
    settings: dict[str, Any] = {
        "token": TOKEN,
        "state_dir": tmp / "coordinator",
        "git_base_url": f"file://{tmp / 'remotes'}",
        "allowed_protocols": ("file",),
    } | kwargs
    return GitHubSourceControl(GitHubClient(TOKEN, transport=api), **settings)


@pytest.fixture
def world(tmp_path: Path) -> World:
    tmp = tmp_path.resolve()
    human = PlainGit(tmp / "human-home")
    bare = tmp / "remotes" / "owner" / "repo.git"
    bare.parent.mkdir(parents=True)
    human("init", "--quiet", "--bare", str(bare), cwd=tmp)
    seed = tmp / "seed"
    seed.mkdir()
    human("init", "--quiet", cwd=seed)
    write_files(seed, {"README.md": "hi\n", ".github/workflows/ci.yml": "on: push\n"})
    human("add", "-A", cwd=seed)
    human("commit", "--quiet", "-m", "base", cwd=seed)
    human("push", "--quiet", str(bare), "HEAD:refs/heads/main", cwd=seed)
    api = FakeGitHub("owner/repo")
    return World(tmp, api, human, bare, make_provider(tmp, api))


# -- hardening -------------------------------------------------------------------------


def test_user_and_inherited_git_configuration_never_apply(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A hostile ~/.gitconfig, XDG config and GIT_* variables are all ignored."""
    sentinel = world.tmp / "EXECUTED"
    tool = world.tmp / "tool.sh"
    tool.write_text(f'#!/bin/sh\necho "$0 $*" >> {sentinel}\nexit 0\n')
    tool.chmod(0o755)
    hooks = world.tmp / "evil-hooks"
    hooks.mkdir()
    for name in ("pre-push", "reference-transaction", "post-checkout", "pre-commit"):
        (hooks / name).write_text(tool.read_text())
        (hooks / name).chmod(0o755)
    evil = (
        f"[core]\n\thooksPath = {hooks}\n\tfsmonitor = {tool}\n\tpager = {tool}\n"
        f"[credential]\n\thelper = {tool}\n[alias]\n\tfetch = !{tool}\n"
        f'[url "file://{world.tmp}/elsewhere/"]\n\tinsteadOf = file://{world.tmp}/remotes/\n'
    )
    home = world.tmp / "evil-home"
    (home / ".config" / "git").mkdir(parents=True)
    (home / ".gitconfig").write_text(evil)
    (home / ".config" / "git" / "config").write_text(evil)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / ".config"))
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(home / ".gitconfig"))
    monkeypatch.setenv("GIT_SSH_COMMAND", str(tool))
    monkeypatch.setenv("GIT_DIR", str(world.tmp / "nowhere"))
    monkeypatch.setenv("GIT_EXTERNAL_DIFF", str(tool))

    provider = make_provider(world.tmp, world.api)
    provider.checkout(REPO, base_branch="main", branch=BRANCH, destination=world.tmp / "w")
    (world.tmp / "w" / "README.md").write_text("changed\n")
    head = provider.branch_head(REPO, "main")
    assert head is not None
    candidate = provider.freeze_candidate(
        REPO, base_revision=head, workspace=world.tmp / "w", message="m"
    )
    provider.publish_revision(REPO, revision=candidate.revision, branch=BRANCH, expected_head=None)
    assert provider.branch_head(REPO, BRANCH) == candidate.revision
    assert not sentinel.exists(), sentinel.read_text()


def test_environment_is_built_from_scratch(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GIT_TRACE", "1")
    monkeypatch.setenv("GITHUB_TOKEN", "must-not-pass")
    git = HardenedGit(world.tmp / "x.git", home=world.tmp / "h")
    env = git.environment()
    assert "GIT_TRACE" not in env
    assert "GITHUB_TOKEN" not in env
    assert env["GIT_CONFIG_NOSYSTEM"] == "1"
    assert env["GIT_CONFIG_GLOBAL"] == "/dev/null"
    config = {
        env[f"GIT_CONFIG_KEY_{i}"]: env[f"GIT_CONFIG_VALUE_{i}"]
        for i in range(int(env["GIT_CONFIG_COUNT"]))
    }
    assert config["core.hooksPath"] == "/dev/null"
    assert config["protocol.allow"] == "never"
    assert config["protocol.https.allow"] == "always"
    assert "protocol.file.allow" not in config
    assert config["http.followRedirects"] == "false"
    assert config["transfer.fsckObjects"] == "true"
    assert config["credential.helper"] == ""


def test_coordinator_repository_has_no_hooks(world: World) -> None:
    world.provider.branch_head(REPO, "main")
    hooks = world.tmp / "coordinator" / "repos" / "owner__repo.git" / "hooks"
    assert not hooks.exists() or list(hooks.iterdir()) == []


@pytest.mark.parametrize("protocol", ["ext", "fd", "bad proto"])
def test_dangerous_protocols_cannot_be_enabled(world: World, protocol: str) -> None:
    with pytest.raises(ValueError, match="protocol"):
        HardenedGit(world.tmp / "x.git", home=world.tmp / "h", allowed_protocols=(protocol,))


def test_only_allowed_protocols_are_used(world: World) -> None:
    https_only = make_provider(world.tmp, world.api, allowed_protocols=("https",))
    with pytest.raises(ProviderError):
        https_only.branch_head(REPO, "main")  # the file:// remote is not allowed


def test_workspaces_contain_no_git_metadata(world: World) -> None:
    path, _ = world.workspace()
    assert not (path / ".git").exists()
    assert sorted(p.relative_to(path).as_posix() for p in path.rglob("*") if p.is_file()) == [
        ".github/workflows/ci.yml",
        "README.md",
    ]


# -- credentials ---------------------------------------------------------------------------


@dataclass
class Recorded:
    argv: list[str]
    env: dict[str, str]


@pytest.fixture
def recorded(monkeypatch: pytest.MonkeyPatch) -> list[Recorded]:
    calls: list[Recorded] = []
    real = subprocess.run

    def spy(argv: Any, *args: Any, **kwargs: Any) -> Any:
        calls.append(Recorded(list(argv), dict(kwargs.get("env") or {})))
        return real(argv, *args, **kwargs)

    monkeypatch.setattr(subprocess, "run", spy)
    return calls


def test_token_travels_only_as_a_scoped_header_in_the_environment(
    world: World, recorded: list[Recorded]
) -> None:
    provider = make_provider(
        world.tmp,
        world.api,
        git_base_url="https://127.0.0.1:9",  # nothing listens: the call fails fast
        allowed_protocols=("https",),
    )
    with pytest.raises(ProviderError) as caught:
        provider.branch_head(REPO, "main")
    assert TOKEN not in str(caught.value)
    remote_calls = [c for c in recorded if "ls-remote" in c.argv]
    assert remote_calls
    for call in recorded:
        assert not any(TOKEN in arg for arg in call.argv)
    header_keys = [k for k, v in remote_calls[0].env.items() if "Basic" in v]
    assert len(header_keys) == 1
    key_name = remote_calls[0].env[header_keys[0].replace("VALUE", "KEY")]
    assert key_name == "http.https://127.0.0.1:9/.extraHeader"
    # Nothing on disk carries the token.
    for path in (world.tmp / "coordinator").rglob("*"):
        if path.is_file():
            assert TOKEN.encode() not in path.read_bytes(), path


def test_no_header_is_configured_for_non_https_remotes(world: World) -> None:
    assert world.provider._auth() == {}


# -- CI configuration is never published (ADR-0010 §5) -----------------------------------------


@pytest.mark.parametrize(
    "change",
    [
        lambda ws: (ws / ".github" / "workflows" / "ci.yml").write_text(
            "on: pull_request_target\n"
        ),
        lambda ws: (ws / ".github" / "workflows" / "new.yml").write_text("on: push\n"),
        lambda ws: (ws / ".github" / "workflows" / "ci.yml").unlink(),
    ],
)
def test_candidates_that_change_ci_configuration_are_refused(world: World, change: Any) -> None:
    path, base = world.workspace()
    change(path)
    candidate = world.provider.freeze_candidate(
        REPO, base_revision=base, workspace=path, message="m"
    )
    with pytest.raises(CandidateRejectedError, match="ci-config-modified"):
        world.provider.publish_revision(
            REPO, revision=candidate.revision, branch=BRANCH, expected_head=None
        )
    assert world.provider.branch_head(REPO, BRANCH) is None


def test_ci_guard_also_applies_to_an_existing_branch(world: World) -> None:
    path, base = world.workspace()
    (path / "README.md").write_text("one\n")
    first = world.provider.freeze_candidate(REPO, base_revision=base, workspace=path, message="m")
    world.provider.publish_revision(
        REPO, revision=first.revision, branch=BRANCH, expected_head=None
    )
    (path / ".github" / "workflows" / "ci.yml").write_text("on: workflow_run\n")
    second = world.provider.freeze_candidate(
        REPO, base_revision=first.revision, workspace=path, message="m"
    )
    with pytest.raises(CandidateRejectedError, match="ci-config-modified"):
        world.provider.publish_revision(
            REPO, revision=second.revision, branch=BRANCH, expected_head=first.revision
        )
    assert world.provider.branch_head(REPO, BRANCH) == first.revision


def test_candidates_without_ci_changes_publish(world: World) -> None:
    path, base = world.workspace()
    (path / "README.md").write_text("fine\n")
    candidate = world.provider.freeze_candidate(
        REPO, base_revision=base, workspace=path, message="m"
    )
    world.provider.publish_revision(
        REPO, revision=candidate.revision, branch=BRANCH, expected_head=None
    )
    assert world.provider.branch_head(REPO, BRANCH) == candidate.revision


# -- races and freezing ---------------------------------------------------------------------


def test_branch_moving_during_publish_is_a_lease_conflict(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    path, base = world.workspace()
    (path / "README.md").write_text("ours\n")
    candidate = world.provider.freeze_candidate(
        REPO, base_revision=base, workspace=path, message="m"
    )
    human_head = ""
    original = world.provider._refuse_ci_changes

    def race(*args: Any) -> None:
        nonlocal human_head
        original(*args)
        human_head = world.push(BRANCH, {"human.txt": "first!\n"})  # lands before our push

    monkeypatch.setattr(world.provider, "_refuse_ci_changes", race)
    with pytest.raises(LeaseConflictError):
        world.provider.publish_revision(
            REPO, revision=candidate.revision, branch=BRANCH, expected_head=None
        )
    assert world.provider.branch_head(REPO, BRANCH) == human_head


@pytest.mark.parametrize("name", [".GIT", ".Git"])
def test_git_directory_variants_are_rejected(world: World, name: str) -> None:
    path, base = world.workspace()
    (path / "sub" / name).mkdir(parents=True)
    with pytest.raises(CandidateRejectedError, match="nested repository"):
        world.provider.freeze_candidate(REPO, base_revision=base, workspace=path, message="m")


def test_non_utf8_names_are_rejected(world: World) -> None:
    path, base = world.workspace()
    (path / os.fsdecode(b"bad-\xff-name")).write_text("x\n")
    with pytest.raises(CandidateRejectedError, match="UTF-8"):
        world.provider.freeze_candidate(REPO, base_revision=base, workspace=path, message="m")


def test_unusual_but_valid_names_round_trip(world: World) -> None:
    path, base = world.workspace()
    names = ['quote"name', "back\\slash", "spa ce", "tab\tname", "ünï.txt", "-dash"]
    for name in names:
        (path / name).write_text(name)
    candidate = world.provider.freeze_candidate(
        REPO, base_revision=base, workspace=path, message="m"
    )
    assert set(names) <= set(candidate.changed_paths)
    out = world.tmp / "out"
    world.provider.checkout_revision(REPO, revision=candidate.revision, destination=out)
    for name in names:
        assert (out / name).read_text() == name


def test_restrictive_umask_does_not_drop_the_executable_bit(world: World) -> None:
    path, base = world.workspace()
    (path / "run.sh").write_text("#!/bin/sh\n")
    (path / "run.sh").chmod(0o755)
    candidate = world.provider.freeze_candidate(
        REPO, base_revision=base, workspace=path, message="m"
    )
    old = os.umask(0o077)
    try:
        world.provider.checkout_revision(
            REPO, revision=candidate.revision, destination=world.tmp / "out"
        )
    finally:
        os.umask(old)
    assert (world.tmp / "out" / "run.sh").stat().st_mode & 0o777 == 0o755


@pytest.mark.parametrize("branch", ["-x", "a..b", "a//b", "x.lock", "x/", "x@{1}", "sp ace"])
def test_branch_names_are_validated(world: World, branch: str) -> None:
    with pytest.raises(ProviderError, match="branch"):
        world.provider.branch_head(REPO, branch)


@pytest.mark.parametrize(
    "repository",
    [
        RepositoryRef("gitlab", "owner/repo"),
        RepositoryRef("github", "../x"),
        RepositoryRef("github", "a/b/c"),
    ],
)
def test_repository_references_are_validated(world: World, repository: RepositoryRef) -> None:
    with pytest.raises(ProviderError):
        world.provider.branch_head(repository, "main")


# -- pull requests and checks ---------------------------------------------------------------


def spec(title: str = "Fix it") -> ChangeRequestSpec:
    return ChangeRequestSpec(
        repository=REPO,
        source_branch=BRANCH,
        target_branch="main",
        title=title,
        description="Verified by Ilmar.",
        task=TaskRef("github", "owner/repo#7"),
    )


def test_pull_request_carries_the_marker_and_task_and_is_updated_in_place(world: World) -> None:
    first = world.provider.upsert_change_request(spec())
    second = world.provider.upsert_change_request(spec("Fix it, v2"))
    assert first == second
    pull = world.api.pulls[int(first.key)]
    assert pull["title"] == "Fix it, v2"
    assert MARKER in pull["body"]
    assert "owner/repo#7" in pull["body"]
    assert len(world.api.pulls) == 1


def test_a_humans_pull_request_is_never_touched(world: World) -> None:
    number = world.api.add_pull(BRANCH, body="mine")
    with pytest.raises(OwnershipConflictError):
        world.provider.upsert_change_request(spec())
    assert world.api.pulls[number]["body"] == "mine"


def test_closed_pull_requests_do_not_count(world: World) -> None:
    world.api.add_pull(BRANCH, body="old", state="closed")
    created = world.provider.upsert_change_request(spec())
    assert world.api.pulls[int(created.key)]["state"] == "open"


@pytest.mark.parametrize(
    ("runs", "statuses", "expected"),
    [
        ([], [], CheckState.NONE),
        ([{"name": "a", "status": "completed", "conclusion": "success"}], [], CheckState.PASSED),
        (
            [{"name": "a", "status": "completed", "conclusion": "skipped"}],
            [{"context": "b", "state": "success"}],
            CheckState.PASSED,
        ),
        ([{"name": "a", "status": "in_progress", "conclusion": None}], [], CheckState.PENDING),
        ([], [{"context": "b", "state": "pending"}], CheckState.PENDING),
        (
            [{"name": "a", "status": "completed", "conclusion": "success"}],
            [{"context": "b", "state": "error"}],
            CheckState.FAILED,
        ),
        (
            [
                {"name": "a", "status": "queued", "conclusion": None},
                {"name": "c", "status": "completed", "conclusion": "timed_out"},
            ],
            [],
            CheckState.FAILED,
        ),
    ],
)
def test_checks_are_mapped(
    world: World, runs: list[dict[str, Any]], statuses: list[dict[str, Any]], expected: CheckState
) -> None:
    sha = "b" * 40
    world.api.head_sha = lambda branch: sha
    world.api.check_runs[sha] = runs
    world.api.statuses[sha] = statuses
    summary = world.provider.checks(world.provider.upsert_change_request(spec()))
    assert summary.state is expected
    assert len(summary.checks) == len(runs) + len(statuses)
