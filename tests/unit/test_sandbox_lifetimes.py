# SPDX-License-Identifier: Apache-2.0
"""Every sandbox gets a runtime-side lifetime (ADR-0029 §1, #90).

The coordinator's own limits die with it. The agent sandbox therefore lives at
most ``execution_wall_seconds`` plus a grace period, and a verification
sandbox at most the sum of its job's command timeouts plus the grace, both
enforced by the runtime. The real-Podman proof that the container stops with
no coordinator alive is ``tests/integration/test_sigkill_podman.py``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from coordinator_support import Setup, make_setup, policy, task
from ilmar.core.lifetimes import (
    SANDBOX_GRACE_SECONDS,
    agent_sandbox_lifetime,
    verification_sandbox_lifetime,
)
from ilmar.core.worker import Outcome
from ilmar.domain.verification import VerificationCommand


@pytest.fixture
def setup(tmp_path: Path) -> Setup:
    return make_setup(
        tmp_path,
        tasks=(task("1"),),
        verification=policy(
            VerificationCommand("a", ("check", "src/app.py", "fixed"), timeout_seconds=100,
                                job="tests"),
            VerificationCommand("b", ("check", "src/app.py", "fixed"), timeout_seconds=50,
                                job="tests"),
            VerificationCommand("c", ("check", "src/app.py", "fixed"), timeout_seconds=30,
                                job="lint"),
        ),
    )  # fmt: skip


def test_the_agent_sandbox_lives_the_wall_time_plus_grace(setup: Setup) -> None:
    report = setup.coordinator(execution_wall_seconds=300).run_once()
    assert report.results[0].outcome is Outcome.PUBLISHED
    agent, *verification = setup.runtime.started_specs
    assert agent.lifetime_seconds == 300 + SANDBOX_GRACE_SECONDS
    lifetimes = sorted(spec.lifetime_seconds or 0 for spec in verification)
    assert lifetimes == [30 + SANDBOX_GRACE_SECONDS, 150 + SANDBOX_GRACE_SECONDS]


def test_verification_sandboxes_always_get_a_lifetime(setup: Setup) -> None:
    setup.coordinator().run_once()
    agent, *verification = setup.runtime.started_specs
    assert agent.lifetime_seconds is None  # no wall time configured in this test
    assert verification
    assert all(spec.lifetime_seconds is not None for spec in verification)


def test_the_wall_time_must_cover_the_agent_timeout(setup: Setup) -> None:
    with pytest.raises(ValueError, match="at least agent_timeout_seconds"):
        setup.coordinator(execution_wall_seconds=59)  # the helper's agent timeout is 60
    setup.coordinator(execution_wall_seconds=60)


def test_lifetime_helpers() -> None:
    assert agent_sandbox_lifetime(1.0) == 1.0 + SANDBOX_GRACE_SECONDS
    with pytest.raises(ValueError, match="positive"):
        agent_sandbox_lifetime(0)
    commands = [VerificationCommand("x", ("true",), timeout_seconds=12.5, job="j")] * 2
    assert verification_sandbox_lifetime(commands) == 25.0 + SANDBOX_GRACE_SECONDS
