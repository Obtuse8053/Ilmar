# SPDX-License-Identifier: Apache-2.0
"""``ilmar run --loop``: bounded, sequential passes, only while every gate holds
(ADR-0030 §1, §3, ADR-0033, ADR-0035's hard gate, ADR-0040).

:func:`loop_blocker` decides, from facts read fresh before each pass, whether
the loop may run another pass. It answers ``None`` or ``(exit code, reason)``:

* **3, not enabled:** the mode is not ``supervised``; there is no active
  ``supervised`` gate record; the record is for another repository or another
  agent version than the configuration; the live repository's immutable ID is
  not the approved one (a rename keeps it, a fork does not); the ID cannot be
  read.
* **78, suspended:** the kill switch is engaged or cannot be checked; a
  suspension is in force; the clock is behind the latest persisted time.
  Uncertain gateway spend (ADR-0039) always leaves a suspension in force
  until it is reviewed, so it stops the loop through the suspension.

Lowering a mode, revoking a record or suspending takes effect at the next
check (ADR-0030 §4). :func:`run_loop` runs passes one after another, never
concurrently, waits ``poll_interval`` on the monotonic clock between them, and
never restarts itself.
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime

from ilmar.domain.budget import Suspension
from ilmar.domain.gates import GateRecord, active_gate

NOT_ENABLED, SUSPENDED, STOPPED = 3, 78, 0


@dataclass(frozen=True, slots=True)
class LoopFacts:
    """Everything one check reads, gathered just before it."""

    mode: str
    expected_version: str | None
    repository_id: int | None
    """``[autonomy] repository_id``: the approved target."""
    live_repository_id: int | str
    """The ID the forge reports now, or why it could not be read."""
    gates: Sequence[GateRecord]
    kill_switch_engaged: bool
    suspension: Suspension | None
    latest_update: datetime | None
    now: datetime


def loop_blocker(facts: LoopFacts) -> tuple[int, str] | None:
    """Why the loop may not run a pass now, or ``None`` if it may."""
    if facts.mode != "supervised":
        return NOT_ENABLED, f'[autonomy] mode is "{facts.mode}": loop mode needs "supervised"'
    gate = active_gate(facts.gates, "supervised")
    if gate is None:
        return NOT_ENABLED, "no active supervised gate record (ilmar gate record)"
    if facts.repository_id is None or gate.evidence.repository_id != facts.repository_id:
        return NOT_ENABLED, (
            f"the gate record is for repository {gate.evidence.repository_id}, not the "
            f"configured {facts.repository_id}"
        )
    if facts.expected_version is None or gate.evidence.agent_version != facts.expected_version:
        return NOT_ENABLED, (
            f"the gate record is for agent {gate.evidence.agent_version}, not the configured "
            f"expected_version {facts.expected_version}"
        )
    if isinstance(facts.live_repository_id, str):
        return NOT_ENABLED, f"the target's repository ID cannot be read: {facts.live_repository_id}"
    if facts.live_repository_id != facts.repository_id:
        return NOT_ENABLED, (
            f"the target is repository {facts.live_repository_id}, not the approved "
            f"{facts.repository_id}: a fork or another repository is never approved"
        )
    if facts.kill_switch_engaged:
        return SUSPENDED, "the kill switch is engaged or cannot be checked"
    if facts.suspension is not None:
        return SUSPENDED, f"new work is suspended ({facts.suspension.reason.value})"
    if facts.latest_update is not None and facts.now < facts.latest_update:
        return SUSPENDED, "the clock is behind the latest persisted time"
    return None


def run_loop(
    *,
    check: Callable[[], tuple[int, str] | None],
    run_pass: Callable[[], int],
    poll_interval: float,
    stop: threading.Event,
    report: Callable[[str], None],
) -> int:
    """Passes, one at a time, until a check refuses, a pass ends in a state a
    person must look at, or ``stop`` is set (exit 0).

    A pass that exits 2 (blocked: manual intervention) or 3 (configuration or
    ownership lost) ends the loop with that code; 78 (suspended) too. Exit 0
    and 1 (an execution failed, which the journal records) go on.
    """
    while not stop.is_set():
        refused = check()
        if refused is not None:
            code, reason = refused
            report(f"loop stopped: {reason}")
            return code
        code = run_pass()
        if code not in (0, 1):
            report(f"loop stopped: a pass exited {code}")
            return code
        if stop.wait(poll_interval):
            break
    report("loop stopped by a signal")
    return STOPPED
