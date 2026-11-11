# SPDX-License-Identifier: Apache-2.0
"""Every sandbox gets a runtime-side lifetime (ADR-0029 §1, #90).

The coordinator's own limits die with it. The agent sandbox therefore lives at
most ``execution_wall_seconds`` plus a grace period, and a verification
sandbox at most the sum of its job's command timeouts plus the grace, both
enforced by the runtime. The real-Podman proof that the container stops with
no coordinator alive is ``tests/integration/test_sigkill_podman.py``.
"""

from __future__ import annotations

import inspect
import math
from pathlib import Path

import pytest

from coordinator_support import Setup, make_setup, policy, task
from ilmar.config import WALL_MARGIN_SECONDS
from ilmar.core.effects import Effects
from ilmar.core.lifetimes import (
    DEFAULT_WALL_MARGIN_SECONDS,
    SANDBOX_GRACE_SECONDS,
    agent_sandbox_lifetime,
    execution_wall,
    verification_sandbox_lifetime,
)
from ilmar.core.worker import Outcome
from ilmar.domain.agent import AgentOutcome
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


def test_an_omitted_wall_time_is_derived_from_the_agent_timeout(setup: Setup) -> None:
    """Regression (#108 review): omitting ``execution_wall_seconds`` used to
    start the agent sandbox with no runtime-side lifetime at all."""
    setup.coordinator().run_once()  # the helper's agent timeout is 60; no wall time given
    agent, *verification = setup.runtime.started_specs
    expected = 60 + DEFAULT_WALL_MARGIN_SECONDS + SANDBOX_GRACE_SECONDS
    assert agent.lifetime_seconds == expected
    assert verification
    assert all(spec.lifetime_seconds is not None for spec in verification)


@pytest.mark.parametrize("wall", [None, 60.0, 3600.0])
@pytest.mark.parametrize("retries", [0, 2])
def test_no_agent_sandbox_ever_starts_without_a_finite_lifetime(
    tmp_path: Path, wall: float | None, retries: int
) -> None:
    """Every sandbox a coordinator starts, over first attempts and retries,
    carries a finite runtime-side lifetime."""
    setup = make_setup(tmp_path, tasks=(task("1"),))
    setup.agent.plan = [(AgentOutcome.STALLED, {})] * retries + [(AgentOutcome.COMPLETED, FIXED)]
    for _ in range(retries + 1):
        setup.coordinator(execution_wall_seconds=wall).run_once()
    agents = [
        s for s in setup.runtime.started_specs if s.image == setup.components.agent_sandbox.image
    ]
    assert len(agents) == retries + 1  # one agent sandbox per attempt
    execution = setup.store.get("e1")
    assert execution is not None
    assert execution.attempt == retries + 1
    for spec in setup.runtime.started_specs:
        assert spec.lifetime_seconds is not None, spec.name
        assert math.isfinite(spec.lifetime_seconds), spec.name
        assert spec.lifetime_seconds > 0, spec.name


@pytest.mark.parametrize("lifetime", [0.0, -1.0, float("inf"), float("nan")])
def test_effects_refuse_an_unbounded_or_unusable_agent_sandbox_lifetime(
    setup: Setup, lifetime: float
) -> None:
    coordinator = setup.coordinator()
    effects = coordinator._effects
    with pytest.raises(ValueError, match="positive, finite lifetime"):
        Effects(
            components=setup.components,
            journal=effects._journal,
            verifier=effects._verifier,
            clock=setup.clock,
            sandbox_lifetime_seconds=lifetime,
        )


def test_the_lifetime_argument_is_required() -> None:
    parameter = inspect.signature(Effects.__init__).parameters["sandbox_lifetime_seconds"]
    assert parameter.default is inspect.Parameter.empty


@pytest.mark.parametrize("wall", [float("inf"), float("nan")])
def test_an_infinite_wall_time_is_refused(setup: Setup, wall: float) -> None:
    with pytest.raises(ValueError, match="finite"):
        setup.coordinator(execution_wall_seconds=wall)


def test_the_config_and_core_default_margins_agree() -> None:
    assert WALL_MARGIN_SECONDS == DEFAULT_WALL_MARGIN_SECONDS


def test_the_wall_time_must_cover_the_agent_timeout(setup: Setup) -> None:
    with pytest.raises(ValueError, match="at least agent_timeout_seconds"):
        setup.coordinator(execution_wall_seconds=59)  # the helper's agent timeout is 60
    setup.coordinator(execution_wall_seconds=60)


FIXED = {"src/app.py": "print('fixed')\n"}


def test_execution_wall_helper() -> None:
    assert execution_wall(100.0, None) == 100.0 + DEFAULT_WALL_MARGIN_SECONDS
    assert execution_wall(100.0, 100.0) == 100.0
    with pytest.raises(ValueError, match="at least"):
        execution_wall(100.0, 99.0)
    with pytest.raises(ValueError, match="finite"):
        execution_wall(float("inf"), None)


def test_lifetime_helpers() -> None:
    assert agent_sandbox_lifetime(1.0) == 1.0 + SANDBOX_GRACE_SECONDS
    with pytest.raises(ValueError, match="positive"):
        agent_sandbox_lifetime(0)
    commands = [VerificationCommand("x", ("true",), timeout_seconds=12.5, job="j")] * 2
    assert verification_sandbox_lifetime(commands) == 25.0 + SANDBOX_GRACE_SECONDS
