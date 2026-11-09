# SPDX-License-Identifier: Apache-2.0
"""Opt-in test against real GitHub (``pytest -m integration``).

Needs a *disposable* repository and a token allowed to write its issues:

    ILMAR_TEST_GITHUB_REPO=owner/scratch-repo ILMAR_TEST_GITHUB_TOKEN=... pytest -m integration

It creates one issue, claims and releases it from two instances, then closes it.
The reconciliation test (ADR-0026) also reads standing and held claims; with
``ILMAR_TEST_GITHUB_SECOND_TOKEN`` (a second account) it checks that a forged
claim marker is another party's claim.
"""

from __future__ import annotations

import os
import uuid
from pathlib import Path

import pytest

from ilmar.adapters.github import GitHubClient, GitHubTaskProvider
from ilmar.adapters.github.source_control import GitHubSourceControl
from ilmar.domain.changes import ChangeRequestSpec, RepositoryRef
from ilmar.domain.tasks import ClaimOutcome, ReleaseResult, Standing, TaskRef

pytestmark = pytest.mark.integration

REPO = os.environ.get("ILMAR_TEST_GITHUB_REPO", "")
TOKEN = os.environ.get("ILMAR_TEST_GITHUB_TOKEN", "")
SECOND_TOKEN = os.environ.get("ILMAR_TEST_GITHUB_SECOND_TOKEN", "")


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
        assert a.claim(task.ref).outcome is ClaimOutcome.CLAIMED
        assert b.claim(task.ref).outcome is ClaimOutcome.CLAIMED_BY_OTHER
        a.release(task.ref)
        assert b.claim(task.ref).outcome is ClaimOutcome.CLAIMED
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


@pytest.mark.skipif(not (REPO and TOKEN), reason="set ILMAR_TEST_GITHUB_REPO and _TOKEN")
def test_standing_and_held_claims_on_real_github() -> None:
    """ADR-0026 §1, §5, §6 against a real repository: identity from ``GET /user``,
    fresh standing reads, held claims, a targeted release, and a forged marker
    from a second account (when one is configured)."""
    client = GitHubClient(TOKEN)
    label = f"ilmar-test-{uuid.uuid4().hex[:8]}"
    issue = client.request(
        "POST",
        f"/repos/{REPO}/issues",
        body={"title": "Ilmar reconciliation test (safe to close)", "labels": [label]},
    )
    number = int(issue["number"])
    provider = GitHubTaskProvider(
        client, repository=REPO, instance_id="it-a", eligible_label=label,
        claimed_label=f"{label}-claimed",
    )  # fmt: skip
    ref = TaskRef("github", f"{REPO}#{number}")
    try:
        assert provider.standing(ref).standing is Standing.CLAIM_LOST  # not claimed yet
        claim = provider.claim(ref)
        assert claim.outcome is ClaimOutcome.CLAIMED
        standing = provider.standing(ref)
        assert standing.standing is Standing.HELD
        assert standing.claim is not None
        assert standing.claim.claim_id == claim.claim_id
        assert [h.ref for h in provider.held_claims()] == [ref]
        if SECOND_TOKEN:
            other = GitHubClient(SECOND_TOKEN)
            other.request(
                "POST", f"/repos/{REPO}/issues/{number}/comments",
                body={"body": f"<!-- ilmar:claim instance=it-a token={'f' * 32} -->"},
            )  # fmt: skip
            assert provider.standing(ref).reason == "task-claimed-by-other-instance"
            assert [h.claim_id for h in provider.held_claims()] == [claim.claim_id]
        client.request("PATCH", f"/repos/{REPO}/issues/{number}", body={"state": "closed"})
        assert provider.standing(ref).reason == "task-closed"  # fresh: no cached answer
        assert provider.release_claim(standing.claim) is ReleaseResult.RELEASED
        assert provider.release_claim(standing.claim) is ReleaseResult.ABSENT
    finally:
        client.request("PATCH", f"/repos/{REPO}/issues/{number}", body={"state": "closed"})
