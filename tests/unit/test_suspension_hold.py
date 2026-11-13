# SPDX-License-Identifier: Apache-2.0
"""What may run while new work is suspended (ADR-0029 §3; #113 review).

``recover()`` resumes executions before the pass checks for a suspension, so
the hold is enforced where effects start, in the journal
(``HELD_WHILE_SUSPENDED``). While a suspension is in force:

* **held**, whether new or repeated: claiming a task, starting a container
  (agent or verification), running the agent, running verification, pushing
  and opening or updating a change request. A held execution keeps its state,
  writes nothing and is reported ``suspended``; the pass exits 78.
* **allowed**, because it is cleanup or local bookkeeping: releasing claims,
  notes, stopping containers, freezing a finished agent's output, deleting
  workspaces, cancellation, and the tracker-label reconciliation.

After ``ilmar resume``, every held execution continues from where it stopped.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from coordinator_support import REPO, Setup, make_setup, task
from ilmar.adapters.sqlite import SQLiteExecutionStore
from ilmar.core import cancel_inbox as fs
from ilmar.core.coordinator import ExitCode
from ilmar.core.journal import HELD_WHILE_SUSPENDED
from ilmar.domain.agent import AgentOutcome
from ilmar.domain.budget import Suspension, SuspensionReason
from ilmar.domain.events import Actor, ActorKind, EventContext
from ilmar.domain.execution import ExecutionState
from ilmar.domain.ledger import Commit, OperationKind

S = ExecutionState
FIXED = {"src/app.py": "print('fixed')\n"}


@pytest.fixture
def setup(tmp_path: Path) -> Iterator[Setup]:
    store = SQLiteExecutionStore(tmp_path / "executions.sqlite3")
    made = make_setup(tmp_path, tasks=(task("1"),), store=store)
    made.agent.plan = [(AgentOutcome.COMPLETED, FIXED)]
    yield made
    made.ownership.release()
    store.close()


def operator(setup: Setup) -> EventContext:
    fence = setup.ownership.fence
    return EventContext(at=setup.clock(), instance="inst", epoch=fence.epoch,
                        actor=Actor(ActorKind.HUMAN, "operator"))  # fmt: skip


def suspend(setup: Setup) -> None:
    held = Suspension(SuspensionReason.OPERATOR, setup.clock())
    setup.store.commit(setup.ownership.fence, Commit(suspend=held), context=operator(setup))


def resume(setup: Setup) -> None:
    setup.store.commit(setup.ownership.fence, Commit(resume=setup.clock()), context=operator(setup))


class SuspendDuring:
    """Wraps a port: the first call to ``method`` suspends the instance, then runs."""

    def __init__(self, target: Any, method: str, setup: Setup) -> None:
        self._target, self._method, self._setup, self.fired = target, method, setup, False

    def __getattr__(self, name: str) -> Any:
        attribute = getattr(self._target, name)
        if name != self._method:
            return attribute

        def call(*args: Any, **kwargs: Any) -> Any:
            if not self.fired:
                self.fired = True
                suspend(self._setup)
            return attribute(*args, **kwargs)

        return call


def operations(setup: Setup, kind: OperationKind) -> int:
    return sum(1 for o in setup.store.operations("e1") if o.kind is kind)


def held_pass(setup: Setup) -> tuple[ExitCode, S]:
    report = setup.coordinator().run_once()
    execution = setup.store.get("e1")
    assert execution is not None
    return report.code, execution.state


def test_the_held_and_allowed_kinds_are_exactly_these() -> None:
    assert {k.value for k in HELD_WHILE_SUSPENDED} == {
        "claim-task", "start-container", "run-agent", "verify", "publish-revision",
        "upsert-change-request",
    }  # fmt: skip


@pytest.mark.parametrize(
    ("port", "method", "stopped_in", "held_kind"),
    [
        # Suspended after the workspace exists: the agent's container is not started.
        ("source_control", "checkout", S.RUNNING_AGENT, OperationKind.START_CONTAINER),
        # Suspended as the agent's output is frozen: it is not verified.
        ("source_control", "freeze_candidate", S.VERIFYING, OperationKind.VERIFY),
        # Suspended during verification: nothing is pushed.
        ("source_control", "checkout_revision", S.PUBLISHING, OperationKind.PUBLISH_REVISION),
    ],
)
def test_an_execution_is_held_where_it_is_and_continues_after_resume(
    setup: Setup, port: str, method: str, stopped_in: S, held_kind: OperationKind
) -> None:
    wrapped = SuspendDuring(getattr(setup.components, port), method, setup)
    setattr(setup.components, port, wrapped)
    code, state = held_pass(setup)
    assert wrapped.fired
    assert code is ExitCode.SUSPENDED
    assert state is stopped_in
    assert operations(setup, held_kind) == 0  # not even an intent
    if held_kind is OperationKind.START_CONTAINER:
        assert setup.runtime.started_specs == []
    # A later pass, recovery first, holds it again without writing anything.
    versions = setup.store.get("e1")
    assert held_pass(setup) == (ExitCode.SUSPENDED, stopped_in)
    assert setup.store.get("e1") == versions
    resume(setup)
    assert held_pass(setup) == (ExitCode.SUCCESS, S.SUCCEEDED)
    assert setup.scm.branch_head(REPO, setup.store.get("e1").branch or "") is not None  # type: ignore[union-attr]


def test_nothing_new_is_claimed_while_suspended(setup: Setup) -> None:
    suspend(setup)
    assert setup.coordinator().run_once().code is ExitCode.SUSPENDED
    assert setup.store.get("e1") is None
    assert setup.tasks.held_claims() == ()


def test_cleanup_and_cancellation_still_run_while_suspended(setup: Setup) -> None:
    """A held execution can still be cancelled: its claim is released and its
    resources settled, with no container started."""
    wrapped = SuspendDuring(setup.components.source_control, "checkout", setup)
    setup.components.source_control = wrapped
    assert held_pass(setup) == (ExitCode.SUSPENDED, S.RUNNING_AGENT)
    fd = fs.open_inbox(setup.layout.coordinator_root, create=True)
    try:
        fs.submit(fd, "e1", "alice", now=setup.clock())
    finally:
        os.close(fd)
    code, state = held_pass(setup)
    assert state is S.CANCELLED
    assert setup.runtime.started_specs == []
    assert setup.tasks.held_claims() == ()  # released while suspended
    assert code is ExitCode.SUSPENDED  # the instance is still suspended


def test_a_failed_execution_releases_its_claim_while_suspended(setup: Setup) -> None:
    setup.agent.plan = [(AgentOutcome.PROVIDER_REFUSED, {})]  # fails and suspends at once
    report = setup.coordinator().run_once()
    assert report.code is ExitCode.SUSPENDED
    execution = setup.store.get("e1")
    assert execution is not None
    assert execution.state is S.FAILED
    assert setup.tasks.held_claims() == ()
    assert operations(setup, OperationKind.RELEASE_TASK) == 1
    assert operations(setup, OperationKind.STOP_CONTAINER) >= 1
