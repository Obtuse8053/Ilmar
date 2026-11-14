# SPDX-License-Identifier: Apache-2.0
"""``[supervisor]`` (ADR-0043 §2 and §3): off by default, cannot be turned on
before Mode 2, and every limit may only be made stricter."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from ilmar.cli_loop import start_limits
from ilmar.config import PORT_NAMES, Supervisor, parse_config
from ilmar.core.starts import StartLimits
from ilmar.domain.errors import ConfigError

BASE = Path("/srv/ilmar")


def parsed(**supervisor: Any) -> Any:
    data: dict[str, Any] = {"adapters": {port: {"kind": "fake"} for port in PORT_NAMES}}
    if supervisor:
        data["supervisor"] = supervisor
    return parse_config(data, base_dir=BASE)


def test_off_by_default_with_the_start_logs_defaults() -> None:
    config = parsed()
    assert config.supervisor == Supervisor()
    assert config.supervisor.enabled is False
    assert start_limits(config) == StartLimits()


@pytest.mark.parametrize("mode", ["manual", None])
def test_it_cannot_be_turned_on_in_any_mode_that_exists_today(mode: str | None) -> None:
    data: dict[str, Any] = {"adapters": {port: {"kind": "fake"} for port in PORT_NAMES},
                            "supervisor": {"enabled": True}}  # fmt: skip
    if mode is not None:
        data["autonomy"] = {"mode": mode}
    with pytest.raises(ConfigError, match="need Mode 2"):
        parse_config(data, base_dir=BASE)


@pytest.mark.parametrize(
    "stricter",
    [
        {"min_interval_seconds": 120},
        {"backoff_max_seconds": 7200, "cooldown_seconds": 43200},
        {"cooldown_seconds": 86400},
        {"per_hour": 2},
        {"per_day": 5},
        {"enabled": False, "per_hour": 1, "per_day": 1},
    ],
)
def test_stricter_limits_are_accepted_and_reach_the_start_log(stricter: dict[str, Any]) -> None:
    limits = start_limits(parsed(**stricter))
    for key, value in stricter.items():
        if key != "enabled":
            assert getattr(limits, key) == value


@pytest.mark.parametrize(
    ("looser", "message"),
    [
        ({"min_interval_seconds": 30}, "min_interval_seconds must be between"),
        ({"backoff_max_seconds": 600}, "backoff_max_seconds may not be below"),
        ({"cooldown_seconds": 3600}, "cooldown_seconds must be at least"),
        ({"per_hour": 7}, "per_hour must be between"),
        ({"per_day": 21}, "per_day must be between"),
        ({"per_hour": 0}, "per_hour must be between"),
        ({"min_interval_seconds": 7200}, "backoff_max_seconds must be at least min_interval"),
        ({"backoff_max_seconds": 86400}, "cooldown_seconds must be at least"),
    ],
)
def test_looser_or_inconsistent_limits_are_refused(looser: dict[str, Any], message: str) -> None:
    with pytest.raises(ConfigError, match=message):
        parsed(**looser)


@pytest.mark.parametrize(
    "bad",
    [
        {"enabled": "yes"},
        {"per_hour": 2.5},
        {"per_day": True},
        {"min_interval_seconds": "60"},
        {"restart_sec": 1},
    ],
)
def test_malformed_values_are_refused(bad: dict[str, Any]) -> None:
    with pytest.raises(ConfigError, match=r"\[supervisor\]"):
        parsed(**bad)
