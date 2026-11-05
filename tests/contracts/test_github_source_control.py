# SPDX-License-Identifier: Apache-2.0
"""The full SourceControl contract against GitHubSourceControl and **real git** (#7).

The remote is a local bare repository reached over ``file://``; pull requests
and checks go to the in-memory GitHub API. No contract case is skipped or
overridden: the planted-hook, ``.git/config``, symlink, nested-repository,
special-file and history-rewriting cases all run against real git here.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

import pytest

from ilmar.adapters.github import GitHubClient
from ilmar.adapters.github.source_control import GitHubSourceControl
from ilmar.domain.changes import ChangeRequest, RepositoryRef
from ilmar.domain.errors import CandidateRejectedError
from ilmar.ports.source_control import SourceControlProvider

from contracts.source_control import BASE_FILES, SourceControlContract
from fake_github import TOKEN, FakeGitHub
from git_support import PlainGit, write_files


class TestGitHubSourceControlOnRealGit(SourceControlContract):
    _tmp: Path
    _api: FakeGitHub
    _human: PlainGit
    _remotes: Path

    @pytest.fixture(autouse=True)
    def _setup(self, tmp_path: Path) -> None:
        self._tmp = tmp_path.resolve()
        self._remotes = self._tmp / "remotes"
        self._human = PlainGit(self._tmp / "human-home")
        self._api = FakeGitHub("owner/repo")

    def make_provider(self) -> SourceControlProvider:
        provider = GitHubSourceControl(
            GitHubClient(TOKEN, transport=self._api),
            token=TOKEN,
            state_dir=self._tmp / "coordinator",
            git_base_url=f"file://{self._remotes}",
            allowed_protocols=("file",),
        )
        self._api.head_sha = lambda branch: (
            provider.branch_head(RepositoryRef("github", "owner/repo"), branch) or "0" * 40
        )
        return provider

    def make_repository(self, provider: SourceControlProvider) -> RepositoryRef:
        bare = self._remotes / "owner" / "repo.git"
        bare.parent.mkdir(parents=True)
        self._human("init", "--quiet", "--bare", str(bare), cwd=self._tmp)
        seed = self._tmp / "seed"
        seed.mkdir()
        self._human("init", "--quiet", cwd=seed)
        write_files(seed, BASE_FILES)
        self._human("add", "-A", cwd=seed)
        self._human("commit", "--quiet", "-m", "base", cwd=seed)
        self._human("push", "--quiet", str(bare), "HEAD:refs/heads/main", cwd=seed)
        return RepositoryRef("github", "owner/repo")

    def test_load_candidate_reads_parents_from_the_commit_itself(
        self, provider: SourceControlProvider, repo: RepositoryRef, tmp_path: Path
    ) -> None:
        """A graft file cannot make a candidate look frozen on another base (review of 28a4a89)."""
        base, candidate = self._frozen(provider, repo, tmp_path)
        other = self.push_external(provider, repo, "main", {"x.txt": "x\n"})
        provider.checkout(repo, base_branch="main", branch="ilmar/g", destination=tmp_path / "g")
        (bare,) = (self._tmp / "coordinator").rglob("*.git")
        (bare / "info").mkdir(exist_ok=True)
        (bare / "info" / "grafts").write_text(f"{candidate.revision} {other}\n")
        with pytest.raises(CandidateRejectedError):
            provider.load_candidate(repo, base_revision=other, revision=candidate.revision)
        loaded = provider.load_candidate(repo, base_revision=base, revision=candidate.revision)
        assert loaded == candidate

    def push_external(
        self,
        provider: SourceControlProvider,
        repository: RepositoryRef,
        branch: str,
        files: Mapping[str, str],
    ) -> str:
        bare = self._remotes / "owner" / "repo.git"
        clone = self._tmp / f"human-{len(list(self._tmp.glob('human-*')))}"
        self._human("clone", "--quiet", "--branch", branch, str(bare), str(clone), cwd=self._tmp)
        write_files(clone, files)
        self._human("add", "-A", cwd=clone)
        self._human("commit", "--quiet", "-m", "human change", cwd=clone)
        self._human("push", "--quiet", "origin", f"HEAD:refs/heads/{branch}", cwd=clone)
        return self._human("rev-parse", "HEAD", cwd=clone)

    def open_foreign_change_request(
        self, provider: SourceControlProvider, repository: RepositoryRef, source_branch: str
    ) -> None:
        self._api.add_pull(source_branch, body="A human's pull request.")

    def finish_change_request(
        self, provider: SourceControlProvider, change_request: ChangeRequest, *, merged: bool
    ) -> None:
        pull = self._api.pulls[int(change_request.key)]
        pull["state"] = "closed"
        pull["merged"] = merged
        pull["merged_at"] = "2026-11-05T12:00:00Z" if merged else None
