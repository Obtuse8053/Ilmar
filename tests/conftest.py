# SPDX-License-Identifier: Apache-2.0
"""Checks every test gets without asking (ADR-0024).

After every coordinator pass in any test, the store's events must satisfy
invariant I11 and carry no ``unclassified``, ``invalid``, ``redacted`` or
``truncated`` event, no failure without its reason code, no reconciliation
without its observation and no coordinator event without its pass. A pass
that a test kills (``Crash``) is checked where the process exits instead
(``recovery_support``).

A test that deliberately breaks these, for example by editing the database
behind Ilmar's back, marks itself ``@pytest.mark.events_unchecked``.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest

from ilmar.core.coordinator import Coordinator
from ilmar.domain.event_history import verify
from recovery_support import line_problems

from contracts.execution_store import stored_state


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers", "events_unchecked: do not check the event log after each coordinator pass"
    )


def event_log_problems(store: Any) -> list[str]:
    events = store.events()
    if not events:
        return []
    report = verify(events, stored_state(store))
    problems = [f"I11 {f.check} {f.subject}: {f.detail}" for f in report.failures]
    for event in events:
        line = event.to_json()
        problems += [f"{problem}: {line[:160]}" for problem in line_problems(line)]
    return problems


@pytest.fixture(autouse=True)
def _event_log_after_every_pass(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
) -> Iterator[None]:
    if request.node.get_closest_marker("events_unchecked") is not None:
        yield
        return
    original = Coordinator.run_once

    def checked(self: Coordinator) -> Any:
        report = original(self)
        problems = event_log_problems(self._c.execution_store)
        assert problems == [], problems
        return report

    monkeypatch.setattr(Coordinator, "run_once", checked)
    yield
