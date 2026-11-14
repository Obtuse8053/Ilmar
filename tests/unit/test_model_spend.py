# SPDX-License-Identifier: Apache-2.0
"""Per-attempt gateway budgets and durable spend settlement (P5 part 2b).

* A GATEWAY sandbox gets its attempt's reservation as the gateway's budget,
  and cannot start without a budget.
* What the gateway recorded is settled into the ledger once, before the
  sandbox (and the record with it) is removed: an ``execution.model_spend``
  event, and an ``extra`` charge for any cost beyond the reservation.
* When the cost is not known in full (an untrusted report, a request never
  settled, a record that cannot be read or is already gone), all new work is
  suspended (``spend-uncertain``) until an operator resumes.
* Before reading, the sandbox is quiesced: its agent stopped and confirmed
  stopped, the gateway closed and its final record written. A stop that
  cannot be confirmed, an incomplete record or an unexpected error is
  uncertain.
* After a crash, the restarted worker quiesces and settles from the record
  if it still exists, and fails closed if it does not. A sandbox whose agent
  never ran settles at zero: no request could have reached the gateway.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from coordinator_support import Setup, make_setup
from ilmar.core.components import AgentSandbox
from ilmar.core.worker import Outcome
from ilmar.domain.budget import BudgetLimits, ChargeKind, ModelSpend, SuspensionReason
from ilmar.domain.errors import ContainmentError, ProviderError
from ilmar.domain.events import EventType
from ilmar.domain.ledger import OperationKind as K
from ilmar.domain.workspace import NetworkPolicy
from recovery_support import Crash

LIMITS = BudgetLimits(daily_micros=10_000_000, monthly_micros=50_000_000, reserve_micros=1_000_000)
GATEWAY = AgentSandbox("localhost/agent:1", NetworkPolicy.GATEWAY)


@pytest.fixture
def setup(tmp_path: Path) -> Setup:
    return make_setup(tmp_path, agent_sandbox=GATEWAY)


def name(setup: Setup, attempt: int = 1) -> str:
    return f"ilmar-inst-e1-{attempt}"


def spends(setup: Setup) -> list[dict[str, object]]:
    return [
        {"reason": e.reason, **e.data}
        for e in setup.store.events(execution_id="e1")
        if e.type is EventType.EXECUTION_MODEL_SPEND
    ]


def charges(setup: Setup) -> list[tuple[ChargeKind, int]]:
    return [(c.kind, c.amount_micros) for c in setup.store.charges("e1")]


def test_the_gateway_gets_the_attempt_s_reservation_as_its_budget(setup: Setup) -> None:
    report = setup.coordinator(budget=LIMITS).run_once()
    assert [r.outcome for r in report.results] == [Outcome.PUBLISHED]
    (started,) = [c for c in setup.runtime.started_specs if c.network is NetworkPolicy.GATEWAY]
    assert started.model_budget_micros == LIMITS.reserve_micros


def test_no_gateway_sandbox_starts_without_a_budget(setup: Setup) -> None:
    report = setup.coordinator().run_once()
    assert [r.outcome for r in report.results] == [Outcome.FAILED]
    assert "needs [budget]" in (report.results[0].detail)
    assert setup.runtime.started == []


def test_a_settled_spend_is_recorded_once_before_removal(setup: Setup) -> None:
    setup.runtime.model_spends[name(setup)] = ModelSpend(requests=3, charged_micros=400_000)
    setup.coordinator(budget=LIMITS).run_once()
    assert spends(setup) == [{
        "reason": "model-spend-settled", "requests": 3, "charged_micros": 400_000,
        "unsettled_micros": 0, "overrun_micros": 0, "extra_micros": 0,
    }]  # fmt: skip
    assert charges(setup) == [(ChargeKind.RESERVATION, 1_000_000)]  # never lowered
    assert setup.store.suspension() is None
    # Quiesced first (the agent stopped, the gateway closed), then read, while
    # the sandbox still existed; the close is journaled.
    assert setup.runtime.gateway_closes == [name(setup)]
    assert setup.runtime.spend_reads == [name(setup)]
    kinds = [o.kind for o in setup.store.operations("e1")]
    assert kinds.index(K.CLOSE_GATEWAY) < kinds.index(K.STOP_CONTAINER)


def test_cost_beyond_the_reservation_is_an_extra_charge(setup: Setup) -> None:
    setup.runtime.model_spends[name(setup)] = ModelSpend(
        requests=2, charged_micros=1_300_000, overrun_micros=300_000
    )
    setup.coordinator(budget=LIMITS).run_once()
    assert charges(setup) == [(ChargeKind.RESERVATION, 1_000_000), (ChargeKind.EXTRA, 300_000)]
    (spend,) = spends(setup)
    assert spend["extra_micros"] == 300_000
    assert setup.store.suspension() is None  # a known amount, in the ledger


@pytest.mark.parametrize(
    "found",
    [
        ModelSpend(requests=1, charged_micros=1_000_000,
                   uncertain="a report the gateway did not trust (incomplete)"),
        ModelSpend(requests=2, charged_micros=100_000, unsettled_micros=500_000,
                   uncertain="1 request(s) admitted but never settled"),
        ProviderError("podman logs failed"),
        ProviderError("the gateway wrote no final record: it may be incomplete"),
        ProviderError("the gateway's record is not trusted: lines are missing"),
    ],
)  # fmt: skip
def test_an_uncertain_spend_suspends_all_new_work(
    setup: Setup, found: ModelSpend | Exception
) -> None:
    setup.runtime.model_spends[name(setup)] = found
    report = setup.coordinator(budget=LIMITS).run_once()
    held = setup.store.suspension()
    assert held is not None
    assert held.reason is SuspensionReason.SPEND_UNCERTAIN
    assert "e1 attempt 1" in held.detail
    (spend,) = spends(setup)
    assert spend["reason"] == "spend-uncertain"
    assert charges(setup)[0] == (ChargeKind.RESERVATION, 1_000_000)
    assert len(report.results) == 1


def test_a_stop_that_cannot_be_confirmed_is_uncertain(setup: Setup) -> None:
    setup.runtime.close_failures[name(setup)] = ContainmentError(
        "container could not be confirmed stopped", container_id="x", name=name(setup)
    )
    setup.coordinator(budget=LIMITS).run_once()
    held = setup.store.suspension()
    assert held is not None
    assert held.reason is SuspensionReason.SPEND_UNCERTAIN
    assert "ContainmentError" in held.detail
    assert setup.runtime.spend_reads == []  # never read before the agent is stopped


def test_an_unexpected_accounting_error_is_recorded_uncertain_then_raised(setup: Setup) -> None:
    setup.runtime.model_spends[name(setup)] = RuntimeError("a bug")
    report = setup.coordinator(budget=LIMITS).run_once()
    # Raised, not swallowed: the worker's process boundary blocks the execution.
    assert report.results[0].code == "unexpected-error"
    (spend,) = spends(setup)
    assert spend["reason"] == "spend-uncertain"
    held = setup.store.suspension()
    assert held is not None
    assert held.reason is SuspensionReason.SPEND_UNCERTAIN


def test_spending_after_a_coordinator_crash_is_quiesced_then_settled(setup: Setup) -> None:
    """The coordinator dies after the agent ran and before settlement. The old
    agent keeps running (and spending) meanwhile. The restarted coordinator
    stops it and closes the gateway before it reads, and records the later,
    larger figure."""
    setup.runtime.model_spends[name(setup)] = ModelSpend(requests=1, charged_micros=200_000)
    real = setup.runtime.close_model_gateway

    def crash(name: str, *, owner: object) -> bool:
        raise Crash

    setup.runtime.close_model_gateway = crash  # type: ignore[method-assign]
    with pytest.raises(Crash):
        setup.coordinator(budget=LIMITS).run_once()
    assert spends(setup) == []
    agent = setup.runtime.find(name(setup))
    assert agent is not None
    assert agent.running  # still there, still running
    setup.runtime.model_spends[name(setup)] = ModelSpend(requests=4, charged_micros=700_000)
    setup.runtime.close_model_gateway = real  # type: ignore[method-assign]
    setup.coordinator(budget=LIMITS).run_once()
    (spend,) = spends(setup)
    assert (spend["reason"], spend["requests"], spend["charged_micros"]) == (
        "model-spend-settled", 4, 700_000,
    )  # fmt: skip
    assert setup.runtime.gateway_closes == [name(setup)]
    assert setup.runtime.spend_reads == [name(setup)]  # once, after the agent stopped
    assert setup.store.suspension() is None


def test_a_record_gone_before_settlement_fails_closed(setup: Setup) -> None:
    real = setup.runtime.close_model_gateway

    def crash(name: str, *, owner: object) -> bool:
        raise Crash

    setup.runtime.close_model_gateway = crash  # type: ignore[method-assign]
    with pytest.raises(Crash):
        setup.coordinator(budget=LIMITS).run_once()
    setup.runtime.close_model_gateway = real  # type: ignore[method-assign]
    for container in list(setup.runtime._containers.values()):  # the sandbox is gone
        setup.runtime._containers.pop(container.handle.id)
    setup.coordinator(budget=LIMITS).run_once()
    (spend,) = spends(setup)
    assert spend["reason"] == "spend-uncertain"
    held = setup.store.suspension()
    assert held is not None
    assert held.reason is SuspensionReason.SPEND_UNCERTAIN


def test_a_sandbox_whose_agent_never_ran_settles_at_zero(setup: Setup) -> None:
    """A crash after the sandbox started and before the agent run was recorded:
    no route token was ever handed out, so nothing can have been spent."""
    real = setup.runtime.start

    def crash_after_start(spec: object) -> object:
        real(spec)
        raise Crash

    setup.runtime.start = crash_after_start  # type: ignore[method-assign,assignment]
    with pytest.raises(Crash):
        setup.coordinator(budget=LIMITS).run_once()
    setup.runtime.start = real  # type: ignore[method-assign]
    setup.coordinator(budget=LIMITS).run_once()
    first = spends(setup)[0]
    assert (first["reason"], first["requests"]) == ("model-spend-settled", 0)
    assert setup.store.suspension() is None


def test_a_non_gateway_attempt_never_reads_a_spend(tmp_path: Path) -> None:
    setup = make_setup(tmp_path)
    setup.coordinator(budget=LIMITS).run_once()
    assert setup.runtime.spend_reads == []
    assert spends(setup) == []
    assert K.RUN_AGENT in [o.kind for o in setup.store.operations("e1")]
