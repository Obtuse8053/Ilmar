# SPDX-License-Identifier: Apache-2.0
"""Runtime-side lifetimes for sandboxes (ADR-0029 §1, #90).

The coordinator ends an agent run at ``agent_timeout_seconds`` and a
verification command at its own timeout. Those limits die with the
coordinator process. Each sandbox therefore also gets a lifetime that the
container runtime enforces itself, a little longer than the coordinator's own
limit, so the coordinator normally acts first and the runtime is the backstop.
"""

from __future__ import annotations

import math
from collections.abc import Iterable

from ilmar.domain.verification import VerificationCommand

SANDBOX_GRACE_SECONDS = 60.0
"""How much longer than its coordinator-side limit a sandbox may live."""

DEFAULT_WALL_MARGIN_SECONDS = 600.0
"""``execution_wall_seconds`` when it is not given: ``agent_timeout_seconds`` plus this."""


def execution_wall(agent_timeout_seconds: float, execution_wall_seconds: float | None) -> float:
    """The wall time of an attempt: as given, or derived from the agent timeout.

    Never unbounded: there is no way to ask for an agent sandbox without a
    runtime-side lifetime (ADR-0029 §1).
    """
    if not (math.isfinite(agent_timeout_seconds) and agent_timeout_seconds > 0):
        raise ValueError("agent_timeout_seconds must be a positive, finite number")
    if execution_wall_seconds is None:
        return agent_timeout_seconds + DEFAULT_WALL_MARGIN_SECONDS
    if not math.isfinite(execution_wall_seconds):
        raise ValueError("execution_wall_seconds must be finite")
    if execution_wall_seconds < agent_timeout_seconds:
        raise ValueError("execution_wall_seconds must be at least agent_timeout_seconds")
    return execution_wall_seconds


def agent_sandbox_lifetime(execution_wall_seconds: float) -> float:
    """The agent sandbox lives at most ``execution_wall_seconds`` plus the grace."""
    if not (math.isfinite(execution_wall_seconds) and execution_wall_seconds > 0):
        raise ValueError("execution_wall_seconds must be a positive, finite number")
    return execution_wall_seconds + SANDBOX_GRACE_SECONDS


def verification_sandbox_lifetime(commands: Iterable[VerificationCommand]) -> float:
    """A verification sandbox runs its job's commands one after another, so it
    lives at most the sum of their timeouts plus the grace."""
    return sum(command.timeout_seconds for command in commands) + SANDBOX_GRACE_SECONDS
