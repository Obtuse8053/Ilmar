# SPDX-License-Identifier: Apache-2.0
"""Runtime-side lifetimes for sandboxes (ADR-0029 §1, #90).

The coordinator ends an agent run at ``agent_timeout_seconds`` and a
verification command at its own timeout. Those limits die with the
coordinator process. Each sandbox therefore also gets a lifetime that the
container runtime enforces itself, a little longer than the coordinator's own
limit, so the coordinator normally acts first and the runtime is the backstop.
"""

from __future__ import annotations

from collections.abc import Iterable

from ilmar.domain.verification import VerificationCommand

SANDBOX_GRACE_SECONDS = 60.0
"""How much longer than its coordinator-side limit a sandbox may live."""


def agent_sandbox_lifetime(execution_wall_seconds: float) -> float:
    """The agent sandbox lives at most ``execution_wall_seconds`` plus the grace."""
    if not execution_wall_seconds > 0:
        raise ValueError("execution_wall_seconds must be positive")
    return execution_wall_seconds + SANDBOX_GRACE_SECONDS


def verification_sandbox_lifetime(commands: Iterable[VerificationCommand]) -> float:
    """A verification sandbox runs its job's commands one after another, so it
    lives at most the sum of their timeouts plus the grace."""
    return sum(command.timeout_seconds for command in commands) + SANDBOX_GRACE_SECONDS
