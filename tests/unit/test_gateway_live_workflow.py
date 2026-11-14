# SPDX-License-Identifier: Apache-2.0
"""The guarded real-API gateway run: ``.github/workflows/gateway-live.yml`` and
``scripts/gateway_live_ci.py`` (#111, P5; maintainer decision 2026-11-14).

The same boundaries as the main live workflow (ADR-0023): started by hand on
main at a named commit, a protected environment with required reviewers,
pinned actions, hash-locked dependencies, one step with the secret that
execs into our script, and records checked for credentials before upload.
"""

from __future__ import annotations

import io
import json
import re
from contextlib import redirect_stderr
from pathlib import Path
from typing import Any

import pytest

import gateway_live_ci as gl
from live_run import RunError
from unit.test_live_workflow import SECRET, jobs, steps

ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = (ROOT / ".github" / "workflows" / "gateway-live.yml").read_text()
SHA = "a" * 40
KEY = "sk-ant-api03-" + "Zy9Xw8Vu" * 8
CONTEXT = {
    "GITHUB_ACTIONS": "true", "GITHUB_EVENT_NAME": "workflow_dispatch",
    "GITHUB_REF": "refs/heads/main", "ILMAR_EXPECTED_SHA": SHA, "GITHUB_SHA": SHA,
    "GITHUB_WORKFLOW_SHA": SHA, "GITHUB_REPOSITORY": "o/ilmar",
    "GITHUB_WORKFLOW_REF": "o/ilmar/.github/workflows/gateway-live.yml@refs/heads/main",
}  # fmt: skip


def test_it_runs_only_when_started_by_hand_with_nothing_permitted() -> None:
    triggers = WORKFLOW.split("\non:\n", 1)[1].split("\n\n", 1)[0]
    assert re.findall(r"^  ([a-z_]+):", triggers, flags=re.MULTILINE) == ["workflow_dispatch"]
    assert re.search(r"^permissions: \{\}$", WORKFLOW, flags=re.MULTILINE)
    assert "concurrency:\n  group: gateway-live\n  cancel-in-progress: false" in WORKFLOW
    assert "${{ inputs.expected_sha }}" not in WORKFLOW.split("\njobs:\n", 1)[1]


def test_every_action_is_pinned_by_commit() -> None:
    uses = re.findall(r"uses: (\S+)", WORKFLOW)
    assert uses
    assert all(re.fullmatch(r"[a-z-]+/[a-z-]+@[0-9a-f]{40}", u) for u in uses), uses


def test_the_one_secret_is_behind_the_protected_environment_in_one_step() -> None:
    assert set(jobs(WORKFLOW)) == {"guard", "prepare", "run"}
    for name in ("guard", "prepare"):
        assert "environment:" not in jobs(WORKFLOW)[name]
        assert not SECRET.search(jobs(WORKFLOW)[name])
    run = jobs(WORKFLOW)["run"]
    assert "    environment: gateway-live\n" in run
    assert "    needs: [guard, prepare]\n" in run
    assert "    permissions:\n      contents: read\n    steps:" in run
    holders = [s for s in steps("run", WORKFLOW) if SECRET.search(s)]
    assert len(holders) == 1
    assert holders[0].rstrip().endswith("run: exec python3 scripts/gateway_live_ci.py --run")
    assert re.findall(r"secrets\.([A-Z_]+)", WORKFLOW) == ["ILMAR_GATEWAY_LIVE_API_KEY"]


def test_everything_is_checked_before_the_secret_and_nothing_runs_after_but_the_upload() -> None:
    all_steps = steps("run", WORKFLOW)
    first = next(i for i, s in enumerate(all_steps) if SECRET.search(s))
    assert [s.splitlines()[0] for s in all_steps[:first]] == [
        "uses: actions/checkout@11d5960a326750d5838078e36cf38b85af677262 # v4.4.0",
        "uses: actions/download-artifact@d3f86a106a0bac45b974a628896c90dbdf5c8093 # v4.3.0",
        "name: Check the context and the commit again (before any secret)",
        "name: Install the locked wheels (digests checked, nothing resolved)",
    ]
    assert "run: python3 scripts/gateway_live_ci.py --guard-context" in all_steps[2]
    assert "run: python3 scripts/live_ci.py --install-wheels " in all_steps[3]
    after = all_steps[first + 1 :]
    assert [s.splitlines()[0] for s in after] == ["name: Keep the checked records (no credentials)"]
    assert "path: ${{ runner.temp }}/gateway-live-records/*.json" in after[0]


