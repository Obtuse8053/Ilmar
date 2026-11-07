# SPDX-License-Identifier: Apache-2.0
"""The manual live workflow (.github/workflows/live.yml) and scripts/live_ci.py.

The workflow is the only place CI may use real credentials. These rules keep it
that way: a manual trigger only, a protected environment, no permissions by
default, the secrets in exactly one step, no untrusted input in a shell
script, and a post job that never sees the secrets.
"""

from __future__ import annotations

import hashlib
import importlib.metadata
import io
import json
import re
import subprocess
import zipfile
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest

import live_ci
import live_run

ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = (ROOT / ".github" / "workflows" / "live.yml").read_text()
CI = (ROOT / ".github" / "workflows" / "ci.yml").read_text()
CONTAINERFILE = (ROOT / "images" / "claude-code" / "Containerfile").read_text()
TOKEN = "github_pat_" + "A1b2C3d4" * 8
KEY = "sk-ant-api03-" + "Zy9Xw8Vu" * 8
SECRET = re.compile(r"\$\{\{\s*secrets\.")


def jobs(workflow: str = WORKFLOW) -> dict[str, str]:
    """Each job's block of the workflow, by name."""
    body = workflow.split("\njobs:\n", 1)[1]
    parts = re.split(r"^  ([a-z][a-z0-9_-]*):\n", body, flags=re.MULTILINE)
    return dict(zip(parts[1::2], parts[2::2], strict=True))


def steps(job: str, workflow: str = WORKFLOW) -> list[str]:
    return re.split(r"^      - ", jobs(workflow)[job], flags=re.MULTILINE)[1:]


def test_it_runs_only_when_started_by_hand() -> None:
    triggers = WORKFLOW.split("\non:\n", 1)[1].split("\n\n", 1)[0]
    assert re.findall(r"^  ([a-z_]+):", triggers, flags=re.MULTILINE) == ["workflow_dispatch"]


def test_nothing_is_permitted_unless_a_job_asks() -> None:
    assert re.search(r"^permissions: \{\}$", WORKFLOW, flags=re.MULTILINE)
    assert "concurrency:\n  group: live-tests\n  cancel-in-progress: false" in WORKFLOW


def test_the_secrets_are_behind_the_protected_environment_in_two_steps() -> None:
    """The live tests and their cleanup; both exec into Python, so no shell keeps them."""
    assert set(jobs()) == {"guard", "prepare", "live", "post"}
    live = jobs()["live"]
    assert "    environment: live\n" in live
    assert "    needs: [guard, prepare]\n" in live
    assert "    permissions:\n      contents: read\n    steps:" in live
    holders = [step for step in steps("live") if SECRET.search(step)]
    assert [h.splitlines()[0] for h in holders] == [
        "name: Live tests", "name: Clean up after a failure or cancellation",
    ]  # fmt: skip
    for step in holders:
        assert re.search(r"run: exec python3 scripts/live_ci\.py( --cleanup)?\n?$", step.rstrip())
    assert sorted(set(re.findall(r"secrets\.([A-Z_]+)", WORKFLOW))) == [
        "ANTHROPIC_API_KEY", "ILMAR_LIVE_GITHUB_TOKEN",
    ]  # fmt: skip


def first_secret_step() -> int:
    return next(i for i, s in enumerate(steps("live")) if SECRET.search(s))


def test_everything_is_checked_before_any_secret_in_this_order() -> None:
    """The tree is expected_sha; main still points at it (asked of the API, not the
    event); the wheels match their digests; the image matches the prepare job's."""
    before = steps("live")[: first_secret_step()]
    assert [s.splitlines()[0] for s in before] == [
        "uses: actions/checkout@11d5960a326750d5838078e36cf38b85af677262 # v4.4.0",
        "uses: actions/download-artifact@d3f86a106a0bac45b974a628896c90dbdf5c8093 # v4.3.0",
        "name: Check the context and the commit again (before any secret)",
        "name: Install the locked wheels (digests checked, nothing resolved)",
        "name: Podman environment",
        "name: Load the images (by digest; nothing is built here)",
    ]
    assert "run: python3 scripts/live_ci.py --guard-context" in before[2]
    assert "GH_TOKEN: ${{ github.token }}" in before[2]
    assert "run: python3 scripts/live_ci.py --install-wheels " in before[3]
    assert "run: python3 scripts/live_ci.py --load-images " in before[5]
    assert "ILMAR_IMAGE_ID: ${{ needs.prepare.outputs.image_id }}" in before[5]
    assert "ILMAR_IMAGE_SHA256: ${{ needs.prepare.outputs.archive_sha256 }}" in before[5]


# What may run once a step has held the secrets: each step's exact text, and why
# it is acceptable. A name kept with a changed body (another command, another
# env entry, another action input) fails, not only the known-bad patterns.
AFTER_THE_SECRETS = {
    # The tests themselves: our script, exec'd, from the checked-out expected_sha.
    "name: Live tests": """name: Live tests
        timeout-minutes: 65
        env:
          ILMAR_LIVE_GITHUB_TOKEN: ${{ secrets.ILMAR_LIVE_GITHUB_TOKEN }}
          ANTHROPIC_API_KEY: ${{ secrets.ANTHROPIC_API_KEY }}
        # exec: no shell keeps the secrets in its environment (the leak check
        # trusts only the coordinator and git).
        run: exec python3 scripts/live_ci.py
""",
    # Our script again; it only calls GitHub's API for the disposable repository.
    "name: Clean up after a failure or cancellation": """\
name: Clean up after a failure or cancellation
        if: failure() || cancelled()
        timeout-minutes: 5
        env:
          ILMAR_LIVE_GITHUB_TOKEN: ${{ secrets.ILMAR_LIVE_GITHUB_TOKEN }}
          ANTHROPIC_API_KEY: ${{ secrets.ANTHROPIC_API_KEY }}
        run: exec python3 scripts/live_ci.py --cleanup
""",
    # The runner's own podman; no secret in its environment.
    "name: Remove every container (no secrets)": """name: Remove every container (no secrets)
        if: always()
        run: |
          podman rm --all --force --time 0 || true
          podman network prune --force || true
""",
    # GitHub's upload-artifact, pinned to a full commit (immutable); no secret in
    # its step; every record was leak-checked in all encodings when written.
    "name: Keep the records (no credentials)": """name: Keep the records (no credentials)
        if: always()
        uses: actions/upload-artifact@ea165f8d65b6e75b540449e92b4886f43607fa02 # v4.6.2
        with:
          name: live-records
          path: ${{ runner.temp }}/live-reports/*.json
          if-no-files-found: ignore
          retention-days: 7
""",
}
FORBIDDEN_AFTER_SECRETS = re.compile(
    r"\bpip\b|\bnpm\b|\bnpx\b|\bcurl\b|\bwget\b|\bapt(-get)?\b|git (clone|fetch|pull)"
    r"|\bdocker\b|\bgh\b|python3? -c|podman (pull|build|load|import|run|create|exec)"
    r"|setup-python|download-artifact"
)


