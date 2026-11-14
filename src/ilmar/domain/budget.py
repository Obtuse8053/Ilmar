# SPDX-License-Identifier: Apache-2.0
"""Accounting reservations and suspension (ADR-0029 §2 and §3, #18).

Amounts are integer micro-dollars (USD). A reservation is accounting, not a
cost limit: it decides whether new work may *start*. It is charged in the
same commit that starts an attempt, before any container exists, and in Mode 1
it is never reduced, released or refunded. The provider's workspace spend
limit is the hard cost limit.

Days and months are UTC, taken from the time the attempt starts (ADR-0028).
"""

from __future__ import annotations

import enum
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation

MICROS_PER_USD = 1_000_000
MAX_MICROS = 10**15  # a billion dollars: anything larger is a mistake
_MAX_DETAIL = 200


def usd_to_micros(value: object) -> int:
    """Decimal dollars (a number or a string) as whole micro-dollars.

    Refuses booleans, negatives, non-finite values, more than six decimal
    places and anything above ``MAX_MICROS``."""
    if isinstance(value, bool) or not isinstance(value, int | float | str | Decimal):
        raise ValueError("an amount in USD is a number")
    try:
        amount = Decimal(str(value))
    except InvalidOperation:
        raise ValueError("an amount in USD is a number") from None
    if not amount.is_finite() or amount < 0:
        raise ValueError("an amount in USD is finite and not negative")
    micros = amount * MICROS_PER_USD
    if micros != micros.to_integral_value():
        raise ValueError("an amount in USD has at most six decimal places")
    if micros > MAX_MICROS:
        raise ValueError("an amount in USD is implausibly large")
    return int(micros)


def micros_to_usd(micros: int) -> str:
    return f"{Decimal(micros) / MICROS_PER_USD:.6f}".rstrip("0").rstrip(".")


@dataclass(frozen=True, slots=True)
class BudgetLimits:
    """The allowances an attempt is admitted against, and its reservation."""

    daily_micros: int
    monthly_micros: int
    reserve_micros: int

    def __post_init__(self) -> None:
        for name in ("daily_micros", "monthly_micros", "reserve_micros"):
            value = getattr(self, name)
            if type(value) is not int or not 0 < value <= MAX_MICROS:
                raise ValueError(f"{name} must be a positive whole number of micro-dollars")
        if not self.reserve_micros <= self.daily_micros <= self.monthly_micros:
            raise ValueError(
                "the reservation must fit the daily and the daily the monthly allowance"
            )


class ChargeKind(enum.Enum):
    RESERVATION = "reservation"
    """Charged when an attempt starts; one per attempt; never reduced."""
    EXTRA = "extra"
    """A reported cost above the reservation: it may raise a charge, never lower one."""


def utc_day(at: datetime) -> str:
    return at.astimezone(UTC).strftime("%Y-%m-%d")


def utc_month(at: datetime) -> str:
    return at.astimezone(UTC).strftime("%Y-%m")


@dataclass(frozen=True, slots=True)
class Charge:
    execution_id: str
    attempt: int
    kind: ChargeKind
    amount_micros: int
    at: datetime

    def __post_init__(self) -> None:
        if not self.execution_id:
            raise ValueError("a charge names its execution")
        if type(self.attempt) is not int or self.attempt < 1:
            raise ValueError("attempt starts at 1")
        if type(self.amount_micros) is not int or not 0 < self.amount_micros <= MAX_MICROS:
            raise ValueError("a charge is a positive whole number of micro-dollars")
        if self.at.tzinfo is None:
            raise ValueError("a charge time is timezone-aware")

    @property
    def day(self) -> str:
        return utc_day(self.at)

    @property
    def month(self) -> str:
        return utc_month(self.at)


@dataclass(frozen=True, slots=True)
class ModelSpend:
    """What a gateway recorded for one attempt's model requests (P5 part 2b).

    ``charged_micros`` is the sum the gateway settled (reported usage in full,
    or a whole reservation for a report it did not trust). ``unsettled_micros``
    is the reservation of requests admitted but never settled. ``overrun_micros``
    is what reported usage cost beyond the requests' reservations. ``uncertain``
    is the first reason the true cost is unknown, if there is one."""

    requests: int
    charged_micros: int
    unsettled_micros: int = 0
    overrun_micros: int = 0
    uncertain: str | None = None

    def __post_init__(self) -> None:
        for name in ("requests", "charged_micros", "unsettled_micros", "overrun_micros"):
            value = getattr(self, name)
            if type(value) is not int or not 0 <= value <= MAX_MICROS:
                raise ValueError(f"{name} must be a whole number from 0")
        if self.unsettled_micros and self.uncertain is None:
            raise ValueError("unsettled requests make the spend uncertain")

    def extra(self, reserved: int) -> int:
        """What the ledger must add to the attempt's reservation: what the gateway
        charged beyond it. Never negative: a charge is never lowered."""
        return max(0, self.charged_micros + self.unsettled_micros - reserved)


@dataclass(frozen=True, slots=True)
class BudgetTotals:
    """What is charged in one UTC day and in its month."""

    day_micros: int = 0
    month_micros: int = 0


def exhausted(limits: BudgetLimits, totals: BudgetTotals, adding: int) -> SuspensionReason | None:
    """Which allowance ``adding`` more would exceed, the day first; ``None`` if
    it fits. Exactly at the limit fits."""
    if totals.day_micros + adding > limits.daily_micros:
        return SuspensionReason.BUDGET_DAILY
    if totals.month_micros + adding > limits.monthly_micros:
        return SuspensionReason.BUDGET_MONTHLY
    return None


class SuspensionReason(enum.Enum):
    BUDGET_DAILY = "budget-daily"
    BUDGET_MONTHLY = "budget-monthly"
    PROVIDER_CAP = "provider-cap"
    KILL_SWITCH = "kill-switch"
    OPERATOR = "operator-suspended"
    CLOCK_ANOMALY = "clock-anomaly"
    RESTART_LOOP = "restart-loop"
    """Loop mode started too often (ADR-0033 §3)."""
    SUSPENSION_CHECK_FAILED = "suspension-check-failed"
    """The kill-switch and suspension check failed while an agent ran (ADR-0029 §4):
    whether work may go on was unknown, so all of it stopped. Lifted only by an
    operator, and only once the check succeeds again."""
    SPEND_UNCERTAIN = "spend-uncertain"
    """What an attempt's model requests cost could not be settled from the
    gateway's record: a report the gateway did not trust, a request still
    unsettled, or a record that could not be read (P5 part 2b). The ledger holds
    the reservation; what the provider billed beyond it is unknown, so an
    operator checks the provider's own figures before resuming."""


@dataclass(frozen=True, slots=True)
class Suspension:
    """The durable record that stops all new work until ``ilmar resume``.

    ``detail`` is a short, operator-facing note; it never holds a secret."""

    reason: SuspensionReason
    since: datetime
    detail: str = ""

    def __post_init__(self) -> None:
        if self.since.tzinfo is None:
            raise ValueError("a suspension time is timezone-aware")
        if len(self.detail) > _MAX_DETAIL or not self.detail.isprintable():
            raise ValueError(f"a suspension detail is printable and at most {_MAX_DETAIL} chars")
