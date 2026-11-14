# SPDX-License-Identifier: Apache-2.0
"""``ilmar run --loop``: bounded, sequential passes, only while every gate holds
(ADR-0030 §1, §3, ADR-0033, ADR-0035's hard gate, ADR-0040).

:func:`loop_blocker` decides, from facts read fresh before each pass, whether
the loop may run another pass. It answers ``None`` or ``(exit code, reason)``:

* **3, not enabled:** the configuration file changed, vanished or cannot be
  read since the loop started (it is never reloaded: restart the loop to
  apply a change); the mode is not ``supervised``; there is no active
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
from ilmar.domain.gates import GateRecord, TrialRecord, active_gate

NOT_ENABLED, SUSPENDED, STOPPED = 3, 78, 0


@dataclass(frozen=True, slots=True)
class LoopFacts:
    """Everything one check reads, gathered just before it."""

    configuration: str
    """The configuration file's digest now, or why it cannot be read."""
    started_with: str
    """Its digest when the loop started."""
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
    trial: TrialUse | None = None
    """``--loop --trial`` (ADR-0041): the trial in force and its use so far;
    ``None`` for the permanent loop, which never accepts a trial."""


@dataclass(frozen=True, slots=True)
class TrialUse:
    """What ``--loop --trial`` reads about its authorization before each pass."""

    record: TrialRecord | None
    """The trial in force now (unrevoked, started, not expired), if any."""
    latest: TrialRecord | None
    """The newest trial at all, to say why none is in force."""
    executions: int
    """Executions created since the trial started."""
    spent_micros: int
    """Reservations and charges made since the trial started."""
    reserve_micros: int
    """What one more attempt reserves: it must still fit the trial's cap."""


def _authorization(facts: LoopFacts) -> tuple[int, str] | tuple[int, str, str]:
    """``(repository_id, agent_version)`` of what authorizes this loop, or a refusal
    ``(code, reason, "")``."""
    use = facts.trial
    if use is None:
        gate = active_gate(facts.gates, "supervised")
        if gate is None:
            return NOT_ENABLED, "no active supervised gate record (ilmar gate record)", ""
        return gate.evidence.repository_id, gate.evidence.agent_version
    trial = use.record
    if trial is None:
        latest = use.latest
        why = (
            "none was authorized" if latest is None
            else f"trial {latest.seq} was revoked" if latest.revoked_at is not None
            else f"trial {latest.seq} expired at {latest.terms.expires_at.isoformat()}"
            if facts.now >= latest.terms.expires_at
            else f"trial {latest.seq} starts at {latest.terms.starts_at.isoformat()}"
        )  # fmt: skip
        return NOT_ENABLED, f"no trial authorization in force: {why} (ilmar gate trial)", ""
    if use.executions >= trial.terms.max_executions:
        return (
            NOT_ENABLED,
            (f"the trial's {trial.terms.max_executions} execution(s) are used up"),
            "",
        )
    if use.spent_micros + use.reserve_micros > trial.terms.max_spend_micros:
        return (
            NOT_ENABLED,
            (
                f"another attempt would exceed the trial's spending cap ({use.spent_micros} of "
                f"{trial.terms.max_spend_micros} micro-dollars reserved or charged)"
            ),
            "",
        )
    return trial.terms.repository_id, trial.terms.agent_version


def loop_blocker(facts: LoopFacts) -> tuple[int, str] | None:
    """Why the loop may not run a pass now, or ``None`` if it may."""
    if facts.configuration != facts.started_with:
        return NOT_ENABLED, (
            "the configuration changed, vanished or cannot be read since the loop started "
            f"({facts.configuration[:120]}); restart the loop to apply a change"
        )
    if facts.mode != "supervised":
        return NOT_ENABLED, f'[autonomy] mode is "{facts.mode}": loop mode needs "supervised"'
    found = _authorization(facts)
    if len(found) == 3:
        return found[0], str(found[1])
    approved_id, approved_version = found[0], str(found[1])
    what = "gate record" if facts.trial is None else "trial authorization"
    if facts.repository_id is None or approved_id != facts.repository_id:
        return NOT_ENABLED, (
            f"the {what} is for repository {approved_id}, not the configured {facts.repository_id}"
        )
    if facts.expected_version is None or approved_version != facts.expected_version:
        return NOT_ENABLED, (
            f"the {what} is for agent {approved_version}, not the configured "
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