def after_secret_problems(workflow: str) -> list[str]:
    """Every way the steps after the first secret step differ from the reviewed list."""
    live = steps("live", workflow)
    first = next(i for i, step in enumerate(live) if SECRET.search(step))
    after = live[first:]
    names = [step.splitlines()[0] for step in after]
    if names != list(AFTER_THE_SECRETS):
        return [f"steps after the secrets: {names}"]
    problems = []
    for step, (name, expected) in zip(after, AFTER_THE_SECRETS.items(), strict=True):
        if step.rstrip("\n") + "\n" != expected:
            problems.append(f"{name}: changed")
        if FORBIDDEN_AFTER_SECRETS.search(step):
            problems.append(f"{name}: runs something forbidden")
    return problems


def test_nothing_is_installed_pulled_or_fetched_after_the_secrets() -> None:
    assert after_secret_problems(WORKFLOW) == []


@pytest.mark.parametrize(
    ("old", "new"),
    [
        # Same name, another command.
        ("podman network prune --force || true\n",
         "podman network prune --force || true\n          docker run attacker/image\n"),
        # Same command, one more env entry for the tests.
        ("        run: exec python3 scripts/live_ci.py\n",
         "          PYTHONPATH: ./elsewhere\n        run: exec python3 scripts/live_ci.py\n"),
        # Another commit of the upload action.
        ("upload-artifact@ea165f8d65b6e75b540449e92b4886f43607fa02 # v4.6.2\n        with:\n"
         "          name: live-records",
         "upload-artifact@" + "f" * 40 + " # v4.6.2\n        with:\n          name: live-records"),
        # A new step after the secrets.
        ("      - name: Keep the records (no credentials)\n",
         "      - name: Fetch a helper\n        run: gh release download x\n"
         "      - name: Keep the records (no credentials)\n"),
        # The cleanup taught to install something first.
        ("        run: exec python3 scripts/live_ci.py --cleanup\n",
         "        run: python3 -m pip install x && exec python3 scripts/live_ci.py --cleanup\n"),
    ],
)  # fmt: skip
def test_a_changed_or_added_step_after_the_secrets_is_caught(old: str, new: str) -> None:
    assert WORKFLOW.count(old) == 1, old
    assert after_secret_problems(WORKFLOW.replace(old, new)) != []


def test_the_live_job_never_installs_builds_or_resolves() -> None:
    live = jobs()["live"]
    assert not re.search(r"\bpip\b|\bnpm\b|podman (build|pull)|setup-python|\.\[dev\]", live)


def test_the_prepare_job_does_the_downloads_without_any_secret() -> None:
    prepare = jobs()["prepare"]
    assert "environment:" not in prepare
    assert not SECRET.search(prepare)
    assert "    needs: guard\n" in prepare
    assert "    permissions:\n      contents: read\n    outputs:" in prepare
    assert "python scripts/live_ci.py --guard-context\n" in prepare
    assert 'python scripts/live_ci.py --prepare "$RUNNER_TEMP/live-inputs"' in prepare


def test_the_guard_runs_mains_code_and_the_others_exactly_expected_sha() -> None:
    """The guard must not run code from an arbitrary commit (any commit in the
    repository's network can be checked out by SHA); once it has confirmed that
    expected_sha is main's head, the other jobs check out exactly that commit."""
    guard_checkout = steps("guard")[0]
    assert "ref:" not in guard_checkout
    for name in ("prepare", "live", "post"):
        (checkout,) = [s for s in steps(name) if "actions/checkout@" in s]
        assert "ref: ${{ env.ILMAR_EXPECTED_SHA }}\n" in checkout, name
        assert "persist-credentials: false" in checkout, name


def test_no_expression_is_expanded_inside_a_shell_script() -> None:
    for name in jobs():
        for step in steps(name):
            script = step.split("run: ", 1)[1] if "run: " in step else ""
            assert "${{" not in script, (name, step.splitlines()[0])


def test_the_guard_runs_first_and_never_sees_a_secret() -> None:
    guard = jobs()["guard"]
    assert "environment:" not in guard
    assert "needs:" not in guard
    assert not SECRET.search(guard)
    assert "if: github.ref == 'refs/heads/main'" in guard
    assert "      actions: read\n      contents: read\n" in guard
    assert "run: python3 scripts/live_ci.py --guard\n" in guard


def test_every_job_runs_only_on_main() -> None:
    for name, job in jobs().items():
        assert "github.ref == 'refs/heads/main'" in job, name


def test_the_post_job_never_sees_the_secrets_and_needs_a_passed_run() -> None:
    post = jobs()["post"]
    assert "environment:" not in post
    assert not SECRET.search(post)
    assert "      contents: read\n      issues: write\n" in post
    assert "needs: live" in post
    assert "if: success() && inputs.post_records" in post


def test_every_action_is_pinned_to_a_full_commit() -> None:
    uses = re.findall(r"uses: (\S+)", WORKFLOW)
    assert uses
    for use in uses:
        assert re.fullmatch(r"actions/[a-z-]+@[0-9a-f]{40}", use), use


