# SPDX-License-Identifier: Apache-2.0
"""Credential-free checks for the manual cap drill; no network or model calls."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from scripts import cap_drill as d


def limit(amount: str = "100", **changes: Any) -> dict[str, Any]:
    row = {
        "type": "spend_limit",
        "amount": amount,
        "currency": "USD",
        "period": "monthly",
        "is_enabled": True,
        "scope": {"type": "workspace", "workspace_id": "wrkspc_1234"},
    }
    row.update(changes)
    return {"data": [row], "next_page": None}


def test_limit_100_cents_matches_operator_authorization() -> None:
    assert d.validate_limit(limit(), "wrkspc_1234") == 100


@pytest.mark.parametrize(
    "bad",
    [
        limit("101"),
        limit("200"),
        limit("0"),
        limit(currency="EUR"),
        limit(period="daily"),
        limit(is_enabled=False),
        limit(scope={"type": "organization"}),
        {"data": [], "next_page": None},
        {"data": limit()["data"] * 2},
        {"data": limit()["data"], "next_page": "cursor"},
    ],
)
def test_limit_verification_fails_closed(bad: dict[str, Any]) -> None:
    with pytest.raises(d.DrillError):
        d.validate_limit(bad, "wrkspc_1234")


def test_other_workspaces_limit_cannot_be_mistaken_for_target() -> None:
    with pytest.raises(d.DrillError):
        d.validate_limit(limit(), "wrkspc_another")


def test_model_binding_requires_workspace_id_header(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_api(
        path: str,
        key: str,
        *,
        admin: bool,
        method: str = "GET",
        payload: Any = None,
        beta: bool = False,
    ) -> tuple[int, dict[str, Any], dict[str, str]]:
        assert path == "/v1/messages/count_tokens"
        assert key == "dummy"
        assert method == "POST"
        assert payload == d.MODEL_BODY
        return 200, {"input_tokens": 7000}, {"anthropic-workspace-id": "wrkspc_1234"}

    monkeypatch.setattr(d, "api", fake_api)
    assert d.verify_model_key("dummy", "wrkspc_1234") == 7000
    with pytest.raises(d.DrillError):
        d.verify_model_key("dummy", "wrkspc_different")


def test_model_binding_refuses_missing_header(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(d, "api", lambda *_a, **_k: (200, {"input_tokens": 100}, {}))
    with pytest.raises(d.DrillError):
        d.verify_model_key("dummy", "wrkspc_1234")


def test_only_genuine_workspace_limit_refusal_counts() -> None:
    assert d.is_cap_refusal(
        400,
        {
            "error": {
                "type": "invalid_request_error",
                "message": "You have reached your specified workspace API usage limits",
            }
        },
    )
    for status, kind, message in [
        (429, "rate_limit_error", "request rate limit reached"),
        (401, "authentication_error", "invalid API key"),
        (400, "invalid_request_error", "unknown model"),
        (529, "overloaded_error", "workspace spend limit"),
    ]:
        assert not d.is_cap_refusal(status, {"error": {"type": kind, "message": message}})


def test_cost_bound_includes_margin_and_prevents_excess() -> None:
    worst = d.max_cost(10000)
    base = (Decimal(10000) * d.INPUT_PRICE + Decimal(d.MAX_OUTPUT) * d.OUTPUT_PRICE) / 1_000_000
    assert worst >= base * Decimal("1.10")
    assert Decimal("0.9999") + worst > Decimal("1.00")
    with pytest.raises(d.DrillError):
        d.max_cost(0)


def test_untrusted_usage_refuses_further_spend() -> None:
    for usage in (
        {"input_tokens": -1, "output_tokens": 1},
        {"input_tokens": 10, "output_tokens": 9},
        {"input_tokens": "10", "output_tokens": 1},
        {"input_tokens": 10},
    ):
        with pytest.raises(d.DrillError):
            d.reported_cost(usage)


def test_context_needs_first_manual_run_at_exact_main_sha() -> None:
    env = {
        "GITHUB_ACTIONS": "true",
        "GITHUB_EVENT_NAME": "workflow_dispatch",
        "GITHUB_REF": "refs/heads/main",
        "GITHUB_REPOSITORY": d.REPO,
        "GITHUB_RUN_ATTEMPT": "1",
        "ILMAR_EXPECTED_SHA": "a" * 40,
        "GITHUB_SHA": "a" * 40,
    }
    d.run_context(env)
    for key, value in [
        ("GITHUB_RUN_ATTEMPT", "2"),
        ("GITHUB_EVENT_NAME", "pull_request"),
        ("GITHUB_REF", "refs/heads/feature"),
        ("GITHUB_SHA", "b" * 40),
    ]:
        bad = env | {key: value}
        with pytest.raises(d.DrillError):
            d.run_context(bad)


def test_preflight_without_admin_key_does_not_call_provider(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(d, "check_paid_context", lambda *_: None)
    monkeypatch.delenv("CAP_DRILL_ADMIN_READ", raising=False)
    monkeypatch.setenv("CAP_DRILL_KEY", "dummy")
    monkeypatch.setattr(d, "api", lambda *_a, **_k: pytest.fail("no calls without Admin key"))
    with pytest.raises(d.DrillError, match="required"):
        d.preflight(tmp_path / "state.json")


def test_cannot_reach_paid_path_without_verified_state(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(d, "check_paid_context", lambda *_: None)
    monkeypatch.setenv("CAP_DRILL_KEY", "dummy")
    monkeypatch.setattr(d, "api", lambda *_a, **_k: pytest.fail("no calls without preflight"))
    with pytest.raises(d.DrillError):
        d.exhaust(tmp_path / "missing.json")


def test_record_whitelists_fields_and_never_includes_secrets(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("CAP_DRILL_KEY", "sk-ant-secret-example")
    state = tmp_path / "state.json"
    d._private_json(
        state,
        {
            "phase": "verified",
            "workspace_id": "private",
            "api_key": "sk-ant-secret-example",
            "requests": 0,
        },
    )
    record = tmp_path / "record.json"
    d.record(state, record)
    text = record.read_text()
    assert "private" not in text
    assert "sk-ant" not in text
    assert '"requests": 0' in text


def test_config_uses_private_state_and_one_dollar(
    tmp_path: Path,
) -> None:
    base = tmp_path / "state"
    config = d._config(tmp_path / "cap.toml", repo_id=123456, state_dir=base)
    text = config.read_text()
    assert "provider_cap_attested_usd = 1" in text
    assert "reserve_per_execution_usd = 1" in text
    assert "monthly_usd = 1" in text
    assert "repository_id = 123456" in text
    assert "/var/tmp/ilmar-gate1" not in text
