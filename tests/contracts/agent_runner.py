# SPDX-License-Identifier: Apache-2.0
"""Contract every ``AgentRunner`` adapter must satisfy.

Subclasses implement :meth:`arrange`, which prepares a container runtime and
a running container in which the adapter's agent will behave as described by
an :class:`AgentScenario`. Real adapters typically arrange a scripted fake
agent binary that speaks the real agent's output format (#8).
"""

from __future__ import annotations

import dataclasses
import enum
import threading
from collections.abc import Mapping, Sequence

import pytest

from ilmar.domain.agent import AgentOutcome, AgentRequest, AgentResult
from ilmar.domain.workspace import (
    CommandResult,
    ContainerHandle,
    ContainerInfo,
    ContainerSpec,
    ModelAccess,
    ResourceOwner,
    StopSignal,
)
from ilmar.ports.agent_runner import AgentRunner
from ilmar.ports.container_runtime import ContainerRuntime, StreamConsumer

from helpers import task


class AgentScenario(enum.Enum):
    COMPLETES = "completes"
    """Exits 0 and reports success in its structured result."""
    REPORTS_ERROR_WITH_EXIT_ZERO = "reports_error_with_exit_zero"
    """Exits 0 but its structured result reports an error (F-021)."""
    CLAIMS_SUCCESS_WITH_NONZERO_EXIT = "claims_success_with_nonzero_exit"
    """Structured result says success, but the process exits non-zero."""
    CRASHES = "crashes"
    """Exits non-zero without a structured result."""
    UNPARSEABLE_OUTPUT = "unparseable_output"
    """Exits 0 but prints no recognisable structured result."""
    HANGS = "hangs"
    """Keeps showing progress, but runs longer than the request's timeout."""
    STALLS = "stalls"
    """Shows progress, then nothing for longer than the request's stall timeout
    (ADR-0035 §3)."""


EXPECTED = {
    AgentScenario.COMPLETES: AgentOutcome.COMPLETED,
    AgentScenario.REPORTS_ERROR_WITH_EXIT_ZERO: AgentOutcome.FAILED,
    AgentScenario.CLAIMS_SUCCESS_WITH_NONZERO_EXIT: AgentOutcome.FAILED,
    AgentScenario.CRASHES: AgentOutcome.FAILED,
    AgentScenario.UNPARSEABLE_OUTPUT: AgentOutcome.FAILED,
    AgentScenario.HANGS: AgentOutcome.TIMED_OUT,
    AgentScenario.STALLS: AgentOutcome.STALLED,
}

REQUEST = AgentRequest(
    task=task("42"), instructions="Fix the bug.", timeout_seconds=60, stall_timeout_seconds=5
)


class StopObservingRuntime:
    """Wraps the arranged runtime: records the ``stop`` each command was given and
    behaves like a runtime that honours it (a set signal cancels the command)."""

    def __init__(self, inner: ContainerRuntime) -> None:
        self.inner = inner
        self.stops: list[StopSignal | None] = []

    @property
    def name(self) -> str:
        return self.inner.name

    def start(self, spec: ContainerSpec) -> ContainerHandle:
        return self.inner.start(spec)

    def find(self, name: str) -> ContainerInfo | None:
        return self.inner.find(name)

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
        self.stops.append(stop)
        if stop is not None and stop.is_set():
            return CommandResult(-9, "", "", 0.0, cancelled=True)
        return self.inner.run(
            handle, argv, timeout_seconds=timeout_seconds, env=env, stop=stop,
            idle_timeout_seconds=idle_timeout_seconds, stdout_consumer=stdout_consumer,
        )  # fmt: skip

    def is_running(self, handle: ContainerHandle) -> bool:
        return self.inner.is_running(handle)

    def model_access(self, handle: ContainerHandle) -> ModelAccess | None:
        return self.inner.model_access(handle)

    def remove_owned(self, name: str, *, owner: ResourceOwner) -> bool:
        return self.inner.remove_owned(name, owner=owner)

    def stop(self, handle: ContainerHandle, *, owner: ResourceOwner) -> None:
        self.inner.stop(handle, owner=owner)