DIGEST_PINNED = re.compile(r"[a-z0-9./:-]+@sha256:[0-9a-f]{64}")


def test_images_from_registries_are_pinned_by_digest() -> None:
    assert DIGEST_PINNED.fullmatch(live_ci.PROXY_IMAGE)
    (base,) = re.findall(r"^FROM (\S+)$", CONTAINERFILE, flags=re.MULTILINE)
    assert DIGEST_PINNED.fullmatch(base), base
    assert base.startswith("docker.io/library/node:22-bookworm-slim@sha256:")
    # CI pulls exactly that reference on every pull request.
    assert """podman pull "$(sed -n 's/^FROM //p' images/claude-code/Containerfile)\"""" in CI


def test_ci_downloads_the_lock_against_its_hashes_and_checks_it_is_complete() -> None:
    (step,) = [s for s in CI.split("\n      - ") if s.startswith("name: Live workflow inputs")]
    assert "pip download --require-hashes --no-deps --only-binary=:all:" in step
    assert "--requirement requirements/live.txt" in step
    assert "-m pip check" in step
    assert "scripts/live_ci.py --install-wheels" in step


def test_cleanup_runs_after_failures_and_cancellations() -> None:
    names = {s.splitlines()[0]: s for s in steps("live")}
    assert "if: failure() || cancelled()" in names["name: Clean up after a failure or cancellation"]
    assert "if: always()" in names["name: Remove every container (no secrets)"]
    assert "if: always()" in names["name: Keep the records (no credentials)"]


def test_inputs_are_only_expanded_in_the_environment() -> None:
    assert WORKFLOW.count("${{ inputs.") == 2  # the two env entries, nowhere else
    assert WORKFLOW.count("${{ needs.") == 2  # the load step's two env entries
    assert "ILMAR_EXPECTED_SHA: ${{ inputs.expected_sha }}" in WORKFLOW
    assert "ILMAR_TEST_CLAUDE_VERSION: ${{ inputs.claude_version }}" in WORKFLOW


# -- scripts/live_ci.py ------------------------------------------------------------------


SHA = "a" * 40


def actions(**extra: str) -> dict[str, str]:
    return {
        "GITHUB_ACTIONS": "true",
        "GITHUB_EVENT_NAME": "workflow_dispatch",
        "GITHUB_REF": "refs/heads/main",
        "GITHUB_SHA": SHA,
        "GITHUB_WORKFLOW_SHA": SHA,
        "GITHUB_WORKFLOW_REF": "Obtuse8053/Ilmar/.github/workflows/live.yml@refs/heads/main",
        "ILMAR_EXPECTED_SHA": SHA,
        "GITHUB_REPOSITORY": "Obtuse8053/Ilmar",
        "GITHUB_REPOSITORY_OWNER": "Obtuse8053",
        "GITHUB_RUN_ID": "77",
        "ILMAR_TEST_CLAUDE_VERSION": "2.1.0",
        "RUNNER_TEMP": "/tmp/runner",
        "PATH": "/usr/bin",
        **extra,
    }


class Recorder:
    def __init__(self, head: str = SHA) -> None:
        self.calls: list[list[str]] = []
        self.kwargs: list[dict[str, Any]] = []
        self.head = head

    def __call__(self, argv: Sequence[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        self.calls.append(list(argv))
        # A copy: the script clears its environment (the credentials) after the run.
        self.kwargs.append({**kwargs, "env": dict(kwargs["env"])} if "env" in kwargs else kwargs)
        out = ""
        if list(argv[1:3]) == ["info", "--format"]:
            out = "true\n"
        elif "rev-parse" in argv:
            out = self.head + "\n"
        return subprocess.CompletedProcess(argv, 0, out, "")


@pytest.fixture(autouse=True)
def _tools(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("shutil.which", lambda name: f"/usr/bin/{name}")


GOOD_ENVIRONMENT = {
    "name": "live",
    "can_admins_bypass": False,
    "protection_rules": [
        {"type": "required_reviewers", "prevent_self_review": True,
         "reviewers": [{"type": "User", "reviewer": {"login": "Obtuse8053"}}]},
        {"type": "branch_policy"},
    ],
    "deployment_branch_policy": {"protected_branches": False, "custom_branch_policies": True},
}  # fmt: skip
GOOD_POLICIES = {"total_count": 1, "branch_policies": [{"name": "main", "type": "branch"}]}


GOOD_RULES = [{"type": "pull_request"}, {"type": "non_fast_forward"}, {"type": "deletion"}]


def fake_api(
    environment: dict[str, Any] | None = None,
    policies: dict[str, Any] | None = None,
    rules: Any = None,
    main: str = SHA,
) -> Any:
    environment = GOOD_ENVIRONMENT if environment is None else environment
    policies = GOOD_POLICIES if policies is None else policies

    def get(path: str, token: str) -> Any:
        assert token == "actions-token"
        if path == "/repos/Obtuse8053/Ilmar/git/ref/heads/main":
            return {"ref": "refs/heads/main", "object": {"type": "commit", "sha": main}}
        if path.endswith("/rules/branches/main"):
            return GOOD_RULES if rules is None else rules
        return policies if path.endswith("/deployment-branch-policies") else environment

    return get


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"GITHUB_EVENT_NAME": "push"}, "workflow_dispatch"),
        ({"GITHUB_REF": "refs/heads/attacker"}, "refs/heads/main"),
        ({"GITHUB_SHA": "b" * 40}, "expected commit"),
        ({"ILMAR_EXPECTED_SHA": "main"}, "40-character"),
        ({"ILMAR_TEST_CLAUDE_VERSION": "latest"}, "x.y.z"),
        ({"GITHUB_ACTIONS": "false"}, "GitHub Actions"),
        ({"GITHUB_WORKFLOW_SHA": "b" * 40}, "workflow file"),
        (
            {"GITHUB_WORKFLOW_REF": "Obtuse8053/Ilmar/.github/workflows/live.yml@refs/heads/x"},
            "live.yml on main",
        ),
        (
            {"GITHUB_WORKFLOW_REF": "Obtuse8053/Ilmar/.github/workflows/ci.yml@refs/heads/main"},
            "live.yml on main",
        ),
    ],
)
def test_the_context_must_be_a_manual_run_of_the_expected_commit_on_main(
    change: dict[str, str], message: str
) -> None:
    problems = live_ci.check_context(actions(**change))
    assert any(message in p for p in problems), problems


