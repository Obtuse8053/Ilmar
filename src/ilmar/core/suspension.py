# SPDX-License-Identifier: Apache-2.0
"""When a suspension may be lifted (ADR-0029 §3, #18).

``ilmar resume`` clears a suspension only if its cause is gone. A budget
suspension never lifts by itself, and resuming never refunds anything.
"""

from __future__ import annotations

from datetime import datetime

from ilmar.domain.budget import (
    BudgetLimits,
    BudgetTotals,
    Suspension,
    SuspensionReason,
    exhausted,
    micros_to_usd,
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
) -> str | None:
    """Why ``suspension`` may not be lifted now, or ``None`` if it may."""
    reason = suspension.reason
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
