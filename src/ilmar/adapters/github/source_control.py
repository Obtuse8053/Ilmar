# SPDX-License-Identifier: Apache-2.0
"""GitHub as an Ilmar ``SourceControlProvider`` (ADR-0006, ADR-0010, ADR-0016).

Git operations run in the coordinator with :class:`HardenedGit` against one
coordinator-owned bare repository per GitHub repository. The agent's
workspace is only ever read as plain files (:mod:`ilmar.adapters.git.trees`).
Pull requests and checks use the REST API through :class:`GitHubClient`.
Check runs and commit statuses are read across all pages; a result set that
cannot be completed is reported as PENDING, never PASSED.

Credentials: the token reaches git only as an ``Authorization`` header scoped
to the GitHub URL, passed through ``GIT_CONFIG_*`` environment variables. It
never appears in a URL, on a command line, in a git config file or in a
workspace. Workspaces contain no ``.git`` at all.

Publishing (ADR-0010 §6): exactly the frozen revision, never a new commit;
an expected-head lease; fast-forward only, checked before an unforced push;
the remote head is confirmed afterwards. Revisions that change CI
configuration (``ci_config_paths``, default ``.github``) are refused as
defence in depth (ADR-0010 §5); the publishing token should also lack
permission to write workflow files.
"""

from __future__ import annotations

import base64
import re
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from ilmar.adapters.git.runner import HardenedGit
from ilmar.adapters.git.trees import (
    Identity,
    commit_tree,
    materialise,
    read_workspace,
    tree_entries,
)
from ilmar.adapters.github.client import GitHubClient
from ilmar.domain.changes import (
    CandidateRevision,
    ChangeRequest,
    ChangeRequestSpec,
    ChangeRequestState,
    CheckResult,
    ChecksSummary,
    CheckState,
    RepositoryRef,
)
from ilmar.domain.errors import (
    CandidateRejectedError,
    LeaseConflictError,
    NonFastForwardError,
    OwnershipConflictError,
    ProviderError,
)

MARKER = "<!-- ilmar:change-request -->"
INCOMPLETE_CHECKS = "ilmar: check results incomplete"
_CHECKS_PER_PAGE = 100
_MAX_CHECK_PAGES = 50
DEFAULT_IDENTITY = Identity("Ilmar", "ilmar@users.noreply.github.com")
_SLUG = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_BRANCH = re.compile(r"^(?!-)(?!.*\.\.)(?!.*//)(?!.*@\{)[A-Za-z0-9._/-]+(?<![./])(?<!\.lock)$")
_SHA = re.compile(r"^[0-9a-f]{40}$")
_FAILED = {"failure", "cancelled", "timed_out", "action_required", "startup_failure", "stale"}
_PASSED = {"success", "neutral", "skipped"}


