# SPDX-License-Identifier: Apache-2.0
"""Contract every ``SourceControlProvider`` adapter must satisfy (ADR-0010, ADR-0012).

Subclasses implement the hooks below. :meth:`make_repository` returns a
repository whose ``main`` branch contains :data:`BASE_FILES`. Real adapters
(#7) run the same suite against real git, where the hook and symlink cases
prove that nothing in an agent workspace is executed or followed.
"""

from __future__ import annotations

import os
import stat
from collections.abc import Mapping
from pathlib import Path

import pytest

from ilmar.domain.changes import (
    CandidateRevision,
    ChangeRequestSpec,
    ChecksSummary,
    RepositoryRef,
)
from ilmar.domain.errors import (
    CandidateRejectedError,
    LeaseConflictError,
    OwnershipConflictError,
    ProviderError,
)
from ilmar.ports.source_control import SourceControlProvider

from helpers import task

BASE_FILES = {
    "README.md": "hello\n",
    "src/app.py": "print('v1')\n",
    "tests/test_app.py": "def test_app():\n    assert True\n",
}
TASK_BRANCH = "ilmar/fake-42"
HOOK_NAMES = (
    "pre-commit",
    "prepare-commit-msg",
    "commit-msg",
    "post-commit",
    "pre-push",
    "post-checkout",
    "post-merge",
    "reference-transaction",
    "fsmonitor-watchman",
)


def read_tree(root: Path) -> dict[str, str]:
    """Map every path under ``root`` (excluding ``.git``) to its content or ``-> target``."""
    found: dict[str, str] = {}
    for directory, subdirs, files in os.walk(root, followlinks=False):
        subdirs[:] = [d for d in subdirs if d != ".git"]
        for name in files + [d for d in subdirs if (Path(directory) / d).is_symlink()]:
            path = Path(directory) / name
            relative = path.relative_to(root).as_posix()
            if path.is_symlink():
                found[relative] = f"-> {os.readlink(path)}"
            else:
                found[relative] = path.read_bytes().decode("latin-1")
    return found


