# SPDX-License-Identifier: Apache-2.0
"""Opt-in test against real GitHub (``pytest -m integration``).

Needs a *disposable* repository and a token allowed to write its issues:

    ILMAR_TEST_GITHUB_REPO=owner/scratch-repo ILMAR_TEST_GITHUB_TOKEN=... pytest -m integration

It creates one issue, claims and releases it from two instances, then closes it.
"""

from __future__ import annotations

import os
import uuid

import pytest

from ilmar.adapters.github import GitHubClient, GitHubTaskProvider
from ilmar.domain.tasks import ClaimOutcome

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
