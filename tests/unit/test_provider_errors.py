# SPDX-License-Identifier: Apache-2.0
"""Provider failures fail closed (ADR-0029 §5, #90).

A spend, usage, credit or billing limit, and any authentication failure, end
the execution without a retry (#18 will make them a durable suspension). Only
an ordinary rate limit or overload is transient: it counts as an attempt and
waits for its backoff. The texts below are illustrative: the exact text
Claude Code shows at a workspace spend limit is not yet observed, and the
Gate 1 drill (#93) must record it.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from coordinator_support import make_setup
from ilmar.adapters.claude_code import ClaudeCodeRunner
from ilmar.adapters.claude_code.provider_errors import ProviderFailure, classify
from ilmar.adapters.sqlite import SQLiteExecutionStore
from ilmar.core.state_machine import RetryBackoff
from ilmar.domain.agent import AgentOutcome, AgentRequest
from ilmar.domain.execution import ExecutionState
from ilmar.domain.workspace import CommandResult, ContainerSpec, ResourceOwner

from claude_code_support import INIT, allow_probe, result_json, stream
from contracts.container_runtime import identity_of
from fakes import FakeContainerRuntime
from helpers import task

KEY = "sk-ant-test-key-0123456789"
OWNER = ResourceOwner(instance_id="t", execution_id="e", attempt=1, token="t")
S = ExecutionState
BACKOFF = RetryBackoff(300.0, 1200.0)

CAP = [
    "API Error: 400 You have reached your specified workspace API usage limits.",
    "API Error: 429 usage limit reached for this workspace",
    "Your credit balance is too low to access the Anthropic API.",
    "billing_error: please update your billing details",
    "Spend limit exceeded",
    "rate_limit_error: 429 ... monthly spending limit reached",  # cap wins over rate
]
AUTH = [
    "API Error: 401 authentication_error: invalid x-api-key",
    "API Error: 403 permission_error",
    "Invalid API key · Please run /login",
    "OAuth token has expired",
    "Unauthorized",
]
RATE = [
    "API Error: 429 rate_limit_error: Number of requests has exceeded your rate limit",
    "API Error: 529 overloaded_error",
    "Too Many Requests",
]
OTHER = ["the tool failed", "max turns reached", ""]


@pytest.mark.parametrize("text", CAP)
def test_spend_usage_credit_and_billing_limits_are_caps(text: str) -> None:
    assert classify(text) is ProviderFailure.CAP


@pytest.mark.parametrize("text", AUTH)
def test_authentication_failures_are_auth(text: str) -> None:
    assert classify(text) is ProviderFailure.AUTH


@pytest.mark.parametrize("text", RATE)
def test_ordinary_rate_limits_are_transient(text: str) -> None:
    assert classify(text) is ProviderFailure.RATE_LIMIT


@pytest.mark.parametrize("text", OTHER)
def test_other_failures_are_not_provider_failures(text: str) -> None:
    assert classify(text) is None


def run_claude(tmp_path: Path, result: CommandResult) -> AgentOutcome:
    runtime = FakeContainerRuntime()
    runtime.register("claude", lambda argv, timeout, env: result)
    allow_probe(runtime)
    handle = runtime.start(
        ContainerSpec(image="img", host_workspace=tmp_path, name="agent", owner=OWNER,
                      workspace_identity=identity_of(tmp_path))
    )  # fmt: skip
    request = AgentRequest(task=task("1"), instructions="x", timeout_seconds=60)
    return ClaudeCodeRunner(api_key=KEY).run(runtime, handle, request).outcome


def error_stream(text: str) -> str:
    return stream(result_json(subtype="error", is_error=True, text=text), before=(INIT,))


@pytest.mark.parametrize(
    ("text", "outcome"),
    [
        *((t, AgentOutcome.PROVIDER_REFUSED) for t in CAP + AUTH),
        *((t, AgentOutcome.RATE_LIMITED) for t in RATE),
        ("the tool failed", AgentOutcome.FAILED),
    ],
)
def test_the_runner_classifies_an_error_result(
    tmp_path: Path, text: str, outcome: AgentOutcome
) -> None:
    assert run_claude(tmp_path, CommandResult(1, error_stream(text), "", 1.0)) is outcome


@pytest.mark.parametrize(
    ("stderr", "outcome"),
    [
        ("API Error: 401 invalid x-api-key", AgentOutcome.PROVIDER_REFUSED),
        ("Credit balance is too low", AgentOutcome.PROVIDER_REFUSED),
        ("API Error: 429 rate_limit_error", AgentOutcome.RATE_LIMITED),
        ("something else broke", AgentOutcome.FAILED),
    ],
)
def test_the_runner_classifies_a_run_that_ended_without_a_result(
    tmp_path: Path, stderr: str, outcome: AgentOutcome
) -> None:
    ended = CommandResult(1, INIT + "\n", stderr + "\n", 1.0)  # no result object
    assert run_claude(tmp_path, ended) is outcome


def test_a_malformed_stream_is_never_reclassified(tmp_path: Path) -> None:
    """Only a missing result is classified: a malformed event stays a failure."""
    bad = CommandResult(1, "not json\n", "API Error: 429 rate_limit_error\n", 1.0)
    assert run_claude(tmp_path, bad) is AgentOutcome.FAILED


def coordinator_run(tmp_path: Path, text: str) -> tuple[S, int, str | None, list[str]]:
    setup = make_setup(
        tmp_path, tasks=(task("1"),), store=SQLiteExecutionStore(tmp_path / "e.sqlite3")
    )
    setup.runtime.register("claude", lambda argv, timeout, env: CommandResult(
        1, error_stream(text), "", 1.0))  # fmt: skip
    allow_probe(setup.runtime)
    setup.components.agent_runner = ClaudeCodeRunner(api_key=KEY)
    setup.coordinator(retry_backoff=BACKOFF).run_once()
    execution = setup.store.get("e1")
    assert execution is not None
    reasons = [e.reason for e in setup.store.events() if e.reason]
    return execution.state, execution.attempt, execution.last_error, reasons


@pytest.mark.parametrize("text", [CAP[0], AUTH[0]])
def test_a_provider_refusal_fails_the_execution_without_a_retry(tmp_path: Path, text: str) -> None:
    state, attempt, error, reasons = coordinator_run(tmp_path, text)
    assert (state, attempt) == (S.FAILED, 1)
    assert error is not None
    assert error.startswith("the provider refused the agent")
    assert "provider-refused" in reasons
    assert KEY not in error


def test_an_ordinary_rate_limit_counts_as_an_attempt_and_backs_off(tmp_path: Path) -> None:
    state, attempt, error, reasons = coordinator_run(tmp_path, RATE[0])
    assert (state, attempt) == (S.RETRY_WAIT, 1)
    assert error == "the provider rate-limited the agent"
    assert "provider-rate-limited" in reasons


def test_a_credential_route_refuses_the_execution_without_a_retry(tmp_path: Path) -> None:
    from claude_code_support import ProbeStandIn

    setup = make_setup(tmp_path, tasks=(task("1"),))
    calls: list[int] = []

    def claude(argv: object, timeout: float, env: object) -> CommandResult:
        calls.append(1)
        return CommandResult(0, "", "", 0.0)

    setup.runtime.register("claude", claude)
    allow_probe(setup.runtime, ProbeStandIn(image_env=["ANTHROPIC_AUTH_TOKEN"]))
    setup.components.agent_runner = ClaudeCodeRunner(api_key=KEY)
    setup.coordinator(retry_backoff=BACKOFF).run_once()
    execution = setup.store.get("e1")
    assert execution is not None
    assert (execution.state, execution.attempt) == (S.FAILED, 1)
    assert calls == []  # claude never ran
    assert any(e.reason == "policy-violation" for e in setup.store.events())
