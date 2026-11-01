# SPDX-License-Identifier: Apache-2.0
"""Requests to and results from coding agents."""

from __future__ import annotations

import enum
from dataclasses import dataclass

from ilmar.domain.tasks import Task


@dataclass(frozen=True, slots=True)
class AgentRequest:
    task: Task
    instructions: str
    timeout_seconds: float

    def __post_init__(self) -> None:
        if self.timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")


class AgentOutcome(enum.Enum):
    """How the agent process ended.

    ``COMPLETED`` only means the agent stopped normally and reported that it
    finished. It is never treated as proof that the work is correct; the core
    runs independent verification afterwards.
    """

    COMPLETED = "completed"
    FAILED = "failed"
    TIMED_OUT = "timed_out"


@dataclass(frozen=True, slots=True)
class AgentResult:
    outcome: AgentOutcome
    summary: str
    exit_code: int | None = None
