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
    stall_timeout_seconds: float | None = None
    """End the run when the agent shows no progress for this long (ADR-0035 §3);
    ``None``: no stall detection. Never longer than ``timeout_seconds``."""

    def __post_init__(self) -> None:
        if self.timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        stall = self.stall_timeout_seconds
        if stall is not None and not 0 < stall <= self.timeout_seconds:
            raise ValueError("stall_timeout_seconds must be positive and at most timeout_seconds")


class AgentOutcome(enum.Enum):
    """How the agent process ended.

    ``COMPLETED`` only means the agent stopped normally and reported that it
    finished. It is never treated as proof that the work is correct; the core
    runs independent verification afterwards.
    """

    COMPLETED = "completed"
    FAILED = "failed"
    TIMED_OUT = "timed_out"
    CANCELLED = "cancelled"  # stopped by a stop signal (ADR-0025 §2); never frozen
    STALLED = "stalled"  # no progress for the stall timeout (ADR-0035 §3); retried
    RATE_LIMITED = "rate_limited"  # an ordinary provider rate limit (ADR-0029 §5); retried
    PROVIDER_REFUSED = "provider_refused"  # spend cap, billing or auth (ADR-0029 §5); never retried


@dataclass(frozen=True, slots=True)
class AgentResult:
    outcome: AgentOutcome
    summary: str
    exit_code: int | None = None
