# SPDX-License-Identifier: Apache-2.0
"""The secret scan through the coordinator (#110, ADR-0038).

A candidate or change-request text that holds one of the deployment's own
secrets fails deterministically with reason code ``candidate-secret``:
nothing is pushed, no change request is opened, and no tracker note carries
the value. Only plain encodings are covered (see ``test_secret_scan.py``).
"""

from __future__ import annotations

import base64
from pathlib import Path

from coordinator_support import REPO, Setup, make_setup
from ilmar.core.secret_scan import SecretScanner
from ilmar.core.worker import Outcome
from ilmar.domain.agent import AgentOutcome
from ilmar.domain.execution import ExecutionState
from ilmar.domain.tasks import TaskRef

S = ExecutionState
KEY = "sk-ant-coordinator-scan-0123456789"
SCANNER = SecretScanner({"ANTHROPIC_API_KEY": KEY})
TASK = TaskRef("fake", "1")


def setup_with(
    tmp_path: Path,
    edits: dict[str, str],
    *,
    outcome: AgentOutcome = AgentOutcome.COMPLETED,
    summary: str | None = None,
) -> Setup:
    setup = make_setup(tmp_path, secret_scanner=SCANNER)
    setup.agent.plan = [(outcome, edits)]
    setup.agent.summary = summary
    return setup


def assert_nothing_left(setup: Setup) -> None:
    execution = setup.store.get("e1")
    assert execution is not None
    assert setup.scm.branch_head(REPO, execution.branch or "") is None  # never pushed
    for update in setup.tasks.updates(TASK):
        assert KEY not in update
    every = [repr(e) for e in setup.store.events()] + [repr(t) for t in setup.store.history("e1")]
    assert not any(KEY in text for text in every)


def test_a_candidate_holding_a_secret_fails_and_is_never_published(tmp_path: Path) -> None:
    encoded = base64.b64encode(f"token={KEY}".encode()).decode()
    setup = setup_with(tmp_path, {"src/app.py": "print('fixed')\n", "conf.txt": encoded})
    report = setup.coordinator().run_once()
    assert [r.outcome for r in report.results] == [Outcome.FAILED]
    assert [r.code for r in report.results] == ["candidate-secret"]
    execution = setup.store.get("e1")
    assert execution is not None
    assert execution.state is S.FAILED
    assert execution.attempt == 1  # deterministic: never retried
    assert execution.last_error is not None
    assert "conf.txt" not in execution.last_error  # the tracker note names no path
    (record,) = setup.store.evidence("e1")
    (scan,) = [c for c in record.checks if c.name == "candidate-secrets"]
    assert scan.detail == "found ANTHROPIC_API_KEY (base64) in conf.txt"
    assert_nothing_left(setup)


def test_a_secret_in_the_agent_summary_stops_the_push(tmp_path: Path) -> None:
    setup = setup_with(
        tmp_path, {"src/app.py": "print('fixed')\n"}, summary=f"Done. I used {KEY} to test."
    )
    report = setup.coordinator().run_once()
    assert [r.code for r in report.results] == ["candidate-secret"]
    execution = setup.store.get("e1")
    assert execution is not None
    assert execution.state is S.FAILED
    assert "ANTHROPIC_API_KEY" in (execution.last_error or "")
    assert_nothing_left(setup)


def test_a_tracker_note_holding_a_secret_is_withheld(tmp_path: Path) -> None:
    setup = setup_with(tmp_path, {}, outcome=AgentOutcome.FAILED, summary=f"bad key {KEY}")
    report = setup.coordinator().run_once()
    assert [r.code for r in report.results] == ["agent-failed"]
    (note,) = setup.tasks.updates(TASK)
    assert note.startswith("Ilmar withheld this update")
    assert "ANTHROPIC_API_KEY" in note
    assert KEY not in note


def test_a_clean_candidate_is_published_with_the_scan_in_its_evidence(tmp_path: Path) -> None:
    setup = setup_with(tmp_path, {"src/app.py": "print('fixed')\n"})
    report = setup.coordinator().run_once()
    assert [r.outcome for r in report.results] == [Outcome.PUBLISHED]
    execution = setup.store.get("e1")
    assert execution is not None
    _, description = setup.scm.change_request_text(REPO, execution.branch or "")
    assert "candidate-secrets" in description  # listed among the checks that passed


def test_secret_bearing_filenames_reach_no_record_and_no_tracker(tmp_path: Path) -> None:
    """CI, protected and check-influencing paths whose names hold the secret."""
    encoded = KEY.encode().hex()
    setup = setup_with(
        tmp_path,
        {
            "src/app.py": "print('fixed')\n",
            f".github/workflows/{KEY}.yml": "on: push\n",
            f".claude/{encoded}.json": "{}\n",
            f"tests/test_{KEY}.py": "x = 1\n",
        },
    )
    report = setup.coordinator().run_once()
    assert [r.code for r in report.results] == ["candidate-secret"]
    for detail in (r.detail for r in report.results):
        assert KEY not in detail
        assert encoded not in detail
    (record,) = setup.store.evidence("e1")
    text = record.to_json()
    assert KEY not in text
    assert encoded not in text
    assert record.ci_config_paths
    assert record.protected_paths
    assert record.check_influencing_paths
    assert_nothing_left(setup)
    for update in setup.tasks.updates(TASK):
        assert encoded not in update