class GitHubSourceControl:
    def __init__(
        self,
        client: GitHubClient,
        *,
        token: str,
        state_dir: Path,
        name: str = "github",
        git_base_url: str = "https://github.com",
        allowed_protocols: Sequence[str] = ("https",),
        git: str = "git",
        identity: Identity = DEFAULT_IDENTITY,
        ci_config_paths: Sequence[str] = (".github",),
    ) -> None:
        if not token:
            raise ValueError("a token is required")
        if not state_dir.is_absolute():
            raise ValueError("state_dir must be absolute")
        self._client = client
        self._token = token
        self._state = state_dir
        self._name = name
        self._base_url = git_base_url.rstrip("/")
        self._protocols = tuple(allowed_protocols)
        self._git = git
        self._identity = identity
        self._ci_paths = tuple(path.strip("/") for path in ci_config_paths if path.strip("/"))
        self._repos: dict[str, HardenedGit] = {}

    @property
    def name(self) -> str:
        return self._name

    # -- git side ------------------------------------------------------------

    def branch_head(self, repository: RepositoryRef, branch: str) -> str | None:
        git, url = self._repository(repository)
        ref = f"refs/heads/{_branch(branch)}"
        listing = git.run("ls-remote", url, ref, extra_config=self._auth()).text()
        for line in listing.splitlines():
            sha, _, name = line.partition("\t")
            if name == ref and _SHA.match(sha):
                return sha
        return None

    def checkout(
        self,
        repository: RepositoryRef,
        *,
        base_branch: str,
        branch: str,
        destination: Path,
    ) -> str:
        if destination.exists() or destination.is_symlink():
            raise ProviderError(f"destination already exists: {destination}")
        git, url = self._repository(repository)
        source = branch if self.branch_head(repository, branch) else base_branch
        revision = self._fetch_branch(git, url, source)
        materialise(tree_entries(git, revision), destination)
        return revision

    def freeze_candidate(
        self,
        repository: RepositoryRef,
        *,
        base_revision: str,
        workspace: Path,
        message: str,
    ) -> CandidateRevision:
        git, url = self._repository(repository)
        if not git.has_commit(base_revision):
            self._fetch_all(git, url)
            if not git.has_commit(base_revision):
                raise ProviderError(f"unknown base revision {base_revision[:12]}")
        entries = read_workspace(workspace)
        timestamp = int(git.run("show", "-s", "--format=%ct", base_revision).text().strip())
        revision = commit_tree(
            git,
            entries,
            parent=base_revision,
            message=message,
            identity=self._identity,
            timestamp=timestamp,
        )
        git.run("update-ref", f"refs/ilmar/candidates/{revision}", revision)
        changed = git.run(
            "diff-tree", "-r", "-z", "--no-renames", "--name-only", "--no-ext-diff",
            "--no-textconv", base_revision, revision,
        ).stdout  # fmt: skip
        paths = sorted({p.decode("utf-8", "replace") for p in changed.split(b"\0") if p})
        return CandidateRevision(revision, base_revision, tuple(paths))

    def checkout_revision(
        self,
        repository: RepositoryRef,
        *,
        revision: str,
        destination: Path,
    ) -> None:
        git, _ = self._repository(repository)
        if destination.exists() or destination.is_symlink():
            raise ProviderError(f"destination already exists: {destination}")
        if not git.has_commit(revision):
            raise ProviderError(f"unknown revision {revision[:12]}")
        materialise(tree_entries(git, revision), destination)

    def publish_revision(
        self,
        repository: RepositoryRef,
        *,
        revision: str,
        branch: str,
        expected_head: str | None,
    ) -> None:
        git, url = self._repository(repository)
        branch = _branch(branch)
        if not git.has_commit(revision):
            raise ProviderError(f"unknown revision {revision[:12]}")
        current = self.branch_head(repository, branch)
        if current == revision:
            return  # already published: re-execution after a crash
        if current != expected_head:
            raise LeaseConflictError(
                f"{branch} is at {_short(current)}, expected {_short(expected_head)}"
            )
        if current is not None:
            fetched = self._fetch_branch(git, url, branch)
            if fetched != current:
                raise LeaseConflictError(f"{branch} moved while publishing")
            ancestor = git.run("merge-base", "--is-ancestor", current, revision, check=False)
            if ancestor.returncode != 0:
                raise NonFastForwardError(f"{revision[:12]} does not descend from {branch}")
        self._refuse_ci_changes(git, url, revision, current)

        pushed = git.run(
            "push", "--porcelain", "--no-verify", "--no-recurse-submodules", url,
            f"{revision}:refs/heads/{branch}",
            extra_config=self._auth(), check=False,
        )  # fmt: skip
        head = self.branch_head(repository, branch)
        if head == revision:
            return
        if pushed.returncode != 0 and head != current:
            raise LeaseConflictError(f"{branch} moved while publishing")
        raise ProviderError(f"publishing to {branch} failed: {pushed.error()}")

    # -- pull requests -----------------------------------------------------------

    def upsert_change_request(self, spec: ChangeRequestSpec) -> ChangeRequest:
        slug = self._slug(spec.repository)
        owner = slug.split("/")[0]
        source, target = _branch(spec.source_branch), _branch(spec.target_branch)
        body = f"{spec.description}\n\n---\nTask: `{spec.task.key}`\n\n{MARKER}\n"
        open_pulls = list(
            self._client.paginate(
                f"/repos/{slug}/pulls",
                query={"state": "open", "head": f"{owner}:{source}", "per_page": "100"},
            )
        )
        mine = [p for p in open_pulls if MARKER in str(p.get("body") or "")]
        foreign = [p for p in open_pulls if MARKER not in str(p.get("body") or "")]
        if foreign:
            raise OwnershipConflictError(
                f"pull request #{foreign[0].get('number')} on {source} was not opened by Ilmar"
            )
        if mine:
            pull = self._client.request(
                "PATCH",
                f"/repos/{slug}/pulls/{int(mine[0]['number'])}",
                body={"title": spec.title, "body": body},
            )
        else:
            pull = self._client.request(
                "POST",
                f"/repos/{slug}/pulls",
                body={"title": spec.title, "head": source, "base": target, "body": body},
            )
        labels = [str(label.get("name")) for label in pull.get("labels") or []]
        if spec.labels:  # added, never removed: a human may have set others
            added = self._client.request(
                "POST",
                f"/repos/{slug}/issues/{int(pull['number'])}/labels",
                body={"labels": sorted(set(spec.labels))},
            )
            labels = [str(label.get("name")) for label in added or []]
        return self._change_request(spec.repository, pull, labels)

    def change_request_state(self, change_request: ChangeRequest) -> ChangeRequestState:
        slug = self._slug(change_request.repository)
        pull = self._client.request("GET", f"/repos/{slug}/pulls/{int(change_request.key)}")
        if pull.get("merged") is True or pull.get("merged_at"):
            return ChangeRequestState.MERGED
        if pull.get("state") == "closed":
            return ChangeRequestState.CLOSED
        if pull.get("state") == "open":
            return ChangeRequestState.OPEN
        raise ProviderError(f"pull request {change_request.key} has an unknown state")

    def checks(self, change_request: ChangeRequest) -> ChecksSummary:
        slug = self._slug(change_request.repository)
        pull = self._client.request("GET", f"/repos/{slug}/pulls/{int(change_request.key)}")
        sha = str((pull.get("head") or {}).get("sha") or "")
        if not _SHA.match(sha):
            raise ProviderError(f"pull request {change_request.key} has no head commit")
        runs, runs_complete = self._all_pages(
            f"/repos/{slug}/commits/{sha}/check-runs", "check_runs"
        )
        statuses, statuses_complete = self._all_pages(
            f"/repos/{slug}/commits/{sha}/status", "statuses"
        )
        results = [
            CheckResult(str(run.get("name")), _run_state(run), run.get("html_url")) for run in runs
        ]
        results += [
            CheckResult(
                str(status.get("context")),
                _status_state(str(status.get("state"))),
                status.get("target_url"),
            )
            for status in statuses
        ]
        if not (runs_complete and statuses_complete):
            # Fail closed: an unseen check may have failed, so never report PASSED.
            results.append(CheckResult(INCOMPLETE_CHECKS, CheckState.PENDING))
        return ChecksSummary(_overall(results), tuple(sorted(results, key=lambda r: r.name)))

    def _all_pages(self, path: str, key: str) -> tuple[list[Any], bool]:
        """Every item of a ``{total_count, <key>: [...]}`` endpoint, and whether all were seen."""
        items: list[Any] = []
        total: int | None = None
        for page in range(1, _MAX_CHECK_PAGES + 1):
            body = self._client.request(
                "GET", path, query={"per_page": str(_CHECKS_PER_PAGE), "page": str(page)}
            )
            batch = body.get(key) or []
            count = body.get("total_count")
            if isinstance(count, int):
                total = count if total is None else max(total, count)
            items.extend(batch)
            if len(batch) < _CHECKS_PER_PAGE or (total is not None and len(items) >= total):
                break
        complete = total is not None and len(items) >= total
        return items, complete

    # -- helpers -------------------------------------------------------------------

    def _slug(self, repository: RepositoryRef) -> str:
        if repository.provider != self._name or not _SLUG.match(repository.slug):
            raise ProviderError(f"not a {self._name} repository: {repository}")
        if ".." in repository.slug:
            raise ProviderError(f"invalid repository slug: {repository.slug}")
        return repository.slug

    def _repository(self, repository: RepositoryRef) -> tuple[HardenedGit, str]:
        slug = self._slug(repository)
        git = self._repos.get(slug)
        if git is None:
            git = HardenedGit(
                self._state / "repos" / (slug.replace("/", "__") + ".git"),
                home=self._state / "git-home",
                git=self._git,
                allowed_protocols=self._protocols,
            )
            git.init_bare()
            self._repos[slug] = git
        return git, f"{self._base_url}/{slug}.git"

    def _auth(self) -> dict[str, str]:
        if not self._base_url.startswith("https://"):
            return {}
        credential = base64.b64encode(f"x-access-token:{self._token}".encode()).decode()
        return {f"http.{self._base_url}/.extraHeader": f"Authorization: Basic {credential}"}

    def _fetch_branch(self, git: HardenedGit, url: str, branch: str) -> str:
        local = f"refs/ilmar/remote/{_branch(branch)}"
        git.run(
            "fetch", "--quiet", "--no-tags", "--no-write-fetch-head",
            "--no-recurse-submodules", url, f"+refs/heads/{branch}:{local}",
            extra_config=self._auth(),
        )  # fmt: skip
        revision = git.run("rev-parse", "--verify", f"{local}^{{commit}}").text().strip()
        if not _SHA.match(revision):
            raise ProviderError(f"cannot resolve {branch}")
        return revision

    def _fetch_all(self, git: HardenedGit, url: str) -> None:
        git.run(
            "fetch", "--quiet", "--no-tags", "--no-write-fetch-head",
            "--no-recurse-submodules", url, "+refs/heads/*:refs/ilmar/remote/*",
            extra_config=self._auth(),
        )  # fmt: skip

    def _default_head(self, git: HardenedGit, url: str) -> str | None:
        listing = git.run("ls-remote", "--symref", url, "HEAD", extra_config=self._auth())
        for line in listing.text().splitlines():
            if line.startswith("ref: ") and line.endswith("\tHEAD"):
                default = line[len("ref: refs/heads/") : -len("\tHEAD")]
                return self._fetch_branch(git, url, default)
        return None

    def _refuse_ci_changes(
        self, git: HardenedGit, url: str, revision: str, current: str | None
    ) -> None:
        """ADR-0010 §5: CI configuration changes are never published in M2."""
        if not self._ci_paths:
            return
        reference = current or self._default_head(git, url)
        if reference is None:
            return
        base = git.run("merge-base", reference, revision, check=False).text().strip()
        reference = base if _SHA.match(base) else reference
        for path in self._ci_paths:
            if _object_at(git, reference, path) != _object_at(git, revision, path):
                raise CandidateRejectedError(
                    f"ci-config-modified: {revision[:12]} changes {path}; a human must publish it"
                )

    def _change_request(
        self, repository: RepositoryRef, pull: Any, labels: list[str]
    ) -> ChangeRequest:
        return ChangeRequest(
            repository=repository,
            key=str(int(pull["number"])),
            url=str(pull.get("html_url") or ""),
            source_branch=str((pull.get("head") or {}).get("ref") or ""),
            target_branch=str((pull.get("base") or {}).get("ref") or ""),
            labels=tuple(sorted(set(labels))),
        )


