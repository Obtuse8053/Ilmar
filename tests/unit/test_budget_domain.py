# SPDX-License-Identifier: Apache-2.0
"""Accounting values (ADR-0029 §2): micro-dollars, allowances and UTC periods."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from ilmar.domain.budget import (
    BudgetLimits,
    BudgetTotals,
    Charge,
    ChargeKind,
    Suspension,
    SuspensionReason,
    exhausted,
    micros_to_usd,
    usd_to_micros,
)

LIMITS = BudgetLimits(daily_micros=2_000_000, monthly_micros=3_000_000, reserve_micros=1_000_000)


@pytest.mark.parametrize(
    ("value", "micros"),
    [(1, 1_000_000), (0.5, 500_000), ("2.25", 2_250_000), (Decimal("0.000001"), 1), (0, 0)],
)
def test_usd_to_micros(value: object, micros: int) -> None:
    assert usd_to_micros(value) == micros


@pytest.mark.parametrize(
    "value", [True, None, "abc", -1, float("inf"), float("nan"), "0.0000001", 10**10, [1]]
)
def test_usd_to_micros_refuses(value: object) -> None:
    with pytest.raises(ValueError, match="USD"):
        usd_to_micros(value)


def test_micros_to_usd() -> None:
    assert micros_to_usd(0) == "0"
    assert micros_to_usd(2_250_000) == "2.25"
    assert micros_to_usd(1) == "0.000001"


@pytest.mark.parametrize(
    "values", [(0, 1, 1), (2, 1, 1), (1, 2, 3), (1, 1, 0), (True, 2, 1), (1.0, 2, 1)]
)
def test_limits_need_reserve_within_daily_within_monthly(values: tuple[object, ...]) -> None:
    daily, monthly, reserve = values
    with pytest.raises(ValueError):  # noqa: PT011 - each field has its own message
        BudgetLimits(daily_micros=daily, monthly_micros=monthly, reserve_micros=reserve)  # type: ignore[arg-type]


def test_exactly_at_the_limit_fits_and_the_day_is_checked_first() -> None:
    assert exhausted(LIMITS, BudgetTotals(1_000_000, 1_000_000), 1_000_000) is None
    assert exhausted(LIMITS, BudgetTotals(1_000_001, 1_000_001), 1_000_000) is (
        SuspensionReason.BUDGET_DAILY
    )
    assert exhausted(LIMITS, BudgetTotals(0, 2_500_000), 1_000_000) is (
        SuspensionReason.BUDGET_MONTHLY
    )
    assert exhausted(LIMITS, BudgetTotals(2_000_000, 3_000_000), 1) is SuspensionReason.BUDGET_DAILY


def test_a_charge_belongs_to_the_utc_day_and_month() -> None:
    local = datetime(2026, 1, 31, 23, 30, tzinfo=timezone(timedelta(hours=-2)))
    charge = Charge("e1", 1, ChargeKind.RESERVATION, 1, local)
    assert (charge.day, charge.month) == ("2026-02-01", "2026-02")


@pytest.mark.parametrize(
    "fields",
    [
        {"execution_id": ""},
        {"attempt": 0},
        {"amount_micros": 0},
        {"amount_micros": 1.5},
        {"at": datetime(2026, 1, 1)},
    ],
)
def test_a_charge_refuses_invalid_fields(fields: dict[str, object]) -> None:
    values: dict[str, object] = {"execution_id": "e1", "attempt": 1,
                                 "kind": ChargeKind.RESERVATION, "amount_micros": 1,
                                 "at": datetime(2026, 1, 1, tzinfo=UTC)} | fields  # fmt: skip
    with pytest.raises(ValueError):  # noqa: PT011 - each field has its own message
        Charge(**values)  # type: ignore[arg-type]


@pytest.mark.parametrize("detail", ["x" * 201, "line\nbreak"])
def test_a_suspension_detail_is_short_printable_text(detail: str) -> None:
    with pytest.raises(ValueError, match="detail"):
        Suspension(SuspensionReason.OPERATOR, datetime(2026, 1, 1, tzinfo=UTC), detail)


def test_the_suspension_reasons_are_registered_reason_codes() -> None:
    from ilmar.domain.events import REASON_CODES

    assert {r.value for r in SuspensionReason} <= set(REASON_CODES)