def test_a_correctly_protected_environment_passes_the_guard(
    capsys: pytest.CaptureFixture[str],
) -> None:
    get = fake_api(GOOD_ENVIRONMENT, GOOD_POLICIES)
    assert live_ci.main(["--guard"], environ=actions(GH_TOKEN="actions-token"), get=get) == 0
    assert "as required" in capsys.readouterr().out


def _without_reviewers() -> dict[str, Any]:
    return {**GOOD_ENVIRONMENT, "protection_rules": [{"type": "branch_policy"}]}


@pytest.mark.parametrize(
    ("environment", "policies", "message"),
    [
        (_without_reviewers(), GOOD_POLICIES, "no required reviewers"),
        ({**GOOD_ENVIRONMENT, "protection_rules": [{"type": "required_reviewers",
                                                     "reviewers": []}]},
         GOOD_POLICIES, "no required reviewers"),
        ({**GOOD_ENVIRONMENT, "can_admins_bypass": True}, GOOD_POLICIES, "bypass"),
        ({k: v for k, v in GOOD_ENVIRONMENT.items() if k != "can_admins_bypass"},
         GOOD_POLICIES, "bypass"),
        ({**GOOD_ENVIRONMENT, "deployment_branch_policy": None}, GOOD_POLICIES,
         "selected branches"),
        ({**GOOD_ENVIRONMENT, "deployment_branch_policy": {"protected_branches": True,
                                                           "custom_branch_policies": False}},
         GOOD_POLICIES, "selected branches"),
        (GOOD_ENVIRONMENT, {"branch_policies": [{"name": "*", "type": "branch"}]}, "exactly main"),
        (GOOD_ENVIRONMENT, {"branch_policies": [{"name": "main", "type": "branch"},
                                                {"name": "dev", "type": "branch"}]},
         "exactly main"),
        (GOOD_ENVIRONMENT, {"branch_policies": [{"name": "main", "type": "tag"}]},
         "exactly main"),
    ],
)  # fmt: skip
def test_a_weaker_environment_is_refused(
    environment: dict[str, Any], policies: dict[str, Any], message: str,
    capsys: pytest.CaptureFixture[str],
) -> None:  # fmt: skip
    get = fake_api(environment, policies)
    assert live_ci.main(["--guard"], environ=actions(GH_TOKEN="actions-token"), get=get) == 1
    assert message in capsys.readouterr().err


def _reviewers(**rule: Any) -> dict[str, Any]:
    reviewers = [{"type": "User", "reviewer": {"login": "a-separate-reviewer"}}]
    return {**GOOD_ENVIRONMENT, "protection_rules": [
        {"type": "required_reviewers", "reviewers": reviewers, **rule}, {"type": "branch_policy"},
    ]}  # fmt: skip