class AgentRunnerContract:
    def make_runner(self) -> AgentRunner:
        raise NotImplementedError

    def arrange(self, scenario: AgentScenario) -> tuple[ContainerRuntime, ContainerHandle]:
        raise NotImplementedError

    def commands_run_in(self, runtime: ContainerRuntime, container: ContainerHandle) -> int:
        """How many commands the runtime executed in ``container`` (provider-specific)."""
        raise NotImplementedError

    @pytest.fixture
    def runner(self) -> AgentRunner:
        return self.make_runner()

    @pytest.mark.parametrize("scenario", list(AgentScenario), ids=lambda s: s.value)
    def test_outcome_is_derived_from_structured_result_and_exit_status(
        self, runner: AgentRunner, scenario: AgentScenario
    ) -> None:
        runtime, container = self.arrange(scenario)
        result = runner.run(runtime, container, REQUEST)
        assert result.outcome is EXPECTED[scenario]

    def test_completed_result_carries_a_summary(self, runner: AgentRunner) -> None:
        runtime, container = self.arrange(AgentScenario.COMPLETES)
        result = runner.run(runtime, container, REQUEST)
        assert result.summary.strip()
        assert result.exit_code == 0

    def test_runs_inside_the_given_container(self, runner: AgentRunner) -> None:
        runtime, container = self.arrange(AgentScenario.COMPLETES)
        before = self.commands_run_in(runtime, container)
        runner.run(runtime, container, REQUEST)
        assert self.commands_run_in(runtime, container) > before

    def test_runs_are_repeatable(self, runner: AgentRunner) -> None:
        for scenario in (AgentScenario.COMPLETES, AgentScenario.REPORTS_ERROR_WITH_EXIT_ZERO):
            runtime, container = self.arrange(scenario)
            first = runner.run(runtime, container, REQUEST)
            second = runner.run(runtime, container, REQUEST)
            assert first.outcome is second.outcome is EXPECTED[scenario]

    def test_result_cannot_express_acceptance(self, runner: AgentRunner) -> None:
        """Agent output is advisory: the result type has no acceptance or verification field."""
        runtime, container = self.arrange(AgentScenario.COMPLETES)
        result = runner.run(runtime, container, REQUEST)
        assert type(result) is AgentResult
        fields = {f.name for f in dataclasses.fields(AgentResult)}
        assert fields == {"outcome", "summary", "exit_code"}

    # -- the stop signal (ADR-0025 §2) ------------------------------------

    def test_a_set_stop_signal_is_passed_through_and_yields_cancelled(
        self, runner: AgentRunner
    ) -> None:
        runtime, container = self.arrange(AgentScenario.COMPLETES)
        observing = StopObservingRuntime(runtime)
        stop = threading.Event()
        stop.set()
        result = runner.run(observing, container, REQUEST, stop=stop)
        assert result.outcome is AgentOutcome.CANCELLED
        assert result.exit_code is None
        assert stop in observing.stops  # the very signal, unchanged, reached the runtime

    def test_an_unset_stop_signal_changes_nothing(self, runner: AgentRunner) -> None:
        runtime, container = self.arrange(AgentScenario.COMPLETES)
        observing = StopObservingRuntime(runtime)
        stop = threading.Event()
        result = runner.run(observing, container, REQUEST, stop=stop)
        assert result.outcome is AgentOutcome.COMPLETED
        assert stop in observing.stops
        assert not stop.is_set()  # a runner only reads the signal

    def test_without_a_stop_signal_nothing_is_passed(self, runner: AgentRunner) -> None:
        runtime, container = self.arrange(AgentScenario.COMPLETES)
        observing = StopObservingRuntime(runtime)
        assert runner.run(observing, container, REQUEST).outcome is AgentOutcome.COMPLETED
        assert set(observing.stops) == {None}
