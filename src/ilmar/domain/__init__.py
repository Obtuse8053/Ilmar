# SPDX-License-Identifier: Apache-2.0
"""Provider-neutral domain types. Depends only on the standard library."""

from ilmar.domain.agent import AgentOutcome, AgentRequest, AgentResult
from ilmar.domain.changes import (
    CandidateRevision,
    ChangeRequest,
    ChangeRequestSpec,
    CheckResult,
    ChecksSummary,
    CheckState,
    RepositoryRef,
)
from ilmar.domain.execution import Execution, ExecutionState, Transition
from ilmar.domain.tasks import ClaimOutcome, Task, TaskRef
from ilmar.domain.workspace import (
    CommandResult,
    ContainerHandle,
    ContainerInfo,
    ContainerSpec,
    NetworkPolicy,
    ResourceLimits,
    ResourceOwner,
)

__all__ = [
    "AgentOutcome",
    "AgentRequest",
    "AgentResult",
    "CandidateRevision",
    "ChangeRequest",
    "ChangeRequestSpec",
    "CheckResult",
    "CheckState",
    "ChecksSummary",
    "ClaimOutcome",
    "CommandResult",
    "ContainerHandle",
    "ContainerInfo",
    "ContainerSpec",
    "Execution",
    "ExecutionState",
    "NetworkPolicy",
    "RepositoryRef",
    "ResourceLimits",
    "ResourceOwner",
    "Task",
    "TaskRef",
    "Transition",
]