class SourceControlContract:
    # -- hooks -----------------------------------------------------------

    def make_provider(self) -> SourceControlProvider:
        raise NotImplementedError

    def make_repository(self, provider: SourceControlProvider) -> RepositoryRef:
        raise NotImplementedError

    def push_external(
        self,
        provider: SourceControlProvider,
        repository: RepositoryRef,
        branch: str,
        files: Mapping[str, str],
    ) -> str:
        """Move ``branch`` as another actor would; return its new head."""
        raise NotImplementedError

    def open_foreign_change_request(
        self, provider: SourceControlProvider, repository: RepositoryRef, source_branch: str
    ) -> None:
        """Open a change request on ``source_branch`` the way a human would."""
        raise NotImplementedError

    # -- fixtures --------------------------------------------------------

    @pytest.fixture
    def provider(self) -> SourceControlProvider:
        return self.make_provider()

    @pytest.fixture
    def repo(self, provider: SourceControlProvider) -> RepositoryRef:
        return self.make_repository(provider)

    @pytest.fixture
    def workspace(
        self, provider: SourceControlProvider, repo: RepositoryRef, tmp_path: Path
    ) -> Path:
        path = tmp_path / "agent-workspace"
        provider.checkout(repo, base_branch="main", branch=TASK_BRANCH, destination=path)
        return path

    def _base(self, provider: SourceControlProvider, repo: RepositoryRef) -> str:
        head = provider.branch_head(repo, "main")
        assert head is not None
        return head

    def _freeze(
        self,
        provider: SourceControlProvider,
        repo: RepositoryRef,
        workspace: Path,
        message: str = "m",
    ) -> CandidateRevision:
        return provider.freeze_candidate(
            repo, base_revision=self._base(provider, repo), workspace=workspace, message=message
        )

    # -- branch_head / checkout ------------------------------------------

    def test_unknown_branch_has_no_head(
        self, provider: SourceControlProvider, repo: RepositoryRef
    ) -> None:
        assert provider.branch_head(repo, "no-such-branch") is None

    def test_checkout_starts_from_base_branch(
        self, provider: SourceControlProvider, repo: RepositoryRef, tmp_path: Path
    ) -> None:
        base = provider.checkout(
            repo, base_branch="main", branch=TASK_BRANCH, destination=tmp_path / "w"
        )
        assert base == provider.branch_head(repo, "main")
        assert read_tree(tmp_path / "w") == BASE_FILES

    def test_checkout_refuses_existing_destination(
        self, provider: SourceControlProvider, repo: RepositoryRef, tmp_path: Path
    ) -> None:
        (tmp_path / "w").mkdir()
        with pytest.raises(ProviderError):
            provider.checkout(
                repo, base_branch="main", branch=TASK_BRANCH, destination=tmp_path / "w"
            )

    def test_checkout_is_repeatable(
        self, provider: SourceControlProvider, repo: RepositoryRef, tmp_path: Path
    ) -> None:
        first = provider.checkout(
            repo, base_branch="main", branch=TASK_BRANCH, destination=tmp_path / "a"
        )
        second = provider.checkout(
            repo, base_branch="main", branch=TASK_BRANCH, destination=tmp_path / "b"
        )
        assert first == second
        assert read_tree(tmp_path / "a") == read_tree(tmp_path / "b")

    def test_checkout_resumes_an_existing_task_branch(
        self, provider: SourceControlProvider, repo: RepositoryRef, workspace: Path, tmp_path: Path
    ) -> None:
        (workspace / "progress.txt").write_text("half done\n")
        candidate = self._freeze(provider, repo, workspace)
        provider.publish_revision(
            repo, revision=candidate.revision, branch=TASK_BRANCH, expected_head=None
        )
        base = provider.checkout(
            repo, base_branch="main", branch=TASK_BRANCH, destination=tmp_path / "r"
        )
        assert base == candidate.revision
        assert read_tree(tmp_path / "r")["progress.txt"] == "half done\n"

    # -- freeze_candidate ------------------------------------------------

    def test_freeze_reports_changed_paths_relative_to_base(
        self, provider: SourceControlProvider, repo: RepositoryRef, workspace: Path
    ) -> None:
        (workspace / "src" / "app.py").write_text("print('v2')\n")
        (workspace / "docs").mkdir()
        (workspace / "docs" / "new.md").write_text("new\n")
        (workspace / "tests" / "test_app.py").unlink()
        candidate = self._freeze(provider, repo, workspace)
        assert candidate.base_revision == self._base(provider, repo)
        assert candidate.revision != candidate.base_revision
        assert candidate.changed_paths == ("docs/new.md", "src/app.py", "tests/test_app.py")

    def test_freeze_is_deterministic(
        self, provider: SourceControlProvider, repo: RepositoryRef, workspace: Path
    ) -> None:
        (workspace / "src" / "app.py").write_text("print('v2')\n")
        first = self._freeze(provider, repo, workspace, message="same")
        second = self._freeze(provider, repo, workspace, message="same")
        assert first == second

    def test_freeze_rejects_unknown_base(
        self, provider: SourceControlProvider, repo: RepositoryRef, workspace: Path
    ) -> None:
        with pytest.raises(ProviderError):
            provider.freeze_candidate(
                repo, base_revision="0" * 40, workspace=workspace, message="m"
            )

    def test_freeze_ignores_the_agent_git_directory(
        self, provider: SourceControlProvider, repo: RepositoryRef, workspace: Path, tmp_path: Path
    ) -> None:
        git = workspace / ".git"
        git.mkdir(exist_ok=True)
        (git / "planted-by-agent").write_text("not content\n")
        candidate = self._freeze(provider, repo, workspace)
        assert not any(p == ".git" or p.startswith(".git/") for p in candidate.changed_paths)
        provider.checkout_revision(repo, revision=candidate.revision, destination=tmp_path / "v")
        assert not (tmp_path / "v" / ".git" / "planted-by-agent").exists()

    @pytest.mark.parametrize("kind", ["directory", "gitlink-file"])
    def test_freeze_rejects_nested_repositories(
        self, provider: SourceControlProvider, repo: RepositoryRef, workspace: Path, kind: str
    ) -> None:
        nested = workspace / "vendor" / "lib"
        nested.mkdir(parents=True)
        if kind == "directory":
            (nested / ".git").mkdir()
        else:
            (nested / ".git").write_text("gitdir: /somewhere/else\n")
        with pytest.raises(CandidateRejectedError):
            self._freeze(provider, repo, workspace)

    @pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="needs FIFO support")
    def test_freeze_rejects_special_files(
        self, provider: SourceControlProvider, repo: RepositoryRef, workspace: Path
    ) -> None:
        os.mkfifo(workspace / "pipe")
        with pytest.raises(CandidateRejectedError):
            self._freeze(provider, repo, workspace)

    def test_symlinks_are_stored_as_links_and_never_followed(
        self, provider: SourceControlProvider, repo: RepositoryRef, workspace: Path, tmp_path: Path
    ) -> None:
        secret = tmp_path / "host-secret.txt"
        secret.write_text("TOP-SECRET-HOST-DATA\n")
        (workspace / "abs-link").symlink_to(secret)
        (workspace / "rel-link").symlink_to("../host-secret.txt")
        (workspace / "dir-link").symlink_to(tmp_path)
        candidate = self._freeze(provider, repo, workspace)
        provider.checkout_revision(repo, revision=candidate.revision, destination=tmp_path / "v")
        tree = read_tree(tmp_path / "v")
        assert tree["abs-link"] == f"-> {secret}"
        assert tree["rel-link"] == "-> ../host-secret.txt"
        assert tree["dir-link"] == f"-> {tmp_path}"
        assert not any("TOP-SECRET-HOST-DATA" in content for content in tree.values())

    def test_file_modes_and_bytes_are_preserved(
        self, provider: SourceControlProvider, repo: RepositoryRef, workspace: Path, tmp_path: Path
    ) -> None:
        script = workspace / "run.sh"
        script.write_text("#!/bin/sh\necho hi\n")
        script.chmod(0o755)
        (workspace / "blob.bin").write_bytes(bytes(range(256)))
        candidate = self._freeze(provider, repo, workspace)
        provider.checkout_revision(repo, revision=candidate.revision, destination=tmp_path / "v")
        assert os.stat(tmp_path / "v" / "run.sh").st_mode & stat.S_IXUSR
        assert not os.stat(tmp_path / "v" / "README.md").st_mode & stat.S_IXUSR
        assert (tmp_path / "v" / "blob.bin").read_bytes() == bytes(range(256))

    def test_nothing_in_the_workspace_is_ever_executed(
        self, provider: SourceControlProvider, repo: RepositoryRef, workspace: Path, tmp_path: Path
    ) -> None:
        """Agent-planted hooks and git configuration must never run on the host (ADR-0010)."""
        sentinel = tmp_path / "EXECUTED"
        script = f'#!/bin/sh\necho "$0" >> {sentinel}\nexit 0\n'
        git = workspace / ".git"
        hooks = git / "hooks"
        hooks.mkdir(parents=True, exist_ok=True)
        for name in HOOK_NAMES:
            (hooks / name).write_text(script)
            (hooks / name).chmod(0o755)
        tool = git / "tool.sh"
        tool.write_text(script)
        tool.chmod(0o755)
        with (git / "config").open("a") as config:
            config.write(
                "[core]\n"
                f"\thooksPath = {hooks}\n"
                f"\tfsmonitor = {tool}\n"
                f"\tpager = {tool}\n"
                f"\tsshCommand = {tool}\n"
                '[diff "evil"]\n'
                f"\ttextconv = {tool}\n"
                '[filter "evil"]\n'
                f"\tclean = {tool}\n"
                f"\tsmudge = {tool}\n"
                "[credential]\n"
                f"\thelper = {tool}\n"
            )
        (workspace / ".gitattributes").write_text("* diff=evil filter=evil\n")
        (workspace / "src" / "app.py").write_text("print('v2')\n")

        candidate = self._freeze(provider, repo, workspace)
        provider.checkout_revision(repo, revision=candidate.revision, destination=tmp_path / "v")
        provider.publish_revision(
            repo, revision=candidate.revision, branch=TASK_BRANCH, expected_head=None
        )
        provider.checkout(repo, base_branch="main", branch=TASK_BRANCH, destination=tmp_path / "r")

        assert not sentinel.exists(), sentinel.read_text()
        assert read_tree(tmp_path / "v")["src/app.py"] == "print('v2')\n"

    # -- checkout_revision -----------------------------------------------

    def test_checkout_revision_reproduces_exactly_the_frozen_tree(
        self, provider: SourceControlProvider, repo: RepositoryRef, workspace: Path, tmp_path: Path
    ) -> None:
        (workspace / "src" / "app.py").write_text("print('v2')\n")
        candidate = self._freeze(provider, repo, workspace)
        (workspace / "src" / "app.py").write_text("print('changed after freeze')\n")
        provider.checkout_revision(repo, revision=candidate.revision, destination=tmp_path / "v1")
        provider.checkout_revision(repo, revision=candidate.revision, destination=tmp_path / "v2")
        expected = {**BASE_FILES, "src/app.py": "print('v2')\n"}
        assert read_tree(tmp_path / "v1") == expected
        assert read_tree(tmp_path / "v2") == expected

    def test_checkout_revision_refuses_existing_destination_and_unknown_revision(
        self, provider: SourceControlProvider, repo: RepositoryRef, tmp_path: Path
    ) -> None:
        (tmp_path / "exists").mkdir()
        base = self._base(provider, repo)
        with pytest.raises(ProviderError):
            provider.checkout_revision(repo, revision=base, destination=tmp_path / "exists")
        with pytest.raises(ProviderError):
            provider.checkout_revision(repo, revision="0" * 40, destination=tmp_path / "new")

    # -- publish_revision ------------------------------------------------

    def test_publish_points_branch_exactly_at_the_candidate(
        self, provider: SourceControlProvider, repo: RepositoryRef, workspace: Path
    ) -> None:
        (workspace / "src" / "app.py").write_text("print('v2')\n")
        candidate = self._freeze(provider, repo, workspace)
        provider.publish_revision(
            repo, revision=candidate.revision, branch=TASK_BRANCH, expected_head=None
        )
        assert provider.branch_head(repo, TASK_BRANCH) == candidate.revision

    def test_publish_is_a_no_op_when_already_published(
        self, provider: SourceControlProvider, repo: RepositoryRef, workspace: Path
    ) -> None:
        candidate = self._freeze(provider, repo, workspace)
        provider.publish_revision(
            repo, revision=candidate.revision, branch=TASK_BRANCH, expected_head=None
        )
        # Re-execution after a crash: the lease is now stale, but the work is done.
        provider.publish_revision(
            repo, revision=candidate.revision, branch=TASK_BRANCH, expected_head=None
        )
        assert provider.branch_head(repo, TASK_BRANCH) == candidate.revision

    def test_publish_refuses_when_branch_exists_but_none_expected(
        self, provider: SourceControlProvider, repo: RepositoryRef, workspace: Path
    ) -> None:
        external = self.push_external(provider, repo, "main", {"other.txt": "x\n"})
        candidate = self._freeze(provider, repo, workspace)
        with pytest.raises(LeaseConflictError):
            provider.publish_revision(
                repo, revision=candidate.revision, branch="main", expected_head=None
            )
        assert provider.branch_head(repo, "main") == external

    def test_publish_refuses_when_the_branch_moved(
        self, provider: SourceControlProvider, repo: RepositoryRef, workspace: Path, tmp_path: Path
    ) -> None:
        first = self._freeze(provider, repo, workspace)
        provider.publish_revision(
            repo, revision=first.revision, branch=TASK_BRANCH, expected_head=None
        )
        human = self.push_external(provider, repo, TASK_BRANCH, {"human.txt": "edit\n"})
        (workspace / "src" / "app.py").write_text("print('v3')\n")
        second = provider.freeze_candidate(
            repo, base_revision=first.revision, workspace=workspace, message="m"
        )
        with pytest.raises(LeaseConflictError):
            provider.publish_revision(
                repo, revision=second.revision, branch=TASK_BRANCH, expected_head=first.revision
            )
        assert provider.branch_head(repo, TASK_BRANCH) == human

    def test_publish_with_correct_lease_updates_the_branch(
        self, provider: SourceControlProvider, repo: RepositoryRef, workspace: Path, tmp_path: Path
    ) -> None:
        first = self._freeze(provider, repo, workspace)
        provider.publish_revision(
            repo, revision=first.revision, branch=TASK_BRANCH, expected_head=None
        )
        resumed = tmp_path / "resumed"
        base = provider.checkout(repo, base_branch="main", branch=TASK_BRANCH, destination=resumed)
        (resumed / "src" / "app.py").write_text("print('v3')\n")
        second = provider.freeze_candidate(repo, base_revision=base, workspace=resumed, message="m")
        assert second.changed_paths == ("src/app.py",)
        provider.publish_revision(
            repo, revision=second.revision, branch=TASK_BRANCH, expected_head=base
        )
        assert provider.branch_head(repo, TASK_BRANCH) == second.revision

    def test_publish_rejects_unknown_revision(
        self, provider: SourceControlProvider, repo: RepositoryRef
    ) -> None:
        with pytest.raises(ProviderError):
            provider.publish_revision(
                repo, revision="0" * 40, branch=TASK_BRANCH, expected_head=None
            )
        assert provider.branch_head(repo, TASK_BRANCH) is None

    # -- change requests -------------------------------------------------

    def _spec(
        self, repo: RepositoryRef, title: str = "Fix #42", branch: str = TASK_BRANCH
    ) -> ChangeRequestSpec:
        return ChangeRequestSpec(
            repository=repo,
            source_branch=branch,
            target_branch="main",
            title=title,
            description="Verified by Ilmar.",
            task=task("42").ref,
        )

    def test_upsert_is_idempotent_per_source_branch(
        self, provider: SourceControlProvider, repo: RepositoryRef
    ) -> None:
        first = provider.upsert_change_request(self._spec(repo))
        second = provider.upsert_change_request(self._spec(repo, title="Fix #42 (updated)"))
        assert first == second
        assert (first.source_branch, first.target_branch) == (TASK_BRANCH, "main")
        other = provider.upsert_change_request(self._spec(repo, branch="ilmar/fake-43"))
        assert other.key != first.key

    def test_upsert_never_edits_a_change_request_it_did_not_open(
        self, provider: SourceControlProvider, repo: RepositoryRef
    ) -> None:
        self.open_foreign_change_request(provider, repo, TASK_BRANCH)
        with pytest.raises(OwnershipConflictError):
            provider.upsert_change_request(self._spec(repo))

    def test_checks_are_reported_for_a_change_request(
        self, provider: SourceControlProvider, repo: RepositoryRef
    ) -> None:
        change_request = provider.upsert_change_request(self._spec(repo))
        assert isinstance(provider.checks(change_request), ChecksSummary)
