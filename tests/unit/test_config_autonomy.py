# SPDX-License-Identifier: Apache-2.0
"""``[autonomy]`` (ADR-0030, #91): the operating mode a deployment asks for.

Asking for ``supervised`` is one opt-in of three; the loop itself is not in
this change. Configuration validation refuses the later modes, and Mode 1
configurations that break its own rules: more than one execution at a time,
no kill switch, no verification, an unpinned agent version, or Ilmar's own
repository as the target.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from ilmar.config import PORT_NAMES, Autonomy, parse_config
from ilmar.domain.errors import ConfigError

BASE = Path("/srv/ilmar")


def supervised(**autonomy: Any) -> dict[str, Any]:
    adapters: dict[str, Any] = {port: {"kind": "fake"} for port in PORT_NAMES}
    adapters["agent_runner"]["options"] = {"expected_version": "2.1.318"}
    adapters["task_provider"]["options"] = {"repository": "someone/ilmar-disposable"}
    return {
        "adapters": adapters,
        "autonomy": {"mode": "supervised", **autonomy},
        "budget": {"daily_usd": 2.0, "monthly_usd": 10.0, "reserve_per_execution_usd": 1.0,
                   "provider_cap_attested_usd": 10.0, "kill_switch_file": "/etc/ilmar/KILL"},
        "verification": {"image": "localhost/verify:1", "commands": [
            {"name": "tests", "job": "unit", "argv": ["pytest"], "timeout_seconds": 600}]},
    }  # fmt: skip


def test_without_autonomy_the_mode_is_manual() -> None:
    data = supervised()
    del data["autonomy"]
    assert parse_config(data, base_dir=BASE).autonomy == Autonomy("manual", 60.0)


def test_a_complete_supervised_configuration_is_accepted() -> None:
    config = parse_config(supervised(poll_interval_seconds=30), base_dir=BASE)
    assert config.autonomy == Autonomy("supervised", 30.0)


@pytest.mark.parametrize("mode", ["autonomous", "self-hosting"])
def test_the_later_modes_are_refused(mode: str) -> None:
    with pytest.raises(ConfigError, match="needs a later gate"):
        parse_config(supervised(mode=mode), base_dir=BASE)


@pytest.mark.parametrize(
    ("autonomy", "message"),
    [
        ({"mode": "loop"}, "mode must be one of"),
        ({"mode": True}, "mode must be one of"),
        ({"poll_interval_seconds": 0}, "between 1 and 86400"),
        ({"poll_interval_seconds": True}, "between 1 and 86400"),
        ({"poll_interval_seconds": float("inf")}, "between 1 and 86400"),
        ({"restart": True}, "unknown"),
    ],
)
def test_bad_autonomy_settings_are_refused(autonomy: dict[str, Any], message: str) -> None:
    with pytest.raises(ConfigError, match=message):
        parse_config(supervised(**autonomy), base_dir=BASE)


def _without_kill_switch(data: dict[str, Any]) -> None:
    del data["budget"]["kill_switch_file"]


def _unpinned(data: dict[str, Any]) -> None:
    data["adapters"]["agent_runner"]["options"] = {"expected_version": "2.1"}


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda d: d.update(limits={"max_concurrent_executions": 2}),
         "max_concurrent_executions = 1"),
        (lambda d: d.pop("budget"), "kill_switch_file"),
        (_without_kill_switch, "kill_switch_file"),
        (lambda d: d.pop("verification"), "needs \\[verification\\]"),
        (lambda d: d["adapters"]["agent_runner"].pop("options"), "exact x.y.z"),
        (_unpinned, "exact x.y.z"),
        (lambda d: d["verification"].update(self_repository=True), "own repository"),
        (lambda d: d["adapters"]["source_control"].update(
            options={"repository": "Obtuse8053/Ilmar"}), "own repository"),
    ],
)  # fmt: skip
def test_supervised_refuses_what_mode_1_does_not_allow(mutate: Any, message: str) -> None:
    data = supervised()
    mutate(data)
    with pytest.raises(ConfigError, match=message):
        parse_config(data, base_dir=BASE)


def test_manual_mode_keeps_today_s_rules() -> None:
    """Nothing above is checked in manual mode: ``--once`` is unchanged."""
    data = supervised(mode="manual")
    data["limits"] = {"max_concurrent_executions": 2}
    del data["budget"]
    data["adapters"]["source_control"]["options"] = {"repository": "Obtuse8053/Ilmar"}
    assert parse_config(data, base_dir=BASE).autonomy.mode == "manual"
