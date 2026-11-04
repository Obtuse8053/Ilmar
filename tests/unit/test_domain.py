# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest

from ilmar.domain.agent import AgentRequest
from ilmar.domain.changes import CandidateRevision
from ilmar.domain.execution import Execution, ExecutionState, Transition
from ilmar.domain.tasks import Task, TaskRef
from ilmar.domain.workspace import (
    CommandResult,
    ContainerSpec,
    DirectoryIdentity,
    NetworkPolicy,
    ResourceLimits,
    ResourceOwner,
)

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


OWNER = ResourceOwner(instance_id="i", execution_id="e", attempt=1, token="t")
IDENT = DirectoryIdentity(1, 2)


def test_container_spec_defaults_to_no_network(tmp_path: Path) -> None:
    spec = ContainerSpec(
        image="img",
        host_workspace=tmp_path,
        name="ilmar-e-1",
        owner=OWNER,
        workspace_identity=IDENT,
    )
    assert spec.network is NetworkPolicy.NONE
    assert str(spec.container_workdir) == "/workspace"


@pytest.mark.parametrize("name", ["", "-leading-dash", "has space", "slash/inside", "x" * 129])
def test_container_spec_rejects_invalid_names(tmp_path: Path, name: str) -> None:
    with pytest.raises(ValueError, match="container name"):
        ContainerSpec(
            image="img", host_workspace=tmp_path, name=name, owner=OWNER, workspace_identity=IDENT
        )


@pytest.mark.parametrize(
    ("instance", "execution", "attempt", "token"),
    [("", "e", 1, "t"), ("i", "", 1, "t"), ("i", "e", 0, "t"), ("i", "e", 1, "")],
)
def test_resource_owner_validation(instance: str, execution: str, attempt: int, token: str) -> None:
    with pytest.raises(ValueError, match=r"non-empty|attempt"):
        ResourceOwner(instance_id=instance, execution_id=execution, attempt=attempt, token=token)


def test_candidate_revision_accepts_sorted_relative_paths() -> None:
    candidate = CandidateRevision("r2", "r1", ("a.txt", "src/b.py"))
    assert candidate.changed_paths == ("a.txt", "src/b.py")


@pytest.mark.parametrize(
    "paths",
    [("b", "a"), ("a", "a"), ("/abs",), ("../up",), ("./dot",), ("",)],
)
def test_candidate_revision_rejects_bad_changed_paths(paths: tuple[str, ...]) -> None:
    with pytest.raises(ValueError, match="changed"):
        CandidateRevision("r2", "r1", paths)


def test_candidate_revision_requires_revisions() -> None:
    with pytest.raises(ValueError, match="non-empty"):
        CandidateRevision("", "r1")


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


# -- network allow-list (#40) ---------------------------------------------------


def _spec(tmp_path: Path, **kwargs: object) -> ContainerSpec:
    return ContainerSpec(
        image="img",
        host_workspace=tmp_path,
        name="ilmar-e-1",
        owner=OWNER,
        workspace_identity=IDENT,
        **kwargs,  # type: ignore[arg-type]
    )


def test_allowlist_hosts_are_normalised_and_deduplicated(tmp_path: Path) -> None:
    spec = _spec(
        tmp_path,
        network=NetworkPolicy.ALLOWLIST,
        allowed_hosts=("API.Anthropic.com.", "api.anthropic.com", "example.org"),
    )
    assert spec.allowed_hosts == ("api.anthropic.com", "example.org")


@pytest.mark.parametrize(
    "host",
    [
        "1.2.3.4",
        "[::1]",
        "::1",
        "localhost",
        "exa mple.com",
        "-bad.com",
        "bad-.com",
        "under_score.com",
        "a..b",
        "",
        "host.com:443",
        "x" * 64 + ".com",
        "10.0.0.1.",
    ],
)
def test_allowlist_rejects_addresses_and_malformed_names(tmp_path: Path, host: str) -> None:
    with pytest.raises(ValueError, match="hostname"):
        _spec(tmp_path, network=NetworkPolicy.ALLOWLIST, allowed_hosts=(host,))


def test_allowlist_needs_hosts_and_hosts_need_allowlist(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="at least one"):
        _spec(tmp_path, network=NetworkPolicy.ALLOWLIST)
    for policy in (NetworkPolicy.NONE, NetworkPolicy.UNRESTRICTED):
        with pytest.raises(ValueError, match="only meaningful"):
            _spec(tmp_path, network=policy, allowed_hosts=("example.org",))