@pytest.mark.parametrize(
    "environment",
    [
        _reviewers(prevent_self_review=False),  # the dispatcher could approve their own run
        _reviewers(),  # the field missing: never assumed on
        _reviewers(prevent_self_review="true"),  # only the boolean true counts
        _reviewers(prevent_self_review=None),
    ],
)
def test_reviewers_who_may_approve_their_own_run_are_refused(
    environment: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    get = fake_api(environment, GOOD_POLICIES)
    assert live_ci.main(["--guard"], environ=actions(GH_TOKEN="actions-token"), get=get) == 1
    assert "'Prevent self-review' is not enabled" in capsys.readouterr().err


def test_prevent_self_review_is_enough_to_pass_with_a_separate_reviewer() -> None:
    environment = _reviewers(prevent_self_review=True)
    assert live_ci.check_environment(environment, GOOD_POLICIES) == []


def test_every_reviewers_rule_must_prevent_self_review() -> None:
    reviewers = [{"type": "User", "reviewer": {"login": "a-separate-reviewer"}}]
    environment = {**GOOD_ENVIRONMENT, "protection_rules": [
        {"type": "required_reviewers", "reviewers": reviewers, "prevent_self_review": True},
        {"type": "required_reviewers", "reviewers": reviewers, "prevent_self_review": False},
    ]}  # fmt: skip
    problems = live_ci.check_environment(environment, GOOD_POLICIES)
    assert problems == ["'Prevent self-review' is not enabled for the required reviewers"]


@pytest.mark.parametrize(
    "rules",
    [[], [{"type": "pull_request"}], [{"type": "non_fast_forward"}, {"type": "deletion"}],
     {"message": "not a list"}],
)  # fmt: skip
def test_main_must_change_only_through_pull_requests(
    rules: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    get = fake_api(GOOD_ENVIRONMENT, GOOD_POLICIES, rules)
    assert live_ci.main(["--guard"], environ=actions(GH_TOKEN="actions-token"), get=get) == 1
    assert "main" in capsys.readouterr().err


@pytest.mark.parametrize("main", ["b" * 40, ""])
def test_the_guard_refuses_when_main_has_moved(
    main: str, capsys: pytest.CaptureFixture[str]
) -> None:
    get = fake_api(main=main)
    assert live_ci.main(["--guard"], environ=actions(GH_TOKEN="actions-token"), get=get) == 1
    assert "main no longer points at the expected commit" in capsys.readouterr().err


def test_the_live_job_asks_github_again_before_any_secret(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Approval can come hours after dispatch: main is read again in the live job."""
    env = actions(GH_TOKEN="actions-token")
    good = live_ci.main(["--guard-context"], environ=env, run=Recorder(), get=fake_api())
    assert good == 0
    moved = live_ci.main(["--guard-context"], environ=env, run=Recorder(),
                         get=fake_api(main="b" * 40))  # fmt: skip
    assert moved == 1
    assert "main no longer points" in capsys.readouterr().err
    no_token = live_ci.main(["--guard-context"], environ=actions(), run=Recorder(),
                            get=fake_api())  # fmt: skip
    assert no_token == 1


def test_an_unreadable_environment_is_refused(capsys: pytest.CaptureFixture[str]) -> None:
    def missing(path: str, token: str) -> Any:
        raise live_run.RunError(f"GET {path}: HTTP 404")

    assert live_ci.main(["--guard"], environ=actions(GH_TOKEN="actions-token"), get=missing) == 1
    assert "cannot read the 'live' environment" in capsys.readouterr().err


def test_a_redirect_is_never_followed_with_the_token() -> None:
    handler = live_ci._NoRedirects()
    assert handler.redirect_request(None, None, 302, "Found", {}, "https://evil.example/") is None


def test_it_refuses_to_run_outside_github_actions(capsys: pytest.CaptureFixture[str]) -> None:
    env = actions(ILMAR_LIVE_GITHUB_TOKEN=TOKEN, ANTHROPIC_API_KEY=KEY)
    del env["GITHUB_ACTIONS"]
    run = Recorder()
    assert live_ci.main([], environ=env, run=run) == 1
    assert run.calls == []
    assert "GitHub Actions" in capsys.readouterr().err


def test_a_different_checked_out_tree_is_refused_before_any_secret_is_read() -> None:
    run = Recorder(head="b" * 40)
    env = actions(ILMAR_LIVE_GITHUB_TOKEN=TOKEN, ANTHROPIC_API_KEY=KEY)
    assert live_ci.main([], environ=env, run=run) == 1
    assert not [c for c in run.calls if "pytest" in c]
    assert live_ci.main(["--guard-context"], environ=env, run=run) == 1


def installed_site(temp: Path) -> Path:
    """What --install-wheels leaves: the marker bound to this commit's lock."""
    site = temp / "live-site"
    site.mkdir(parents=True)
    lock = hashlib.sha256(live_ci.LOCK.read_text().encode()).hexdigest()
    (site / live_ci.SITE_MARKER).write_text(lock + "\n")
    return site


def test_the_tests_never_run_without_the_hash_checked_dependencies(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    run = Recorder()
    env = actions(ILMAR_LIVE_GITHUB_TOKEN=TOKEN, ANTHROPIC_API_KEY=KEY, RUNNER_TEMP=str(tmp_path))
    assert live_ci.main([], environ=env, run=run) == 1
    assert "--install-wheels" in capsys.readouterr().err
    stale = installed_site(tmp_path)
    (stale / live_ci.SITE_MARKER).write_text("0" * 64 + "\n")  # another lock's wheels
    assert live_ci.main([], environ=env, run=run) == 1
    assert not [c for c in run.calls if "pytest" in c]


def test_missing_secrets_are_named_never_shown(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    installed_site(tmp_path)
    env = actions(ILMAR_LIVE_GITHUB_TOKEN=TOKEN, RUNNER_TEMP=str(tmp_path))
    assert live_ci.main([], environ=env, run=Recorder()) == 1
    err = capsys.readouterr().err
    assert "ANTHROPIC_API_KEY" in err
    assert TOKEN not in err


def test_the_settings_follow_the_repository_owner() -> None:
    settings = live_ci.ci_settings(actions())
    assert settings["ILMAR_LIVE_REPO"] == "Obtuse8053/ilmar-disposable"
    assert settings["ILMAR_LIVE_OUTSIDE_REPO"] == "Obtuse8053/ilmar-outside-canary"
    assert settings["ILMAR_LIVE_REPORT_DIR"] == "/tmp/runner/live-reports"
    assert settings["ILMAR_TEST_PODMAN_PROXY_IMAGE"] == live_ci.PROXY_IMAGE
    assert settings["PYTHONPATH"] == "/tmp/runner/live-site"
    assert settings["PYTHONNOUSERSITE"] == "1"
    assert settings["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] == "1"
    with pytest.raises(live_run.RunError, match=r"x\.y\.z"):
        live_ci.ci_settings(actions(ILMAR_TEST_CLAUDE_VERSION="latest; rm -rf /"))


def test_a_failed_run_writes_a_failed_summary_bound_to_its_commit(
    tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:  # fmt: skip
    run = Recorder()
    installed_site(tmp_path)
    env = actions(ILMAR_LIVE_GITHUB_TOKEN=TOKEN, ANTHROPIC_API_KEY=KEY, PYTEST_ADDOPTS="-l",
                  RUNNER_TEMP=str(tmp_path))  # fmt: skip
    code = live_ci.main([], environ=env, run=run)
    assert code == 1  # the fake tests write no records: a skip is never a pass
    tests = [c for c in run.calls if "pytest" in c]
    assert [c[-1] for c in tests] == [live_run.SMOKE]
    for argv in run.calls:
        assert not [a for a in argv if TOKEN in a or KEY in a]
    test_env = run.kwargs[run.calls.index(tests[0])]["env"]
    assert "PYTEST_ADDOPTS" not in test_env
    assert test_env["ILMAR_LIVE_REQUIRED"] == "1"
    assert test_env["PYTHONPATH"] == str(tmp_path / "live-site")
    assert test_env["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] == "1"
    summary = json.loads((tmp_path / "live-reports" / "run-summary.json").read_text())
    assert summary["outcome"] == "failed"
    assert summary["commit"] == SHA
    output = capsys.readouterr()
    assert TOKEN not in output.out + output.err
    assert KEY not in output.out + output.err


def test_a_summary_holding_a_credential_is_never_written(tmp_path: Path) -> None:
    with pytest.raises(live_run.RunError):
        live_ci.write_summary(tmp_path, {"note": KEY[-30:] + "x"}, [KEY[-30:] + "x"])
    with pytest.raises(live_run.RunError):
        live_ci.write_summary(tmp_path, {"note": TOKEN}, [])  # by its shape alone
    assert list(tmp_path.iterdir()) == []


def records(directory: Path, *, outcome: str = "passed", commit: str = SHA) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "issue-44-smoke-test.json").write_text('{"hosts": ["api.anthropic.com"]}')
    (directory / "end-to-end-1107-ab12cd.json").write_text('{"pull_request": "u"}')
    (directory / "run-summary.json").write_text(json.dumps({"outcome": outcome, "commit": commit}))
    return directory


def test_only_checked_records_of_a_passed_run_of_this_commit_are_posted(tmp_path: Path) -> None:
    good = records(tmp_path / "good")
    assert [p.name for p in live_ci.checked_records(good, SHA)] == [
        "end-to-end-1107-ab12cd.json", "issue-44-smoke-test.json", "run-summary.json",
    ]  # fmt: skip


@pytest.mark.parametrize(
    ("damage", "message"),
    [
        (lambda d: (d / "run-summary.json").unlink(), "did not pass"),
        (lambda d: (d / "run-summary.json").write_text('{"outcome": "failed", "commit": "'
                                                       + SHA + '"}'), "did not pass"),
        (lambda d: (d / "run-summary.json").write_text('{"outcome": "passed", "commit": "'
                                                       + "b" * 40 + '"}'), "not from this commit"),
        (lambda d: (d / "notes.txt").write_text("x"), "unexpected file"),
        (lambda d: (d / "preflight.json").write_text(f'{{"t": "{TOKEN}"}}'), "credential"),
        (lambda d: (d / "preflight.json").write_text('{"x": "' + "y" * 70000 + '"}'),
         "too large"),
        (lambda d: (d / "preflight.json").write_text("not json"), "Expecting value"),
    ],
)  # fmt: skip
def test_anything_else_in_the_records_stops_the_post(
    tmp_path: Path, damage: Any, message: str
) -> None:
    directory = records(tmp_path / "r")
    damage(directory)
    with pytest.raises((live_run.RunError, ValueError), match=message):
        live_ci.checked_records(directory, SHA)


def test_post_mode_needs_the_context_and_records(tmp_path: Path) -> None:
    directory = records(tmp_path / "r")
    run = Recorder()
    outside = actions()
    del outside["GITHUB_ACTIONS"]
    assert live_ci.main(["--post", str(directory)], environ=outside, run=run) == 1
    assert live_ci.main(["--post", str(tmp_path / "empty")], environ=actions(), run=run) == 1
    assert [c for c in run.calls if c[0].endswith("gh")] == []


class FakeRepo:
    """Answers the cleanup's GitHub calls; records what it changed."""

    def __init__(self, repo: dict[str, Any]) -> None:
        self.repo = repo
        self.changed: list[str] = []

    def __call__(self, request: Any) -> Any:
        from ilmar.adapters.github.client import HttpResponse

        path = request.url.removeprefix("https://api.github.com")
        if request.method != "GET":
            self.changed.append(f"{request.method} {path}")
            return HttpResponse(204, {}, b"")
        body: Any = {}
        if path == "/repos/Obtuse8053/ilmar-disposable":
            body = self.repo
        elif "/git/matching-refs/heads/ilmar-live/" in path:
            body = [{"ref": "refs/heads/ilmar-live/1107-ab12cd"}]
        elif path.startswith("/repos/Obtuse8053/ilmar-disposable/labels"):
            body = [{"name": "ilmar-live:1107-ab12cd"}, {"name": "bug"}]
        elif "/issues?" in path:
            body = [{"number": 5}]
        elif "/pulls?" in path:
            body = [{"number": 6, "state": "open", "head": {"ref": "ilmar/task-5"}}]
        return HttpResponse(200, {}, json.dumps(body).encode())


DISPOSABLE = {"name": "ilmar-disposable", "private": True, "topics": ["ilmar-disposable"]}


def test_cleanup_removes_only_live_run_leftovers(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeRepo(DISPOSABLE)
    monkeypatch.setattr("ilmar.adapters.github.client.urllib_transport", fake)
    env = actions(ILMAR_LIVE_GITHUB_TOKEN=TOKEN, ANTHROPIC_API_KEY=KEY)
    assert live_ci.main(["--cleanup"], environ=env, run=Recorder()) == 0
    assert fake.changed == [
        "PATCH /repos/Obtuse8053/ilmar-disposable/issues/5",
        "DELETE /repos/Obtuse8053/ilmar-disposable/labels/ilmar-live%3A1107-ab12cd",
        "PATCH /repos/Obtuse8053/ilmar-disposable/pulls/6",
        "DELETE /repos/Obtuse8053/ilmar-disposable/git/refs/heads/ilmar/task-5",
        "DELETE /repos/Obtuse8053/ilmar-disposable/git/refs/heads/ilmar-live/1107-ab12cd",
    ]
    assert not [c for c in fake.changed if "/bug" in c]


@pytest.mark.parametrize("change", [{"private": False}, {"topics": []}, {"name": "Ilmar"}])
def test_cleanup_touches_nothing_outside_the_disposable_repository(
    monkeypatch: pytest.MonkeyPatch, change: dict[str, Any]
) -> None:
    fake = FakeRepo({**DISPOSABLE, **change})
    monkeypatch.setattr("ilmar.adapters.github.client.urllib_transport", fake)
    env = actions(ILMAR_LIVE_GITHUB_TOKEN=TOKEN, ANTHROPIC_API_KEY=KEY)
    assert live_ci.main(["--cleanup"], environ=env, run=Recorder()) == 1
    assert fake.changed == []


# -- the hash lock and the wheels ---------------------------------------------------------


LOCKED = {"iniconfig", "packaging", "pluggy", "pygments", "pytest"}


def test_the_lock_pins_every_dependency_to_one_digest() -> None:
    lock = live_ci.parse_lock(live_ci.LOCK.read_text())
    assert set(lock) == LOCKED
    for name, (version, hashes) in lock.items():
        assert re.fullmatch(r"\d+(\.\d+)+", version), name
        assert len(hashes) == 1, name
        assert hashes != {"0" * 64}, f"{name}: placeholder digest"


def _requirement_names(dist: str) -> set[str]:
    from packaging.requirements import Requirement

    marker_env = {"python_version": "3.12", "python_full_version": "3.12.3",
                  "sys_platform": "linux", "platform_system": "Linux", "os_name": "posix",
                  "implementation_name": "cpython", "platform_python_implementation": "CPython",
                  "platform_machine": "x86_64", "extra": ""}  # fmt: skip
    names = set()
    for text in importlib.metadata.requires(dist) or []:
        requirement = Requirement(text)
        if requirement.marker is None or requirement.marker.evaluate(marker_env):
            names.add(re.sub(r"[-_.]+", "-", requirement.name).lower())
    return names


def test_the_lock_is_closed_under_runtime_dependencies() -> None:
    """Every runtime dependency of a locked package on CPython 3.12 / Linux is
    itself locked. (Checked against the installed versions; CI also runs
    ``pip check`` on exactly the locked wheels.)"""
    for name in sorted(LOCKED):
        try:
            missing = _requirement_names(name) - LOCKED
        except importlib.metadata.PackageNotFoundError:
            continue
        assert not missing, f"{name} needs {missing}, which requirements/live.txt lacks"


@pytest.mark.parametrize(
    "line",
    ["pytest>=9 --hash=sha256:" + "a" * 64, "pytest==9.2.0", "pytest==9.2.0 --hash=md5:abc",
     "-i https://example.invalid/simple", "--extra-index-url https://example.invalid",
     "pytest @ https://example.invalid/pytest.whl", "-r other.txt", "-e .",
     "pytest==9.2.0 --hash=sha256:" + "a" * 64 + " --no-binary :all:"],
)  # fmt: skip
def test_anything_but_an_exact_hashed_requirement_is_refused(line: str) -> None:
    with pytest.raises(live_run.RunError, match="exact, hashed"):
        live_ci.parse_lock(line + "\n")


def test_a_package_listed_twice_or_an_empty_lock_is_refused() -> None:
    line = "pytest==9.2.0 --hash=sha256:" + "a" * 64 + "\n"
    with pytest.raises(live_run.RunError, match="twice"):
        live_ci.parse_lock(line + line)
    with pytest.raises(live_run.RunError, match="nothing"):
        live_ci.parse_lock("# only comments\n")


def wheel(directory: Path, name: str, version: str, files: dict[str, bytes] | None = None,
          *, symlink: str = "") -> tuple[Path, str]:  # fmt: skip
    directory.mkdir(parents=True, exist_ok=True)
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for member, data in (files or {f"{name}/__init__.py": b"VALUE = 1\n"}).items():
            archive.writestr(member, data)
        if symlink:
            info = zipfile.ZipInfo(symlink)
            info.external_attr = 0o120777 << 16
            archive.writestr(info, "/etc/passwd")
    path = directory / f"{name}-{version}-py3-none-any.whl"
    path.write_bytes(buffer.getvalue())
    return path, hashlib.sha256(buffer.getvalue()).hexdigest()


def lock_for(*entries: tuple[str, str, str]) -> str:
    return "".join(f"{n}=={v} --hash=sha256:{h}\n" for n, v, h in entries)


def test_the_locked_wheels_are_unpacked_after_their_digests_match(tmp_path: Path) -> None:
    _, one = wheel(tmp_path / "w", "alpha", "1.0")
    _, two = wheel(tmp_path / "w", "beta", "2.0")
    lock = lock_for(("alpha", "1.0", one), ("beta", "2.0", two))
    site = tmp_path / "site"
    names = live_ci.install_wheels(tmp_path / "w", site, lock)
    assert names == ["alpha-1.0-py3-none-any.whl", "beta-2.0-py3-none-any.whl"]
    assert (site / "alpha" / "__init__.py").read_text() == "VALUE = 1\n"
    live_ci.check_site(site, lock)  # the run accepts it
    with pytest.raises(live_run.RunError):
        live_ci.check_site(site, lock_for(("alpha", "1.0", one)))  # another lock


def _tampered(tmp_path: Path) -> tuple[str, str]:
    path, digest = wheel(tmp_path / "w", "alpha", "1.0")
    path.write_bytes(path.read_bytes() + b"x")
    return lock_for(("alpha", "1.0", digest)), "does not match its locked sha256"


def _extra(tmp_path: Path) -> tuple[str, str]:
    _, digest = wheel(tmp_path / "w", "alpha", "1.0")
    wheel(tmp_path / "w", "evil", "6.6")
    return lock_for(("alpha", "1.0", digest)), "not exactly the locked ones"


def _missing(tmp_path: Path) -> tuple[str, str]:
    _, digest = wheel(tmp_path / "w", "alpha", "1.0")
    return lock_for(("alpha", "1.0", digest), ("beta", "2.0", "b" * 64)), "not exactly"


def _other_version(tmp_path: Path) -> tuple[str, str]:
    _, digest = wheel(tmp_path / "w", "alpha", "1.1")
    return lock_for(("alpha", "1.0", digest)), "not exactly the locked ones"


def _platform_wheel(tmp_path: Path) -> tuple[str, str]:
    path, digest = wheel(tmp_path / "w", "alpha", "1.0")
    path.rename(tmp_path / "w" / "alpha-1.0-cp312-cp312-manylinux_2_17_x86_64.whl")
    return lock_for(("alpha", "1.0", digest)), "not exactly the locked ones"


def _traversal(tmp_path: Path) -> tuple[str, str]:
    _, digest = wheel(tmp_path / "w", "alpha", "1.0", {"../../escape.py": b"x"})
    return lock_for(("alpha", "1.0", digest)), "leaves the install directory"


def _absolute(tmp_path: Path) -> tuple[str, str]:
    _, digest = wheel(tmp_path / "w", "alpha", "1.0", {"/etc/escape.py": b"x"})
    return lock_for(("alpha", "1.0", digest)), "leaves the install directory"


def _link(tmp_path: Path) -> tuple[str, str]:
    _, digest = wheel(tmp_path / "w", "alpha", "1.0", symlink="alpha/link")
    return lock_for(("alpha", "1.0", digest)), "symbolic link"


def _data(tmp_path: Path) -> tuple[str, str]:
    files = {"alpha-1.0.data/scripts/run": b"#!/bin/sh\n"}
    _, digest = wheel(tmp_path / "w", "alpha", "1.0", files)
    return lock_for(("alpha", "1.0", digest)), "outside site-packages"


@pytest.mark.parametrize(
    "case",
    [_tampered, _extra, _missing, _other_version, _platform_wheel, _traversal, _absolute,
     _link, _data],
)  # fmt: skip
def test_a_wheel_that_differs_in_any_way_installs_nothing(tmp_path: Path, case: Any) -> None:
    lock, message = case(tmp_path)
    site = tmp_path / "site"
    with pytest.raises(live_run.RunError, match=message):
        live_ci.install_wheels(tmp_path / "w", site, lock)
    assert not site.exists()
    assert not (tmp_path.parent / "escape.py").exists()


def test_install_mode_uses_this_commits_lock(tmp_path: Path) -> None:
    assert live_ci.main(["--install-wheels", str(tmp_path / "none"), str(tmp_path / "s")]) == 1
    assert not (tmp_path / "s").exists()


# -- the agent image: prepared without secrets, loaded by digest -------------------------


IMAGE_ID = "c" * 64


class Podman(Recorder):
    """Answers the prepare and load calls; `save` writes the archive."""

    def __init__(self, loaded: str = IMAGE_ID, fail: str = "") -> None:
        super().__init__()
        self.loaded = loaded
        self.fail = fail

    def __call__(self, argv: Sequence[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        done = super().__call__(argv, **kwargs)
        argv = list(argv)
        if self.fail and self.fail in argv:
            return subprocess.CompletedProcess(argv, 1, "", "")
        if argv[:2] == ["podman", "save"]:
            Path(argv[argv.index("--output") + 1]).write_bytes(b"image bytes")
        if argv[:3] == ["podman", "image", "inspect"]:
            return subprocess.CompletedProcess(argv, 0, self.loaded + "\n", "")
        return done


def test_prepare_downloads_by_hash_and_builds_from_the_pinned_base(tmp_path: Path) -> None:
    run = Podman()
    outputs = live_ci.prepare(tmp_path / "out", actions(), run)
    pip, build, _inspect, save = run.calls
    assert pip[1:4] == ["-m", "pip", "download"]
    assert {"--require-hashes", "--no-deps", "--only-binary=:all:"} <= set(pip)
    assert pip[-1] == str(live_ci.LOCK)
    assert build[:2] == ["podman", "build"]
    assert "CLAUDE_CODE_VERSION=2.1.0" in build
    assert build[-1] == str(live_ci.ROOT / "images" / "claude-code")
    assert save[-1] == "localhost/ilmar-claude-code:2.1.0"
    assert outputs == {"image_id": IMAGE_ID,
                       "archive_sha256": hashlib.sha256(b"image bytes").hexdigest()}  # fmt: skip


@pytest.mark.parametrize("fail", ["download", "build", "save"])
def test_prepare_stops_at_the_first_failure(tmp_path: Path, fail: str) -> None:
    with pytest.raises(live_run.RunError):
        live_ci.prepare(tmp_path / "out", actions(), Podman(fail=fail))


def test_prepare_writes_its_outputs_for_the_live_job(tmp_path: Path) -> None:
    output = tmp_path / "github-output"
    env = actions(GITHUB_OUTPUT=str(output))
    assert live_ci.main(["--prepare", str(tmp_path / "out")], environ=env, run=Podman()) == 0
    assert output.read_text() == (
        f"image_id={IMAGE_ID}\narchive_sha256={hashlib.sha256(b'image bytes').hexdigest()}\n"
    )


def archive(tmp_path: Path) -> tuple[Path, dict[str, str]]:
    path = tmp_path / "claude-code.tar"
    path.write_bytes(b"image bytes")
    return path, {"ILMAR_IMAGE_ID": "sha256:" + IMAGE_ID,
                  "ILMAR_IMAGE_SHA256": hashlib.sha256(b"image bytes").hexdigest()}  # fmt: skip


def test_the_prepared_image_is_loaded_by_digest_and_the_proxy_pulled_by_digest(
    tmp_path: Path,
) -> None:
    path, ids = archive(tmp_path)
    run = Podman()
    assert live_ci.main(["--load-images", str(path)], environ=actions(**ids), run=run) == 0
    podman = [c for c in run.calls if c[0] == "podman"]
    assert podman == [
        ["podman", "load", "--quiet", "--input", str(path)],
        ["podman", "tag", IMAGE_ID, "localhost/ilmar-claude-code:2.1.0"],
        ["podman", "image", "inspect", "--format", "{{.Id}}", "localhost/ilmar-claude-code:2.1.0"],
        ["podman", "pull", "--quiet", live_ci.PROXY_IMAGE],
    ]


def test_an_archive_that_changed_is_never_loaded(tmp_path: Path) -> None:
    path, ids = archive(tmp_path)
    path.write_bytes(b"other bytes")
    run = Podman()
    assert live_ci.main(["--load-images", str(path)], environ=actions(**ids), run=run) == 1
    assert [c for c in run.calls if c[0] == "podman"] == []


def test_a_loaded_image_with_another_id_is_refused(tmp_path: Path) -> None:
    path, ids = archive(tmp_path)
    run = Podman(loaded="d" * 64)
    assert live_ci.main(["--load-images", str(path)], environ=actions(**ids), run=run) == 1
    assert ["podman", "pull", "--quiet", live_ci.PROXY_IMAGE] not in run.calls


@pytest.mark.parametrize("missing", ["ILMAR_IMAGE_ID", "ILMAR_IMAGE_SHA256"])
def test_loading_needs_the_prepare_jobs_digests(tmp_path: Path, missing: str) -> None:
    path, ids = archive(tmp_path)
    del ids[missing]
    run = Podman()
    assert live_ci.main(["--load-images", str(path)], environ=actions(**ids), run=run) == 1
    assert [c for c in run.calls if c[0] == "podman"] == []