def test_the_checkouts_never_persist_credentials() -> None:
    assert WORKFLOW.count("persist-credentials: false") == WORKFLOW.count("actions/checkout@")


# -- the script -----------------------------------------------------------------------


def test_the_context_must_be_this_workflow_on_main_at_the_named_commit() -> None:
    assert gl.check_context(CONTEXT) == []
    other = "o/ilmar/.github/workflows/live.yml@refs/heads/main"
    changes: list[dict[str, str]] = [
        {"GITHUB_EVENT_NAME": "push"}, {"GITHUB_REF": "refs/heads/x"},
        {"GITHUB_SHA": "b" * 40}, {"ILMAR_EXPECTED_SHA": "abc"}, {"GITHUB_WORKFLOW_REF": other},
    ]  # fmt: skip
    for change in changes:
        assert gl.check_context({**CONTEXT, **change})


def api(environment: dict[str, Any]) -> Any:
    def get(path: str, token: str) -> Any:
        if path.endswith("/environments/gateway-live"):
            return environment
        if path.endswith("/deployment-branch-policies"):
            return {"branch_policies": [{"name": "main", "type": "branch"}]}
        if path.endswith("/rules/branches/main"):
            return [{"type": "pull_request"}, {"type": "non_fast_forward"}, {"type": "deletion"}]
        if path.endswith("/git/ref/heads/main"):
            return {"object": {"sha": SHA}}
        raise AssertionError(path)

    return get


PROTECTED = {
    "protection_rules": [{"type": "required_reviewers", "reviewers": [{"type": "User"}],
                          "prevent_self_review": True}],
    "can_admins_bypass": False,
    "deployment_branch_policy": {"protected_branches": False, "custom_branch_policies": True},
}  # fmt: skip


def test_the_guard_reads_the_gateway_live_environment() -> None:
    env = {**CONTEXT, "GH_TOKEN": "t"}
    assert gl.guard(env, api(PROTECTED)) == []
    unprotected = {**PROTECTED, "can_admins_bypass": True, "protection_rules": []}
    problems = gl.guard(env, api(unprotected))
    assert any("required reviewers" in p for p in problems)
    assert any("bypass" in p for p in problems)


def test_only_a_sound_credential_free_record_is_kept(tmp_path: Path) -> None:
    good = tmp_path / "issue-111-gateway-live-api.json"
    good.write_text(json.dumps({"log": ["CHARGE charged=1"]}))
    assert gl.checked_records(tmp_path, KEY) == [good]
    for name, text in [
        ("issue-111-gateway-live-api.json", json.dumps({"x": KEY})),
        ("issue-111-gateway-live-api.json", json.dumps({"x": "sk-ant-other-" + "q" * 30})),
        ("issue-111-gateway-live-api.json", "not json"),
        ("other.json", "{}"),
    ]:
        for leftover in tmp_path.iterdir():
            leftover.unlink()
        (tmp_path / name).write_text(text)
        with redirect_stderr(io.StringIO()):
            assert gl.checked_records(tmp_path, KEY) == []
        assert list(tmp_path.iterdir()) == []  # deleted, never uploaded


def test_the_run_needs_a_key_and_the_checked_dependencies(tmp_path: Path) -> None:
    def run(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("nothing may run")

    with pytest.raises(RunError, match="ANTHROPIC_API_KEY"):
        gl.run_tests({"ANTHROPIC_API_KEY": "x"}, run)
    with pytest.raises(RunError, match="hash-checked"):
        gl.run_tests({"ANTHROPIC_API_KEY": KEY, "ILMAR_LIVE_SITE": str(tmp_path)}, run)
