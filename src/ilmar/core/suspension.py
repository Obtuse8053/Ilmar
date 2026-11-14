# SPDX-License-Identifier: Apache-2.0
"""When a suspension may be lifted (ADR-0029 §3, #18).

``ilmar resume`` clears a suspension only if its cause is gone. A budget
suspension never lifts by itself, and resuming never refunds anything.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime

from ilmar.domain.budget import (
    BudgetLimits,
    BudgetTotals,
    Suspension,
    SuspensionReason,
    exhausted,
    micros_to_usd,
)
from ilmar.domain.events import Actor, Event, EventType

SPEND_UNCERTAIN = SuspensionReason.SPEND_UNCERTAIN.value


def unreviewed_spend(events: Sequence[Event]) -> tuple[Event, ...]:
    """Every uncertain gateway settlement (ADR-0039) no operator has reviewed yet.

    Kept apart from the suspension in force: when another suspension was in
    force as the spend became uncertain, the settlement event is the only
    record, and it still blocks ``ilmar resume`` until it is reviewed."""
    reviewed = {e.data["spend_seq"] for e in events if e.type is EventType.EXECUTION_SPEND_REVIEWED}
    return tuple(
        e for e in events
        if e.type is EventType.EXECUTION_MODEL_SPEND and e.reason == SPEND_UNCERTAIN
        and e.seq not in reviewed
    )  # fmt: skip


def spend_reviews(
    pending: Sequence[Event], *, at: datetime, instance: str, epoch: int, actor: Actor
) -> tuple[Event, ...]:
    """The operator's ``execution.spend_reviewed`` events, one per settlement."""
    return tuple(
        Event.create(
            EventType.EXECUTION_SPEND_REVIEWED,
            at=at,
            instance=instance,
            epoch=epoch,
            actor=actor,
            execution=e.execution,
            attempt=e.attempt,
            reason="spend-reviewed",
            data={"spend_seq": e.seq},
        )
        for e in pending
    )


def resume_blocker(
    suspension: Suspension,
    *,
    now: datetime,
    totals: BudgetTotals,
    limits: BudgetLimits | None,
    latest_update: datetime | None,
    provider_cap_raised: bool,
    kill_switch_present: bool,
    restart_breach: str | None = None,
    check_fault_resolved: bool = False,
    unreviewed: int = 0,
    spend_reviewed: bool = False,
) -> str | None:
    """Why ``suspension`` may not be lifted now, or ``None`` if it may.

    ``unreviewed`` uncertain gateway settlements block every resume, whatever
    the suspension's own reason, until ``spend_reviewed`` (ADR-0039)."""
    reason = suspension.reason
    if (unreviewed or reason is SuspensionReason.SPEND_UNCERTAIN) and not spend_reviewed:
        return (
            f"what {unreviewed or 'an'} attempt(s) spent through the model gateway is not "
            "known in full; check the model provider's own usage and billing for them, "
            "then confirm with --provider-spend-reviewed"
        )
    if reason is SuspensionReason.SPEND_UNCERTAIN:
        return None
    if reason in (SuspensionReason.BUDGET_DAILY, SuspensionReason.BUDGET_MONTHLY):
        if limits is None:
            return "no [budget] is configured, so the allowance cannot be checked"
        over = exhausted(limits, totals, limits.reserve_micros)
        if over is not None:
            period, used, allowed = (
                ("daily", totals.day_micros, limits.daily_micros)
                if over is SuspensionReason.BUDGET_DAILY
                else ("monthly", totals.month_micros, limits.monthly_micros)
            )
            return (
                f"the {period} allowance cannot take another reservation yet "
                f"({micros_to_usd(used)} of {micros_to_usd(allowed)} USD charged)"
            )
        return None
    if reason is SuspensionReason.PROVIDER_CAP:
        if not provider_cap_raised:
            return (
                "the model provider refused for its spend limit, billing or authentication; "
                "confirm it is resolved with --provider-cap-resolved"
            )
        return None
    if reason is SuspensionReason.KILL_SWITCH:
        return "the kill-switch file still exists" if kill_switch_present else None
    if reason is SuspensionReason.SUSPENSION_CHECK_FAILED:
        # Operator intervention, then the check itself: the store answered (the
        # caller read this suspension from it) and the kill switch reads clear.
        if not check_fault_resolved:
            return (
                "the suspension check failed while an agent ran; find and fix the cause "
                "(see the event log), then confirm with --check-fault-resolved"
            )
        if kill_switch_present:
            return "the kill switch is engaged or cannot be checked"
        return None
    if reason is SuspensionReason.RESTART_LOOP:
        # ADR-0033 §3: lifted only when the start log shows no breach in its windows.
        return None if restart_breach is None else f"the start log still shows {restart_breach}"
    if reason is SuspensionReason.CLOCK_ANOMALY:
        if latest_update is not None and now < latest_update:
            return "the clock is still behind the latest persisted time"
        return None
    return None  # OPERATOR: the operator lifts their own suspension