def check_repository(slug: str) -> None:
    """Raise ``ValueError`` unless ``slug`` looks like ``owner/name`` (ADR-0019)."""
    if not _SLUG.match(slug) or ".." in slug:
        raise ValueError("repository must look like 'owner/name'")


def check_branch(name: str) -> None:
    """Raise ``ValueError`` unless ``name`` is a branch name Ilmar accepts (ADR-0019)."""
    if not _BRANCH.match(name):
        raise ValueError("not a valid branch name")


def _branch(name: str) -> str:
    if not _BRANCH.match(name):
        raise ProviderError(f"invalid branch name: {name!r}")
    return name


def _short(revision: str | None) -> str:
    return revision[:12] if revision else "(no branch)"


def _object_at(git: HardenedGit, revision: str, path: str) -> str | None:
    found = git.run("rev-parse", "--verify", "--quiet", f"{revision}:{path}", check=False)
    return found.text().strip() if found.returncode == 0 else None


def _run_state(run: Any) -> CheckState:
    if run.get("status") != "completed":
        return CheckState.PENDING
    conclusion = str(run.get("conclusion"))
    if conclusion in _PASSED:
        return CheckState.PASSED
    return CheckState.FAILED if conclusion in _FAILED else CheckState.PENDING


def _status_state(state: str) -> CheckState:
    if state == "success":
        return CheckState.PASSED
    if state in {"failure", "error"}:
        return CheckState.FAILED
    return CheckState.PENDING


def _overall(results: list[CheckResult]) -> CheckState:
    states = {result.state for result in results}
    for state in (CheckState.FAILED, CheckState.PENDING, CheckState.PASSED):
        if state in states:
            return state
    return CheckState.NONE
