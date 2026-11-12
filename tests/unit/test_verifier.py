# SPDX-License-Identifier: Apache-2.0
"""The Verifier and the ADR-0010 §5 outcome table (#9, ADR-0018)."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path

import pytest

from ilmar.adapters.memory import InMemoryExecutionStore
from ilmar.core.layout import WorkspaceLayout
from ilmar.core.secret_scan import SecretScanner
from ilmar.core.state_machine import ExecutionStateMachine
from ilmar.core.verification import Verifier
from ilmar.domain.agent import AgentOutcome, AgentResult
from ilmar.domain.changes import CandidateRevision, RepositoryRef
from ilmar.domain.errors import ContainmentError, ProviderError
from ilmar.domain.execution import Execution, ExecutionState
from ilmar.domain.safety_controls import safety_controls
from ilmar.domain.verification import (
    CheckClass,
    EvidenceRecord,
    Independence,
    Verdict,
    VerificationCommand,
    VerificationPolicy,
)
from ilmar.domain.workspace import (
    CommandResult,
    ContainerHandle,
    ContainerSpec,
    NetworkPolicy,
    ResourceLimits,
    ResourceOwner,
    StopSignal,
)
from ilmar.ports.container_runtime import StreamConsumer

from fakes import FakeContainerRuntime, FakeSourceControl
from helpers import at, task

S = ExecutionState
REPO = RepositoryRef("fake", "owner/repo")
BASE_FILES = {"README.md": "hi\n", "src/app.py": "print(1)\n", "tests/test_app.py": "ok\n"}
COMPLETED = AgentResult(AgentOutcome.COMPLETED, "I fixed it and all tests pass.", 0)


class RecordingRuntime(FakeContainerRuntime):
    """Records sandboxes, and offers programs that touch their own sandbox's state.

    ``tamper`` writes into the checkout and leaves a background "process";
    ``clean`` fails if it sees either. They show what a job can and cannot
    affect.
    """

    def __init__(self) -> None:
        super().__init__()
        self.started: list[ContainerSpec] = []
        self.stopped: list[str] = []
        self.processes: dict[str, set[str]] = {}

    def run(
        self,
        handle: ContainerHandle,
        argv: Sequence[str],
        *,
        timeout_seconds: float,
        env: Mapping[str, str] | None = None,
        stop: StopSignal | None = None,
        idle_timeout_seconds: float | None = None,
        stdout_consumer: StreamConsumer | None = None,
    ) -> CommandResult:
        if argv and argv[0] == "kill-sandbox":  # the candidate ends its own sandbox
            self._containers[handle.id].running = False
            return CommandResult(137, "", "Killed\n", 0.1)
        if argv and argv[0] == "infrastructure-error":
            raise ProviderError("podman is broken")
        if argv and argv[0] == "containment-error":
            raise ContainmentError("removal could not be confirmed", container_id="c", name="n")
        if argv and argv[0] in {"tamper", "clean"}:
            workspace = self._containers[handle.id].spec.host_workspace
            strays = self.processes.setdefault(handle.id, set())
            if argv[0] == "tamper":
                (workspace / "tests" / "test_app.py").write_text("assert True  # weakened\n")
                strays.add("background-helper")
                return CommandResult(0, "", "", 0.1)
            dirty = (workspace / "tests" / "test_app.py").read_text() != "ok\n" or strays
            return CommandResult(1 if dirty else 0, "", "", 0.1)
        return super().run(
            handle, argv, timeout_seconds=timeout_seconds, env=env, stop=stop,
            idle_timeout_seconds=idle_timeout_seconds, stdout_consumer=stdout_consumer,
        )  # fmt: skip

    def start(self, spec: ContainerSpec) -> ContainerHandle:
        self.started.append(spec)
        return super().start(spec)

    def stop(self, handle: ContainerHandle, *, owner: ResourceOwner) -> None:
        self.stopped.append(handle.id)
        super().stop(handle, owner=owner)


def outcome(code: int, stderr: str = "") -> CommandResult:
    return CommandResult(code, "out\n", stderr, 0.5)


@dataclass
class World:
    tmp: Path
    layout: WorkspaceLayout
    scm: FakeSourceControl = field(default_factory=FakeSourceControl)
    runtime: RecordingRuntime = field(default_factory=RecordingRuntime)
    store: InMemoryExecutionStore = field(default_factory=InMemoryExecutionStore)
    program_results: dict[str, CommandResult] = field(default_factory=dict)
    policy: VerificationPolicy | None = None

    def __post_init__(self) -> None:
        self.scm.seed(REPO, "main", BASE_FILES)
        for name in ("lint", "tests", "slow", "optional"):
            self.runtime.register(name, self._program(name))

    def _program(
        self, name: str
    ) -> Callable[[Sequence[str], float, Mapping[str, str]], CommandResult]:
        def run(argv: Sequence[str], timeout: float, env: Mapping[str, str]) -> CommandResult:
            result = self.program_results.get(name, outcome(0))
            if result.timed_out:
                return CommandResult(-9, "", "", timeout, timed_out=True)
            return result

        return run

    def candidate(self, changes: Mapping[str, str | None]) -> tuple[Execution, CandidateRevision]:
        workspace = self.tmp / f"agent-{len(list(self.tmp.iterdir()))}"
        base = self.scm.checkout(REPO, base_branch="main", branch="ilmar/t", destination=workspace)
        for path, content in changes.items():
            target = workspace / path
            if content is None:
                target.unlink()
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(content)
        candidate = self.scm.freeze_candidate(
            REPO, base_revision=base, workspace=workspace, message="m"
        )
        return self.execution(candidate), candidate

    def execution(self, candidate: CandidateRevision) -> Execution:
        machine = ExecutionStateMachine(max_attempts=3)
        execution, transition = machine.start(task("9").ref, execution_id="e9", now=at(0))
        if self.store.get("e9") is None:
            self.store.create(execution, transition)
        for step, target in enumerate((S.CLAIMED, S.PROVISIONING, S.RUNNING_AGENT, S.VERIFYING)):
            execution, _ = machine.advance(
                execution,
                target,
                now=at(step + 1),
                reason="t",
                base_revision=candidate.base_revision if target is S.RUNNING_AGENT else None,
                candidate_revision=candidate.revision if target is S.VERIFYING else None,
            )
        return execution

    def verifier(
        self,
        *commands: VerificationCommand,
        scanner: SecretScanner | None = None,
        **kwargs: object,
    ) -> Verifier:
        policy = self.policy = VerificationPolicy(
            image="localhost/verify:1",
            commands=commands or (VerificationCommand("tests", ("tests",)),),
            limits=ResourceLimits(memory_mb=512),
            **kwargs,  # type: ignore[arg-type]
        )
        return Verifier(
            source_control=self.scm,
            runtime=self.runtime,
            store=self.store,
            layout=self.layout,
            policy=policy,
            instance_id="inst",
            clock=lambda: at(100),
            scanner=scanner,
        )


OWNER = ResourceOwner(instance_id="inst", execution_id="e9", attempt=1, token="tok")


@pytest.fixture
def world(tmp_path: Path) -> World:
    layout = WorkspaceLayout.under(tmp_path.resolve() / "state")
    layout.workspaces_root.mkdir(parents=True)
    agents = tmp_path / "agents"
    agents.mkdir()
    return World(agents, layout)


def verify(
    world: World,
    changes: Mapping[str, str | None],
    *commands: VerificationCommand,
    run: int = 1,
    agent: AgentResult = COMPLETED,
) -> EvidenceRecord:
    execution, candidate = world.candidate(changes)
    return world.verifier(*commands).verify(
        execution=execution,
        repository=REPO,
        candidate=candidate,
        agent_result=agent,
        owner=OWNER,
        run=run,
    )


# -- the ADR-0010 §5 table, one test per row ------------------------------------------


def test_row_failed_required_check_is_not_published(world: World) -> None:
    world.program_results["tests"] = outcome(1, "AssertionError: 1 != 2")
    record = verify(world, {"src/app.py": "print(2)\n"})
    assert record.verdict is Verdict.REJECTED_CHECKS_FAILED
    assert not record.verdict.publishable
    (tests,) = [c for c in record.checks if c.name == "tests"]
    assert not tests.passed
    assert tests.exit_code == 1
    assert "reported by the candidate" in tests.detail


def test_row_unchanged_checks_all_pass_is_published(world: World) -> None:
    record = verify(world, {"src/app.py": "print(2)\n"})
    assert record.verdict is Verdict.PUBLISH
    assert record.independence is Independence.UNCHANGED_CHECKS
    assert not record.requires_human_review
    assert record.review_reasons == ()


def test_row_modified_checks_pass_is_published_for_review(world: World) -> None:
    record = verify(world, {"src/app.py": "print(2)\n", "tests/test_app.py": "weakened\n"})
    assert record.verdict is Verdict.PUBLISH_FOR_REVIEW
    assert record.independence is Independence.MODIFIED_CHECKS
    assert record.requires_human_review
    assert record.check_influencing_paths == ("tests/test_app.py",)
    assert record.review_reasons == ("modified-checks: tests/test_app.py",)


def test_row_ci_configuration_is_never_published_and_nothing_runs(world: World) -> None:
    record = verify(world, {".github/workflows/ci.yml": "on: push\n"})
    assert record.verdict is Verdict.REJECTED_CI_CONFIG
    assert record.independence is Independence.CI_CONFIG_MODIFIED
    assert record.ci_config_paths == (".github/workflows/ci.yml",)
    assert world.runtime.started == []  # no candidate code ran at all
    assert [c.check_class for c in record.checks] == [CheckClass.INDEPENDENT] * 3
    (ci,) = [c for c in record.checks if c.name == "ci-config-paths"]
    assert not ci.passed


def test_ci_configuration_wins_even_when_checks_would_pass(world: World) -> None:
    record = verify(world, {".github/x.yml": "x\n", "tests/test_app.py": "x\n"})
    assert record.verdict is Verdict.REJECTED_CI_CONFIG


# -- protected paths ------------------------------------------------------------------


def test_protected_paths_always_require_review(world: World) -> None:
    passed = verify(world, {"AGENTS.md": "new rules\n"})
    assert passed.verdict is Verdict.PUBLISH_FOR_REVIEW
    assert passed.review_reasons == ("protected-paths: AGENTS.md",)
    world.program_results["tests"] = outcome(1)
    failed = verify(world, {"AGENTS.md": "new rules\n"}, run=2)
    assert failed.verdict is Verdict.REJECTED_CHECKS_FAILED
    assert failed.requires_human_review


# -- trust boundary (ADR-0010 §4) ------------------------------------------------------------


def test_pass_or_fail_comes_only_from_the_harness_exit_status(world: World) -> None:
    """A candidate that prints success but exits non-zero has failed."""
    world.program_results["tests"] = CommandResult(1, "PASSED: 100 tests\nOK\n", "", 0.1)
    record = verify(world, {"src/app.py": "print(2)\n", "report.xml": '<testsuite failures="0"/>'})
    assert record.verdict is Verdict.REJECTED_CHECKS_FAILED


def test_the_agents_claim_never_decides(world: World) -> None:
    world.program_results["tests"] = outcome(1)
    rejected = verify(world, {"src/app.py": "print(2)\n"})
    assert rejected.verdict is Verdict.REJECTED_CHECKS_FAILED
    assert rejected.agent_report.outcome == "completed"
    world.program_results["tests"] = outcome(0)
    claimed_failure = AgentResult(AgentOutcome.FAILED, "I gave up", 1)
    published = verify(world, {"src/app.py": "print(3)\n"}, run=2, agent=claimed_failure)
    assert published.verdict is Verdict.PUBLISH
    assert published.agent_report.outcome == "failed"


def test_verification_sandbox_has_no_network_no_credential_and_a_fresh_checkout(
    world: World,
) -> None:
    record = verify(world, {"src/app.py": "print(2)\n"})
    (spec,) = world.runtime.started
    assert spec.network is NetworkPolicy.NONE
    assert dict(spec.env) == {}
    assert spec.image == "localhost/verify:1"
    assert spec.limits == ResourceLimits(memory_mb=512)
    assert spec.name == "ilmar-inst-e9-1-verify-1-default"
    assert spec.host_workspace == world.layout.verification_checkout("e9", 1, 1) / "default"
    assert (spec.host_workspace / "src" / "app.py").read_text() == "print(2)\n"
    assert spec.owner == OWNER
    assert world.runtime.stopped  # the sandbox is discarded
    assert record.candidate_revision != record.base_revision


def test_each_run_uses_a_new_checkout_and_sandbox(world: World) -> None:
    verify(world, {"src/app.py": "print(2)\n"}, run=1)
    verify(world, {"src/app.py": "print(2)\n"}, run=2)
    names = [spec.name for spec in world.runtime.started]
    assert names == ["ilmar-inst-e9-1-verify-1-default", "ilmar-inst-e9-1-verify-2-default"]
    with pytest.raises(ProviderError):  # a checkout directory is never reused
        verify(world, {"src/app.py": "print(2)\n"}, run=1)


# -- commands -------------------------------------------------------------------------------


def test_every_command_runs_and_is_recorded(world: World) -> None:
    lint = VerificationCommand("lint", ("lint", "--strict"))
    tests = VerificationCommand("tests", ("tests", "-q"))
    world.program_results["lint"] = CommandResult(0, "fine\n", "", 0.25)
    record = verify(world, {"src/app.py": "print(2)\n"}, lint, tests)
    executed = [c for c in record.checks if c.check_class is CheckClass.CANDIDATE_EXECUTED]
    assert [(c.name, c.command, c.exit_code, c.passed) for c in executed] == [
        ("lint", ("lint", "--strict"), 0, True),
        ("tests", ("tests", "-q"), 0, True),
    ]
    assert all(c.output_digest and c.output_digest.startswith("sha256:") for c in executed)
    assert executed[0].duration_seconds == 0.25


def test_a_timeout_fails_the_check_and_later_checks_do_not_run(world: World) -> None:
    world.program_results["slow"] = CommandResult(-9, "", "", 0, timed_out=True)
    slow = VerificationCommand("slow", ("slow",), timeout_seconds=5)
    tests = VerificationCommand("tests", ("tests",))
    record = verify(world, {"src/app.py": "print(2)\n"}, slow, tests)
    by_name = {c.name: c for c in record.checks}
    assert by_name["slow"].timed_out
    assert by_name["slow"].detail == "timed out after 5s"
    assert "not run" in by_name["tests"].detail
    assert record.verdict is Verdict.REJECTED_CHECKS_FAILED
    assert world.runtime.stopped == []  # the runtime already removed the sandbox


def test_optional_checks_are_recorded_but_do_not_block(world: World) -> None:
    world.program_results["optional"] = outcome(1)
    optional = VerificationCommand("optional", ("optional",), required=False)
    tests = VerificationCommand("tests", ("tests",))
    record = verify(world, {"src/app.py": "print(2)\n"}, optional, tests)
    assert record.verdict is Verdict.PUBLISH
    assert [c.passed for c in record.checks if c.name == "optional"] == [False]


# -- evidence ------------------------------------------------------------------------------


def test_evidence_is_appended_before_it_is_returned(world: World) -> None:
    record = verify(world, {"src/app.py": "print(2)\n"})
    assert world.store.evidence("e9") == (record,)
    assert world.policy is not None
    assert record.policy_digest == world.policy.digest()


def test_a_wrong_candidate_or_owner_is_refused(world: World) -> None:
    execution, candidate = world.candidate({"src/app.py": "print(2)\n"})
    verifier = world.verifier()
    other = CandidateRevision("f" * 40, candidate.base_revision, ())
    with pytest.raises(ValueError, match="candidate"):
        verifier.verify(
            execution=execution, repository=REPO, candidate=other,
            agent_result=COMPLETED, owner=OWNER,
        )  # fmt: skip
    with pytest.raises(ValueError, match="owner"):
        verifier.verify(
            execution=execution, repository=REPO, candidate=candidate, agent_result=COMPLETED,
            owner=ResourceOwner(instance_id="inst", execution_id="e9", attempt=2, token="t"),
        )  # fmt: skip
    assert world.store.evidence("e9") == ()


def test_only_a_verifying_execution_can_be_verified(world: World) -> None:
    execution, candidate = world.candidate({"src/app.py": "print(2)\n"})
    machine = ExecutionStateMachine(max_attempts=3)
    publishing, _ = machine.advance(
        execution, S.PUBLISHING, now=execution.updated_at + timedelta(seconds=1), reason="t"
    )
    with pytest.raises(ValueError, match="not verifying"):
        world.verifier().verify(
            execution=publishing, repository=REPO, candidate=candidate,
            agent_result=COMPLETED, owner=OWNER,
        )  # fmt: skip


# -- isolation boundary: the job (PR #45 review) ------------------------------------------


def test_commands_of_one_job_share_one_checkout_and_sandbox(world: World) -> None:
    lint = VerificationCommand("lint", ("lint",), job="static")
    types = VerificationCommand("types", ("tests",), job="static")
    record = verify(world, {"src/app.py": "print(2)\n"}, lint, types)
    (spec,) = world.runtime.started  # one sandbox start and one checkout for the job
    assert spec.name.endswith("-verify-1-static")
    assert [c.job for c in record.checks if c.check_class is CheckClass.CANDIDATE_EXECUTED] == [
        "static",
        "static",
    ]


def test_each_job_gets_its_own_checkout_and_sandbox(world: World) -> None:
    commands = (
        VerificationCommand("lint", ("lint",), job="static"),
        VerificationCommand("unit", ("tests",), job="unit"),
        VerificationCommand("types", ("tests",), job="static"),
    )
    verify(world, {"src/app.py": "print(2)\n"}, *commands)
    names = [spec.name for spec in world.runtime.started]
    paths = [spec.host_workspace for spec in world.runtime.started]
    assert names == ["ilmar-inst-e9-1-verify-1-static", "ilmar-inst-e9-1-verify-1-unit"]
    assert len(set(paths)) == 2
    assert len(world.runtime.stopped) == 2


def test_a_job_cannot_affect_another_jobs_files_or_processes(world: World) -> None:
    """The regression the review asked for: interference stops at the job boundary."""
    commands = (
        VerificationCommand("tamper", ("tamper",), job="first"),
        VerificationCommand("clean-second", ("clean",), job="second"),
    )
    record = verify(world, {"src/app.py": "print(2)\n"}, *commands)
    by_name = {c.name: c for c in record.checks}
    assert by_name["tamper"].passed
    assert by_name["clean-second"].passed  # fresh checkout, no stray processes
    first, second = world.runtime.started
    assert (first.host_workspace / "tests" / "test_app.py").read_text() != "ok\n"
    assert (second.host_workspace / "tests" / "test_app.py").read_text() == "ok\n"


def test_within_a_job_later_commands_see_earlier_ones(world: World) -> None:
    """The documented trade-off: steps of one job share state, as in CI."""
    commands = (
        VerificationCommand("tamper", ("tamper",), job="shared"),
        VerificationCommand("clean-after", ("clean",), job="shared"),
    )
    record = verify(world, {"src/app.py": "print(2)\n"}, *commands)
    assert not {c.name: c for c in record.checks}["clean-after"].passed


def test_a_timeout_ends_only_its_own_job(world: World) -> None:
    world.program_results["slow"] = CommandResult(-9, "", "", 0, timed_out=True)
    commands = (
        VerificationCommand("slow", ("slow",), job="first"),
        VerificationCommand("after-slow", ("tests",), job="first"),
        VerificationCommand("other-job", ("tests",), job="second"),
    )
    record = verify(world, {"src/app.py": "print(2)\n"}, *commands)
    by_name = {c.name: c for c in record.checks}
    assert "not run" in by_name["after-slow"].detail
    assert by_name["other-job"].passed
    assert record.verdict is Verdict.REJECTED_CHECKS_FAILED


def test_job_names_are_validated() -> None:
    with pytest.raises(ValueError, match="job"):
        VerificationCommand("x", ("x",), job="../evil")


# -- adversarial review of cf68225 ---------------------------------------------------


def test_an_existing_run_directory_is_refused_before_anything_runs(world: World) -> None:
    execution, candidate = world.candidate({"src/app.py": "print(2)\n"})
    run_directory = world.layout.verification_checkout("e9", 1, 1)
    run_directory.mkdir(parents=True)  # empty: no job checkout would collide
    with pytest.raises(ProviderError, match="already exists"):
        world.verifier().verify(
            execution=execution,
            repository=REPO,
            candidate=candidate,
            agent_result=COMPLETED,
            owner=OWNER,
        )
    assert world.runtime.started == []
    assert world.store.evidence("e9") == ()


def test_a_candidate_that_kills_its_sandbox_is_rejected_with_evidence(world: World) -> None:
    commands = (
        VerificationCommand("kill", ("kill-sandbox",), job="first"),
        VerificationCommand("after-kill", ("tests",), job="first"),
        VerificationCommand("later", ("tests",), job="first"),
        VerificationCommand("other-job", ("tests",), job="second"),
    )
    record = verify(world, {"src/app.py": "print(2)\n"}, *commands)
    by_name = {c.name: c for c in record.checks}
    assert not by_name["kill"].passed
    assert by_name["after-kill"].detail == "failed: the sandbox stopped while the command ran"
    assert by_name["later"].detail.startswith("not run: the job's sandbox stopped")
    assert by_name["other-job"].passed
    assert record.verdict is Verdict.REJECTED_CHECKS_FAILED
    assert world.store.evidence("e9") == (record,)
    assert len(world.runtime.stopped) == 2  # the stopped sandbox is still removed


def test_an_infrastructure_error_produces_no_verdict_and_removes_the_sandbox(
    world: World,
) -> None:
    with pytest.raises(ProviderError, match="podman is broken"):
        verify(
            world, {"src/app.py": "print(2)\n"}, VerificationCommand("x", ("infrastructure-error",))
        )
    assert world.store.evidence("e9") == ()
    assert len(world.runtime.stopped) == 1


def test_a_containment_error_is_never_turned_into_evidence(world: World) -> None:
    with pytest.raises(ContainmentError):
        verify(
            world, {"src/app.py": "print(2)\n"}, VerificationCommand("x", ("containment-error",))
        )
    assert world.store.evidence("e9") == ()


def test_candidate_stderr_cannot_inject_control_sequences_into_evidence(world: World) -> None:
    world.program_results["tests"] = outcome(1, "\x1b[2J\x1b]8;;http://evil\x07click\rfake PASSED")
    record = verify(world, {"src/app.py": "print(2)\n"})
    (check,) = [c for c in record.checks if c.name == "tests"]
    assert all(c.isprintable() for c in check.detail)
    assert "\x1b" not in check.detail


# -- the candidate secret scan (#110, ADR-0038) ---------------------------------------

SECRET = "sk-ant-verifier-secret-0123456789"
SCANNER = SecretScanner({"ANTHROPIC_API_KEY": SECRET})


def scan_verify(world: World, changes: Mapping[str, str | None]) -> EvidenceRecord:
    execution, candidate = world.candidate(changes)
    return world.verifier(scanner=SCANNER).verify(
        execution=execution, repository=REPO, candidate=candidate, agent_result=COMPLETED,
        owner=OWNER,
    )  # fmt: skip


def test_a_candidate_with_a_secret_is_rejected_and_nothing_of_it_runs(world: World) -> None:
    record = scan_verify(world, {"src/app.py": f"KEY = '{SECRET}'\n"})
    assert record.verdict is Verdict.REJECTED_CANDIDATE_SECRET
    assert not record.verdict.publishable
    assert world.runtime.started == []
    (scan,) = [c for c in record.checks if c.name == "candidate-secrets"]
    assert not scan.passed
    assert scan.check_class is CheckClass.INDEPENDENT
    assert scan.detail == "found ANTHROPIC_API_KEY (raw) in src/app.py"
    assert SECRET not in record.to_json()


def test_the_scan_wins_over_a_ci_configuration_rejection(world: World) -> None:
    encoded = SECRET.encode().hex()
    record = scan_verify(world, {".github/workflows/ci.yml": f"env: {encoded}\n"})
    assert record.verdict is Verdict.REJECTED_CANDIDATE_SECRET
    assert record.independence is Independence.CI_CONFIG_MODIFIED


def test_a_clean_candidate_passes_the_scan_and_is_verified_as_before(world: World) -> None:
    record = scan_verify(world, {"src/app.py": "print(2)\n"})
    assert record.verdict is Verdict.PUBLISH
    assert record.checks[0].name == "candidate-secrets"
    assert record.checks[0].passed
    assert "plain encodings only" in record.checks[0].detail
    assert world.runtime.started  # the candidate's checks ran


def test_only_changed_paths_are_scanned(world: World) -> None:
    world.scm.seed(REPO, "main", {**BASE_FILES, "README.md": f"{SECRET}\n"})
    record = scan_verify(world, {"src/app.py": "print(2)\n"})
    assert record.verdict is Verdict.PUBLISH  # the base already held it; the agent did not add it


def test_the_scan_checkout_is_inside_the_run_directory_and_is_not_a_job(world: World) -> None:
    scan_verify(world, {"src/app.py": "print(2)\n"})
    run = world.layout.verification_checkout("e9", 1, 1)
    assert (run / ".candidate" / "src" / "app.py").read_text() == "print(2)\n"


def test_without_configured_secrets_nothing_changes(world: World) -> None:
    record = verify(world, {"src/app.py": f"KEY = '{SECRET}'\n"})
    assert record.verdict is Verdict.PUBLISH
    assert all(c.name != "candidate-secrets" for c in record.checks)


ENCODED = SECRET.encode().hex()


@pytest.mark.parametrize(
    ("leaked", "field_name", "reason"),
    [
        (f".github/workflows/{SECRET}.yml", "ci_config_paths", "ci-config-modified"),
        (f".claude/{ENCODED}.json", "protected_paths", "protected-paths"),
        (f"tests/test_{SECRET}.py", "check_influencing_paths", "modified-checks"),
    ],
)
def test_a_secret_bearing_path_is_classified_but_never_recorded(
    world: World, leaked: str, field_name: str, reason: str
) -> None:
    """The class still applies (the reason is there); only the name is withheld."""
    record = scan_verify(world, {leaked: "x\n", f"src/{SECRET}.py": SECRET})
    assert record.verdict is Verdict.REJECTED_CANDIDATE_SECRET
    assert record.requires_human_review
    placeholder = "[a path withheld: its name holds ANTHROPIC_API_KEY]"
    assert getattr(record, field_name) == (placeholder,)
    assert any(r.startswith(f"{reason}: {placeholder}") for r in record.review_reasons)
    text = record.to_json()
    assert SECRET not in text
    assert ENCODED not in text
    (scan,) = [c for c in record.checks if c.name == "candidate-secrets"]
    assert placeholder in scan.detail  # the file's content finding names no path either


def test_paths_without_a_secret_are_recorded_as_before(world: World) -> None:
    record = scan_verify(world, {"tests/test_app.py": "x\n", ".claude/settings.json": "{}\n"})
    assert record.check_influencing_paths == ("tests/test_app.py",)
    assert record.protected_paths == (".claude/settings.json",)


# -- protected safety controls (#92, ADR-0031) ---------------------------------------

SAFETY = safety_controls(self_repository=True)


def safety_verify(
    world: World, changes: Mapping[str, str | None], links: Mapping[str, str] = {}
) -> EvidenceRecord:
    workspace = world.tmp / f"agent-{len(list(world.tmp.iterdir()))}"
    base = world.scm.checkout(REPO, base_branch="main", branch="ilmar/t", destination=workspace)
    for path, content in changes.items():
        target = workspace / path
        if content is None:
            target.unlink()
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content)
    for link, to in links.items():
        (workspace / link).parent.mkdir(parents=True, exist_ok=True)
        (workspace / link).symlink_to(to)
    candidate = world.scm.freeze_candidate(REPO, base_revision=base, workspace=workspace,
                                           message="m")  # fmt: skip
    return world.verifier(safety_controls=SAFETY).verify(
        execution=world.execution(candidate), repository=REPO, candidate=candidate,
        agent_result=COMPLETED, owner=OWNER,
    )  # fmt: skip


@pytest.mark.parametrize(
    "changes",
    [
        {"src/ilmar/config.py": "x\n"},  # configuration validation
        {"src/ilmar/domain/safety_controls.py": "EVERY_TARGET = ()\n"},  # the list itself
        {"src/ilmar/domain/verification.py": "x\n"},  # the verification policy
        {"tests/architecture/test_x.py": "x\n"},
        {"README.md": None, "AGENTS.md": "moved\n"},  # a rename onto a control
    ],
)
def test_a_candidate_touching_a_safety_control_is_rejected_and_nothing_runs(
    world: World, changes: Mapping[str, str | None]
) -> None:
    record = safety_verify(world, {"src/app.py": "print(2)\n", **changes})
    assert record.verdict is Verdict.REJECTED_PROTECTED_PATH
    assert not record.verdict.publishable
    assert world.runtime.started == []
    (check,) = [c for c in record.checks if c.name == "safety-controls"]
    assert not check.passed
    for path in changes:
        if changes[path] is not None:
            assert path in check.detail
    assert record.review_reasons[0] == check.detail


def test_a_link_to_a_safety_control_is_rejected(world: World) -> None:
    record = safety_verify(world, {}, links={"docs/policy.py": "../src/ilmar/config.py"})
    assert record.verdict is Verdict.REJECTED_PROTECTED_PATH
    (check,) = [c for c in record.checks if c.name == "safety-controls"]
    assert "docs/policy.py" in check.detail


def test_a_candidate_clear_of_safety_controls_is_verified_as_before(world: World) -> None:
    record = safety_verify(world, {"src/app.py": "print(2)\n"})
    assert record.verdict is Verdict.PUBLISH
    (check,) = [c for c in record.checks if c.name == "safety-controls"]
    assert check.passed
    assert check.detail == "none"


def test_ci_configuration_in_a_target_with_safety_controls_is_protected(world: World) -> None:
    record = safety_verify(world, {".github/workflows/ci.yml": "on: push\n"})
    assert record.verdict is Verdict.REJECTED_PROTECTED_PATH  # wins over the CI rejection


@pytest.mark.parametrize("path", ["docs/AGENTS.md", "CLAUDE.md", ".claude/settings.json"])
def test_agent_instructions_elsewhere_stay_review_only(world: World, path: str) -> None:
    """Hard-blocked only in Ilmar's own repository; other targets keep the review flag."""
    execution, candidate = world.candidate({"src/app.py": "print(2)\n", path: "x\n"})
    record = world.verifier(safety_controls=safety_controls(self_repository=False)).verify(
        execution=execution, repository=REPO, candidate=candidate, agent_result=COMPLETED,
        owner=OWNER,
    )  # fmt: skip
    assert record.verdict is Verdict.PUBLISH_FOR_REVIEW
    assert record.protected_paths == (path,)
