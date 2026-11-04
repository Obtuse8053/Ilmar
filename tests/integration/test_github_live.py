# SPDX-License-Identifier: Apache-2.0
"""Opt-in test against real GitHub (``pytest -m integration``).

Needs a *disposable* repository and a token allowed to write its issues:

    ILMAR_TEST_GITHUB_REPO=owner/scratch-repo ILMAR_TEST_GITHUB_TOKEN=... pytest -m integration

It creates one issue, claims and releases it from two instances, then closes it.
"""

from __future__ import annotations

import os
import uuid
from pathlib import Path

import pytest

from ilmar.adapters.github import GitHubClient, GitHubTaskProvider
from ilmar.adapters.github.source_control import GitHubSourceControl
from ilmar.domain.changes import ChangeRequestSpec, RepositoryRef
from ilmar.domain.tasks import ClaimOutcome, TaskRef

pytestmark = pytest.mark.integration

REPO = os.environ.get("ILMAR_TEST_GITHUB_REPO", "")
TOKEN = os.environ.get("ILMAR_TEST_GITHUB_TOKEN", "")


@pytest.mark.skipif(not (REPO and TOKEN), reason="set ILMAR_TEST_GITHUB_REPO and _TOKEN")
def test_claim_lifecycle_on_real_github() -> None:
    client = GitHubClient(TOKEN)
    label = f"ilmar-test-{uuid.uuid4().hex[:8]}"
    issue = client.request(
        "POST",
        f"/repos/{REPO}/issues",
        body={"title": "Ilmar integration test (safe to close)", "labels": [label]},
    )
    a = GitHubTaskProvider(
        client,
        repository=REPO,
        instance_id="it-a",
        eligible_label=label,
        claimed_label=f"{label}-claimed",
    )
    b = GitHubTaskProvider(
        client,
        repository=REPO,
        instance_id="it-b",
        eligible_label=label,
        claimed_label=f"{label}-claimed",
    )
    try:
        (task,) = a.list_eligible()
        assert a.claim(task.ref) is ClaimOutcome.CLAIMED
        assert b.claim(task.ref) is ClaimOutcome.CLAIMED_BY_OTHER
        a.release(task.ref)
        assert b.claim(task.ref) is ClaimOutcome.CLAIMED
        b.release(task.ref)
        assert [t.ref for t in a.list_eligible()] == [task.ref]
    finally:
        client.request("PATCH", f"/repos/{REPO}/issues/{issue['number']}", body={"state": "closed"})


@pytest.mark.skipif(not (REPO and TOKEN), reason="set ILMAR_TEST_GITHUB_REPO and _TOKEN")
def test_source_control_round_trip_on_real_github(tmp_path: Path) -> None:
    """Checkout, freeze, publish, pull request and checks against a disposable repository.

    Needs a token with contents and pull-request write access to ``REPO`` and,
    deliberately, **no** workflow permission (ADR-0010 §5).
    """
    client = GitHubClient(TOKEN)
    provider = GitHubSourceControl(client, token=TOKEN, state_dir=tmp_path.resolve() / "state")
    repository = RepositoryRef("github", REPO)
    branch = f"ilmar-test/{uuid.uuid4().hex[:8]}"
    workspace = tmp_path / "ws"
    base = provider.checkout(repository, base_branch="main", branch=branch, destination=workspace)
    assert not (workspace / ".git").exists()
    (workspace / "ilmar-integration-test.txt").write_text(f"{branch}\n")
    candidate = provider.freeze_candidate(
        repository, base_revision=base, workspace=workspace, message="Ilmar integration test"
    )
    try:
        provider.publish_revision(
            repository, revision=candidate.revision, branch=branch, expected_head=None
        )
        assert provider.branch_head(repository, branch) == candidate.revision
        change_request = provider.upsert_change_request(
            ChangeRequestSpec(
                repository=repository,
                source_branch=branch,
                target_branch="main",
                title="Ilmar integration test (safe to close)",
                description="Created by tests/integration/test_github_live.py.",
                task=TaskRef("github", f"{REPO}#0"),
            )
        )
        assert (
            provider.upsert_change_request(
                ChangeRequestSpec(
                    repository=repository,
                    source_branch=branch,
                    target_branch="main",
                    title="Ilmar integration test (updated)",
                    description="Created by tests/integration/test_github_live.py.",
                    task=TaskRef("github", f"{REPO}#0"),
                )
            )
            == change_request
        )
        provider.checks(change_request)
        client.request(
            "PATCH", f"/repos/{REPO}/pulls/{change_request.key}", body={"state": "closed"}
        )
    finally:
        client.request("DELETE", f"/repos/{REPO}/git/refs/heads/{branch}")
