# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest

from ilmar.domain.agent import AgentRequest
from ilmar.domain.execution import Execution, ExecutionState, Transition
from ilmar.domain.tasks import Task, TaskRef
from ilmar.domain.workspace import CommandResult, ContainerSpec, NetworkPolicy, ResourceLimits

from helpers import at


@pytest.mark.parametrize(("provider", "key"), [("", "1"), ("github", ""), (" github", "1")])
def test_task_ref_rejects_blank_or_padded_parts(provider: str, key: str) -> None:
    with pytest.raises(ValueError, match="non-empty"):
        TaskRef(provider, key)


def test_task_ref_string_form() -> None:
    assert str(TaskRef("github", "Obtuse8053/Ilmar#7")) == "github:Obtuse8053/Ilmar#7"


def test_execution_rejects_naive_timestamps() -> None:
    naive = datetime(2026, 1, 1)
    with pytest.raises(ValueError, match="timezone-aware"):
        Execution(
            id="e1",
            task=TaskRef("fake", "1"),
            state=ExecutionState.QUEUED,
            attempt=1,
            version=1,
            created_at=naive,
            updated_at=naive,
        )


@pytest.mark.parametrize(
    ("attempt", "version", "message"),
    [(0, 1, "attempt"), (1, 0, "version")],
)
def test_execution_rejects_invalid_counters(attempt: int, version: int, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        Execution(
            id="e1",
            task=TaskRef("fake", "1"),
            state=ExecutionState.QUEUED,
            attempt=attempt,
            version=version,
            created_at=at(0),
            updated_at=at(0),
        )


def test_execution_rejects_update_before_creation() -> None:
    with pytest.raises(ValueError, match="precede"):
        Execution(
            id="e1",
            task=TaskRef("fake", "1"),
            state=ExecutionState.QUEUED,
            attempt=1,
            version=1,
            created_at=at(10),
            updated_at=at(0),
        )


def test_transition_rejects_naive_timestamp() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        Transition("e1", None, ExecutionState.QUEUED, datetime(2026, 1, 1), "x")


def test_terminal_states() -> None:
    terminal = {s for s in ExecutionState if s.is_terminal}
    assert terminal == {
        ExecutionState.SUCCEEDED,
        ExecutionState.FAILED,
        ExecutionState.CANCELLED,
    }


def test_container_spec_defaults_to_no_network(tmp_path: Path) -> None:
    spec = ContainerSpec(image="img", host_workspace=tmp_path)
    assert spec.network is NetworkPolicy.NONE
    assert str(spec.container_workdir) == "/workspace"


@pytest.mark.parametrize("field", ["cpus", "memory_mb", "max_processes"])
def test_resource_limits_must_be_positive(field: str) -> None:
    with pytest.raises(ValueError, match=field):
        ResourceLimits(**{field: 0})


def test_command_result_success_requires_zero_exit_and_no_timeout() -> None:
    assert CommandResult(0, "", "", 1.0).succeeded
    assert not CommandResult(1, "", "", 1.0).succeeded
    assert not CommandResult(0, "", "", 1.0, timed_out=True).succeeded


def test_agent_request_requires_positive_timeout() -> None:
    task = Task(TaskRef("fake", "1"), "t", "d")
    with pytest.raises(ValueError, match="timeout"):
        AgentRequest(task=task, instructions="do it", timeout_seconds=0)
