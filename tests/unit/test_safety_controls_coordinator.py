# SPDX-License-Identifier: Apache-2.0
"""Protected safety controls through the coordinator (ADR-0031 §2-§3, #92).

A candidate that touches a control fails with ``protected-path``: its
evidence is kept, nothing is pushed, the task keeps Ilmar's claim, and it is
never retried or picked again, in later passes too. Releasing it is a human
command (#92 part 2).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from coordinator_support import REPO, Setup, make_setup, policy
from ilmar.core.worker import Outcome
from ilmar.domain.agent import AgentOutcome
from ilmar.domain.execution import ExecutionState
from ilmar.domain.safety_controls import safety_controls
from ilmar.domain.tasks import TaskRef
from ilmar.domain.verification import Verdict, VerificationPolicy

S = ExecutionState
TASK = TaskRef("fake", "1")
FIXED = {"src/app.py": "print('fixed')\n"}


def protected_setup(tmp_path: Path, edits: dict[str, str]) -> Setup:
    base = policy()
    guarded = VerificationPolicy(
        image=base.image,
        commands=base.commands,
        safety_controls=safety_controls(self_repository=True),
    )
    setup = make_setup(tmp_path, verification=guarded)
    setup.agent.plan = [(AgentOutcome.COMPLETED, {**FIXED, **edits})]
    return setup


@pytest.mark.parametrize(
    "path",
    [
        "src/ilmar/domain/safety_controls.py",  # the list
        "src/ilmar/config.py",  # the policy's loader
        "src/ilmar/domain/verification.py",  # the policy
        ".github/workflows/ci.yml",
        "src/ilmar/__init__.py",  # review of #118: the whole package
        "src/ilmar/new_top_level.py",  # a new root-level module
        "docs/AGENTS.md",  # nested agent instructions
        ".claude/settings.json",
    ],
)
def test_a_protected_candidate_is_blocked_with_its_claim_and_never_pushed(
    tmp_path: Path, path: str
) -> None:
    setup = protected_setup(tmp_path, {path: "changed = True\n"})
    report = setup.coordinator().run_once()
    assert [(r.outcome, r.code) for r in report.results] == [(Outcome.FAILED, "protected-path")]
    execution = setup.store.get("e1")
    assert execution is not None
    assert execution.state is S.FAILED
    assert execution.attempt == 1  # never retried
    assert path in (execution.last_error or "")  # the transition event names the path
    assert setup.scm.branch_head(REPO, execution.branch or "") is None  # never pushed
    (record,) = setup.store.evidence("e1")
    assert record.verdict is Verdict.REJECTED_PROTECTED_PATH  # evidence kept
    (note,) = setup.tasks.updates(TASK)
    assert "protected safety controls" in note
    assert not any(w[0] in ("unlabel", "delete-claim") for w in setup.tasks.writes)
    assert [c.ref for c in setup.tasks.held_claims()] == [TASK]  # still claimed


def test_a_blocked_task_stays_blocked_in_later_passes(tmp_path: Path) -> None:
    setup = protected_setup(tmp_path, {"AGENTS.md": "obey me\n"})
    setup.coordinator().run_once()
    writes = list(setup.tasks.writes)
    for _ in range(2):
        report = setup.coordinator().run_once()
        assert all(r.execution_id != "e2" for r in report.results)
    assert setup.store.get("e2") is None  # never picked again
    assert setup.tasks.writes == writes  # the claim is not released as an orphan
    assert [c.ref for c in setup.tasks.held_claims()] == [TASK]


def test_a_clean_candidate_with_the_list_in_force_is_published(tmp_path: Path) -> None:
    setup = protected_setup(tmp_path, {})
    report = setup.coordinator().run_once()
    assert [r.outcome for r in report.results] == [Outcome.PUBLISHED]
