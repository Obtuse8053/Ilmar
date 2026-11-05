# SPDX-License-Identifier: Apache-2.0
"""End to end: a real git candidate verified in a real Podman sandbox (#9).

Combines the hardened source-control adapter (#7, a bare repository over
``file://``), the Podman runtime (#5) and the Verifier. The policy's checks
probe the verification sandbox itself, so this also shows it has no network
and no credential (ADR-0011 §7). Same requirements as ``test_podman_runtime.py``.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from ilmar.adapters.github import GitHubClient
from ilmar.adapters.github.source_control import GitHubSourceControl
from ilmar.adapters.memory import InMemoryExecutionStore
from ilmar.adapters.podman import PodmanRuntime
from ilmar.core.layout import WorkspaceLayout
from ilmar.core.state_machine import ExecutionStateMachine
from ilmar.core.verification import Verifier
from ilmar.domain.agent import AgentOutcome, AgentResult
from ilmar.domain.changes import CandidateRevision, RepositoryRef
from ilmar.domain.execution import Execution, ExecutionState
from ilmar.domain.verification import (
    EvidenceRecord,
    Verdict,
    VerificationCommand,
    VerificationPolicy,
)
from ilmar.domain.workspace import ResourceOwner

from fake_github import TOKEN, FakeGitHub
from git_support import PlainGit, write_files
from helpers import at, task
from integration.podman_support import IMAGE, PODMAN, remove_everything, require

pytestmark = pytest.mark.integration

S = ExecutionState
REPO = RepositoryRef("github", "owner/repo")
OWNER = ResourceOwner(instance_id="it", execution_id="verify", attempt=1, token="tok-v")
NAMES = tuple(
    f"ilmar-it-verify-1-verify-{run}-{job}"
    for run in (1, 2, 3)
    for job in ("default", "first", "second")
)
SECRET = "sk-ant-must-not-reach-verification"

POLICY_COMMANDS = (
    VerificationCommand(
        "code-is-fixed", ("sh", "-c", 'grep -q "fixed" src/app.py'), timeout_seconds=60
    ),
    VerificationCommand(
        "no-network", ("sh", "-c", '[ "$(ls /sys/class/net)" = "lo" ]'), timeout_seconds=60
    ),
    VerificationCommand(
        "no-credential",
        ("sh", "-c", "! env | grep -q -e ANTHROPIC -e GITHUB_TOKEN"),
        timeout_seconds=60,
    ),
    VerificationCommand("read-only-root", ("sh", "-c", "! touch /probe"), timeout_seconds=60),
)


@pytest.fixture(autouse=True)
def _require(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    require(("podman", PODMAN), ("ILMAR_TEST_PODMAN_IMAGE", IMAGE))
    monkeypatch.setenv("ANTHROPIC_API_KEY", SECRET)  # in the coordinator; must not leak
    remove_everything(*NAMES)
    yield
    remove_everything(*NAMES)


def scenario(
    tmp_path: Path,
    files: dict[str, str],
    commands: tuple[VerificationCommand, ...] = POLICY_COMMANDS,
) -> tuple[Verifier, Execution, CandidateRevision]:
    tmp = tmp_path.resolve()
    human = PlainGit(tmp / "human-home")
    bare = tmp / "remotes" / "owner" / "repo.git"
    bare.parent.mkdir(parents=True)
    human("init", "--quiet", "--bare", str(bare), cwd=tmp)
    seed = tmp / "seed"
    seed.mkdir()
    human("init", "--quiet", cwd=seed)
    write_files(seed, {"src/app.py": "print('bug')\n", "README.md": "x\n"})
    human("add", "-A", cwd=seed)
    human("commit", "--quiet", "-m", "base", cwd=seed)
    human("push", "--quiet", str(bare), "HEAD:refs/heads/main", cwd=seed)

    scm = GitHubSourceControl(
        GitHubClient(TOKEN, transport=FakeGitHub()),
        token=TOKEN,
        state_dir=tmp / "coordinator",
        git_base_url=f"file://{tmp / 'remotes'}",
        allowed_protocols=("file",),
    )
    agent_ws = tmp / "agent"
    base = scm.checkout(REPO, base_branch="main", branch="ilmar/v", destination=agent_ws)
    write_files(agent_ws, files)
    candidate = scm.freeze_candidate(REPO, base_revision=base, workspace=agent_ws, message="m")

    machine = ExecutionStateMachine(max_attempts=3)
    store = InMemoryExecutionStore()
    execution, transition = machine.start(task("v").ref, execution_id="verify", now=at(0))
    store.create(execution, transition)
    for step, target in enumerate((S.CLAIMED, S.PROVISIONING, S.RUNNING_AGENT, S.VERIFYING)):
        execution, _ = machine.advance(
            execution,
            target,
            now=at(step + 1),
            reason="t",
            base_revision=base if target is S.RUNNING_AGENT else None,
            candidate_revision=candidate.revision if target is S.VERIFYING else None,
        )
    layout = WorkspaceLayout.under(tmp / "state")
    layout.workspaces_root.mkdir(parents=True)
    verifier = Verifier(
        source_control=scm,
        runtime=PodmanRuntime(layout=layout),
        store=store,
        layout=layout,
        policy=VerificationPolicy(image=IMAGE, commands=commands),
        instance_id="it",
        clock=lambda: at(100),
    )
    return verifier, execution, candidate


def run(verifier: Verifier, execution: Execution, candidate: CandidateRevision) -> EvidenceRecord:
    return verifier.verify(
        execution=execution,
        repository=REPO,
        candidate=candidate,
        agent_result=AgentResult(AgentOutcome.COMPLETED, "All tests pass, trust me.", 0),
        owner=OWNER,
    )


def test_a_fixed_candidate_passes_in_an_isolated_sandbox(tmp_path: Path) -> None:
    verifier, execution, candidate = scenario(tmp_path, {"src/app.py": "print('fixed')\n"})
    record = run(verifier, execution, candidate)
    failed = [(c.name, c.detail) for c in record.checks if not c.passed]
    assert failed == []
    assert record.verdict is Verdict.PUBLISH


def test_an_unfixed_candidate_fails_whatever_the_agent_claims(tmp_path: Path) -> None:
    verifier, execution, candidate = scenario(
        tmp_path, {"src/app.py": "print('still a bug')\n", "PASSED": "all good\n"}
    )
    record = run(verifier, execution, candidate)
    assert record.verdict is Verdict.REJECTED_CHECKS_FAILED
    by_name = {c.name: c for c in record.checks}
    assert not by_name["code-is-fixed"].passed
    assert by_name["no-network"].passed
    assert by_name["no-credential"].passed
    assert by_name["read-only-root"].passed


# The marker is assembled at run time so the checking shell's own command line
# never contains it.
_SEES_STRAY = 'm=stray; m="$m-marker"; grep -qs -- "$m" /proc/[0-9]*/cmdline'


def job_commands(job: str, *, expect_tampered: bool) -> tuple[VerificationCommand, ...]:
    if expect_tampered:
        checks = ("grep -q weakened src/app.py", "[ -e /tmp/left-behind ]", _SEES_STRAY)
    else:
        checks = (
            "! grep -q weakened src/app.py",
            "[ ! -e /tmp/left-behind ]",
            f"! {{ {_SEES_STRAY}; }}",
        )
    return tuple(
        VerificationCommand(f"{job}-{index}", ("sh", "-c", check), timeout_seconds=60, job=job)
        for index, check in enumerate(checks)
    )


TAMPER = (
    "echo weakened >> src/app.py; touch /tmp/left-behind; "
    "sh -c 'while :; do sleep 1; done' stray-marker >/dev/null 2>&1 &"
)


def test_jobs_are_isolated_from_each_other_but_not_internally(tmp_path: Path) -> None:
    """Regression for the PR #45 review: files, /tmp and processes stop at the job."""
    commands = (
        VerificationCommand("tamper", ("sh", "-c", TAMPER), timeout_seconds=60, job="first"),
        *job_commands("first", expect_tampered=True),
        *job_commands("second", expect_tampered=False),
    )
    verifier, execution, candidate = scenario(
        tmp_path, {"src/app.py": "print('fixed')\n"}, commands
    )
    record = run(verifier, execution, candidate)
    failed = [(c.name, c.detail) for c in record.checks if not c.passed]
    assert failed == []
    assert {c.job for c in record.checks if c.job} == {"first", "second"}


def test_a_candidate_that_kills_its_sandbox_is_rejected_with_evidence(tmp_path: Path) -> None:
    """Adversarial review of cf68225: sabotage is a failed check, not missing evidence."""
    commands = (
        VerificationCommand(
            "kill-everything", ("sh", "-c", "kill -9 -1; sleep 5"), timeout_seconds=60, job="first"
        ),
        VerificationCommand("after-kill", ("true",), timeout_seconds=60, job="first"),
        VerificationCommand("other-job", ("true",), timeout_seconds=60, job="second"),
    )
    verifier, execution, candidate = scenario(
        tmp_path, {"src/app.py": "print('fixed')\n"}, commands
    )
    record = run(verifier, execution, candidate)
    by_name = {c.name: c for c in record.checks}
    assert not by_name["after-kill"].passed
    assert by_name["other-job"].passed
    assert record.verdict is Verdict.REJECTED_CHECKS_FAILED
