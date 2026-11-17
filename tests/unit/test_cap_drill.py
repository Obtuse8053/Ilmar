# SPDX-License-Identifier: Apache-2.0
"""The one-time cap drill (scripts/cap_drill.py, .github/workflows/cap-drill.yml).

Offline: the Anthropic and GitHub APIs are fakes. What these tests hold:

* nothing is paid for before the read-only preflight verified the workspace,
  the key's workspace, the limit (at most the allowance) and the headroom;
* every request is sent once, never retried, and the allowance is a ceiling;
* Ilmar runs only after a direct spend-limit refusal, and §5's checks;
* one-time authorization, and no secret in anything kept;
* the workflow's secrets reach only the steps that need them.
"""

from __future__ import annotations

import json
import re
import urllib.parse
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import cap_drill
from ilmar.config import load_config

ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = (ROOT / ".github" / "workflows" / "cap-drill.yml").read_text()
KEY = "sk-ant-api03-" + "Qw3rTy7u" * 10 + "igAA"
OTHER_KEY = "sk-ant-api03-" + "Zz9Yy8Xx" * 10 + "abcd"
ADMIN_KEY = "sk-ant-admin01-" + "AdMiN123" * 8
TOKEN = "github_pat_" + "A1b2C3d4" * 8
NOW = datetime(2026, 11, 17, 9, 0, tzinfo=UTC)
SHA = "a" * 40
WS = "wrkspc_01CapTest"
INPUTS: dict[str, Any] = {
    "expected_sha": SHA,
    "authorization": "https://github.com/Obtuse8053/Ilmar/issues/93#issuecomment-123",
    "model": "claude-haiku-4-5",
    "attested_cap_usd": "0.80",
    "allowance_cents": "100",
    "max_request_cents": "5",
    "ilmar_reserve_cents": "10",
    "issue": "7",
}

DISPATCH = {
    "CAP_DRILL_EXPECTED_SHA": SHA,
    "CAP_DRILL_AUTHORIZATION": INPUTS["authorization"],
    "CAP_DRILL_MODEL": "claude-haiku-4-5",
    "CAP_DRILL_ATTESTED_CAP_USD": "0.80",
    "CAP_DRILL_ISSUE": "7",
}


def hint(key: str) -> str:
    return f"{key[:16]}...{key[-4:]}"


class FakeAnthropic:
    """The Admin API and the Messages API, as far as the drill uses them."""

    def __init__(
        self,
        *,
        limit: str | None = "80",
        spent: str = "0",
        limits_status: int = 200,
        key_workspace: str = WS,
        reported_workspace: str | None = WS,
        extra_keys: tuple[str, ...] = (),
        refuse_after_cents: Decimal | None = Decimal(80),
        refusal: tuple[int, str, str] = (
            400,
            "invalid_request_error",
            "This workspace has reached its spend limit.",
        ),
    ) -> None:
        self.limit = limit
        self.spent = Decimal(spent)
        self.limits_status = limits_status
        self.key_workspace = key_workspace
        self.reported_workspace = reported_workspace
        self.extra_keys = extra_keys
        self.refuse_after = refuse_after_cents
        self.refusal = refusal
        self.calls: list[tuple[str, str]] = []
        self.messages = 0
        self.charged = Decimal(0)

    def __call__(
        self, method: str, url: str, headers: Mapping[str, str], body: bytes | None, timeout: float
    ) -> cap_drill.Response:
        parts = urllib.parse.urlsplit(url)
        query = dict(urllib.parse.parse_qsl(parts.query))
        self.calls.append((method, parts.path))
        if parts.path.startswith("/v1/organizations/"):
            assert method == "GET"
            assert headers["x-api-key"] == ADMIN_KEY
            return self.admin(parts.path, query, headers)
        assert headers["x-api-key"] == KEY
        assert "anthropic-workspace-id" not in headers  # never steered to a workspace
        head = (
            {"anthropic-workspace-id": self.reported_workspace} if self.reported_workspace else {}
        )
        payload = json.loads(body or b"{}")
        text = payload["messages"][0]["content"]
        tokens = len(text) // 4 + 10
        if parts.path == cap_drill.COUNT_TOKENS:
            return 200, head, json.dumps({"input_tokens": tokens}).encode()
        self.messages += 1
        if self.refuse_after is not None and self.spent + self.charged >= self.refuse_after:
            status, kind, message = self.refusal
            return status, head, json.dumps({"error": {"type": kind, "message": message}}).encode()
        self.charged += Decimal(tokens) / 10**6 * 100 + Decimal(8 * 5) / 10**6 * 100
        usage = {"input_tokens": tokens, "output_tokens": 8}
        return 200, head, json.dumps({"usage": usage}).encode()

    def admin(
        self, path: str, query: Mapping[str, str], headers: Mapping[str, str]
    ) -> cap_drill.Response:
        def ok(data: object) -> cap_drill.Response:
            return 200, {}, json.dumps(data).encode()

        if path == cap_drill.WORKSPACES:
            return ok({"data": [{"id": WS, "name": "ilmar-cap-test", "archived_at": None},
                                {"id": "wrkspc_other", "name": "ilmar-test"}],
                       "has_more": False})  # fmt: skip
        if path == cap_drill.API_KEYS:
            keys = [{"id": "apikey_1", "partial_key_hint": hint(KEY), "status": "active",
                     "workspace_id": self.key_workspace}]  # fmt: skip
            keys += [{"id": f"apikey_{i + 2}", "partial_key_hint": hint(k),
                      "workspace_id": WS} for i, k in enumerate(self.extra_keys)]  # fmt: skip
            return ok({"data": keys, "has_more": False})
        if path == cap_drill.SPEND_LIMITS:
            assert headers["anthropic-beta"] == cap_drill.SPEND_BETA
            assert query["workspace_id"] == WS
            if self.limits_status != 200:
                return self.limits_status, {}, b'{"error": {"type": "permission_error"}}'
            rows = [] if self.limit is None else [{
                "type": "spend_limit", "amount": self.limit, "currency": "USD",
                "period": "monthly", "is_enabled": True,
                "scope": {"type": "workspace", "workspace_id": WS}}]  # fmt: skip
            return ok({"data": rows, "next_page": None})
        if path == cap_drill.COST_REPORT:
            total = self.spent + self.charged
            return ok({"data": [{"results": [
                {"amount": str(total), "currency": "USD", "workspace_id": WS},
                {"amount": "999", "currency": "USD", "workspace_id": "wrkspc_other"}]}],
                "has_more": False})  # fmt: skip
        raise AssertionError(path)


def admin(fake: FakeAnthropic) -> cap_drill.Admin:
    return cap_drill.Admin(ADMIN_KEY, fake)


# -- §3 preflight ---------------------------------------------------------------------------


def test_the_preflight_verifies_workspace_key_limit_and_spend_with_reads_only() -> None:
    fake = FakeAnthropic(limit="80", spent="0")
    facts = cap_drill.preflight(admin(fake), cap_drill.Model(KEY, fake), KEY, INPUTS, NOW)
    assert facts["limit_cents"] == "80"
    assert facts["month_to_date_before_cents"] == "0"
    assert {m for m, path in fake.calls if path.startswith("/v1/organizations/")} == {"GET"}
    assert fake.messages == 0


@pytest.mark.parametrize(
    ("fake", "result", "says"),
    [
        (FakeAnthropic(limits_status=403), "cap-unverified", "early-access"),
        (FakeAnthropic(limits_status=400), "cap-unverified", "cannot be read"),
        (FakeAnthropic(limit=None), "cap-unverified", "no spend limit"),
        (FakeAnthropic(key_workspace="wrkspc_other"), "cap-unverified", "does not list"),
        (FakeAnthropic(reported_workspace="wrkspc_other"), "cap-unverified", "not bound"),
        (FakeAnthropic(reported_workspace=None), "cap-unverified", "did not report"),
        (FakeAnthropic(extra_keys=(KEY,)), "cap-unverified", "2 active keys"),
        (FakeAnthropic(limit="101"), "cap-too-high", "above the $1.0000 allowance"),
        (FakeAnthropic(limit="90"), "cap-unverified", "attested cap $0.8000"),
    ],
)
def test_the_preflight_stops_before_any_paid_request(
    fake: FakeAnthropic, result: str, says: str
) -> None:
    with pytest.raises(cap_drill.StopError) as stop:
        cap_drill.preflight(admin(fake), cap_drill.Model(KEY, fake), KEY, INPUTS, NOW)
    assert stop.value.result == result
    assert says in str(stop.value)
    assert fake.messages == 0
    assert all(m == "GET" for m, path in fake.calls if path.startswith("/v1/organizations/"))


def test_a_key_from_another_organization_is_not_matched() -> None:
    fake = FakeAnthropic()
    with pytest.raises(cap_drill.StopError, match="0 active keys"):
        cap_drill.preflight(admin(fake), cap_drill.Model(KEY, fake), OTHER_KEY, INPUTS, NOW)


def test_a_one_dollar_limit_under_a_one_dollar_allowance_has_no_headroom() -> None:
    fake = FakeAnthropic(limit="100")
    inputs = INPUTS | {"attested_cap_usd": "1.00"}
    with pytest.raises(cap_drill.StopError) as stop:
        cap_drill.preflight(admin(fake), cap_drill.Model(KEY, fake), KEY, inputs, NOW)
    assert stop.value.result == "no-headroom"
    assert "lower the workspace limit to $0.8500 or less" in str(stop.value)
    assert fake.messages == 0


def test_the_preflight_sends_the_model_key_only_to_the_free_token_count() -> None:
    fake = FakeAnthropic()
    cap_drill.preflight(admin(fake), cap_drill.Model(KEY, fake), KEY, INPUTS, NOW)
    model_calls = [c for c in fake.calls if not c[1].startswith("/v1/organizations/")]
    assert model_calls == [("POST", cap_drill.COUNT_TOKENS)]


@pytest.mark.parametrize(
    ("given", "matches"),
    [
        (hint(KEY), True),
        (f"sk-ant-...{KEY[-4:]}", True),
        (f"{KEY[:16]}...{KEY[-3:]}", False),  # too short a suffix
        (f"{KEY[:16]}{KEY[-4:]}", False),  # no ellipsis
        (hint(OTHER_KEY), False),
        (None, False),
    ],
)
def test_hints(given: object, matches: bool) -> None:
    assert cap_drill.hint_matches(given, KEY) is matches


def test_the_admin_client_reads_only_four_paths() -> None:
    fake = FakeAnthropic()
    with pytest.raises(cap_drill.DrillError, match="not a read"):
        admin(fake).get("/v1/organizations/api_keys/apikey_1", [])
    assert fake.calls == []


def test_the_model_client_calls_only_two_paths() -> None:
    fake = FakeAnthropic()
    with pytest.raises(cap_drill.DrillError, match="not a call"):
        cap_drill.Model(KEY, fake).post("/v1/messages/batches", {})
    assert fake.calls == []


# -- §4 reaching the cap --------------------------------------------------------------------


def classify(text: str) -> str | None:
    return cap_drill.ilmar_classify(text)


def test_requests_go_one_at_a_time_until_the_spend_limit_refuses() -> None:
    fake = FakeAnthropic(refuse_after_cents=Decimal(80))
    out = cap_drill.exhaust(cap_drill.Model(KEY, fake), INPUTS, classify, KEY, "0")
    assert out["end"] == "spend-limit-refused"
    assert out["classified"] == "cap"
    assert out["requests"] == fake.messages
    assert Decimal(out["worst_case_cents"]) <= Decimal(INPUTS["max_request_cents"])
    assert fake.charged + Decimal(out["worst_case_cents"]) + 10 <= 100


def test_the_allowance_is_a_ceiling_when_the_provider_never_refuses() -> None:
    fake = FakeAnthropic(refuse_after_cents=None)
    out = cap_drill.exhaust(cap_drill.Model(KEY, fake), INPUTS, classify, KEY, "0")
    assert out["end"] == "allowance"
    worst = Decimal(out["worst_case_cents"])
    assert fake.charged <= 100 - 10
    assert fake.charged + worst + 10 > 100  # the next one could have exceeded it
    assert Decimal(out["estimated_cents"]) == fake.charged.quantize(Decimal("0.0001"))


def test_the_ceiling_counts_what_the_workspace_spent_before() -> None:
    fake = FakeAnthropic(refuse_after_cents=None, spent="30")
    out = cap_drill.exhaust(cap_drill.Model(KEY, fake), INPUTS, classify, KEY, "30")
    assert out["end"] == "allowance"
    assert 30 + fake.charged <= 100 - 10


@pytest.mark.parametrize(
    "refusal",
    [
        (529, "overloaded_error", "Overloaded"),
        (429, "rate_limit_error", "Number of requests has exceeded your rate limit"),
        (401, "authentication_error", "invalid x-api-key"),
    ],
)
def test_any_other_refusal_ends_it_without_a_retry(refusal: tuple[int, str, str]) -> None:
    fake = FakeAnthropic(refuse_after_cents=Decimal(0), refusal=refusal)
    out = cap_drill.exhaust(cap_drill.Model(KEY, fake), INPUTS, classify, KEY, "0")
    assert out["end"] == "other-refusal"
    assert fake.messages == 1


def test_a_provider_message_echoing_the_key_is_redacted() -> None:
    refusal = (400, "invalid_request_error", f"spend limit reached for {KEY}")
    fake = FakeAnthropic(refuse_after_cents=Decimal(0), refusal=refusal)
    out = cap_drill.exhaust(cap_drill.Model(KEY, fake), INPUTS, classify, KEY, "0")
    assert KEY not in json.dumps(out)
    assert "[redacted]" in out["message"]


# -- §5 Ilmar -------------------------------------------------------------------------------


def test_the_drill_configuration_is_gate1s_with_only_the_drills_changes(tmp_path: Path) -> None:
    text = cap_drill.drill_config(
        cap_drill.GATE1_CONFIG.read_text(), state_dir=tmp_path / "state", repository_id=42,
        model="claude-haiku-4-5", limit_cents=Decimal(80), reserve_cents=Decimal(10),
    )  # fmt: skip
    path = tmp_path / "drill.toml"
    path.write_text(text)
    config = load_config(path)
    assert config.instance_id == "cap-drill"
    assert config.budget is not None
    assert config.budget.provider_cap_attested_micros == 800_000
    assert config.budget.limits.reserve_micros == 100_000
    assert 'api_key_env = "CAP_DRILL_KEY"' in text
    assert text.count('token_env = "CAP_DRILL_GITHUB_TOKEN"') == 2
    assert 'max_turns = 1, model = "claude-haiku-4-5"' in text
    assert 'expected_version = "2.1.318"' in text
    assert "sk-ant" not in text


def test_the_drill_configuration_refuses_a_changed_gate1_file(tmp_path: Path) -> None:
    gate1 = cap_drill.GATE1_CONFIG.read_text().replace("max_turns = 60", "max_turns = 61")
    with pytest.raises(cap_drill.DrillError, match="changed"):
        cap_drill.drill_config(gate1, state_dir=tmp_path, repository_id=1, model="m",
                               limit_cents=Decimal(80), reserve_cents=Decimal(10))  # fmt: skip


def good_ilmar() -> dict[str, Any]:
    facts = {"executions": 1, "attempts": 1, "provider_refused": 1, "provider_cap_suspensions": 1}
    return {
        "after_run1": facts, "after_run2": dict(facts), "budget_exit": 78,
        "budget_output": "suspended: provider-cap", "run2_exit": 78,
        "containers_before_run2": 0, "containers_after_run2": 0, "change_requests_opened": 0,
    }  # fmt: skip


@pytest.mark.parametrize(
    ("change", "problem"),
    [
        ({}, None),
        ({"budget_exit": 0}, "ilmar budget"),
        ({"run2_exit": 0}, "second pass did not exit 78"),
        ({"containers_after_run2": 1}, "started a container"),
        ({"change_requests_opened": 1}, "change request"),
        ({"after_run2": {"executions": 2, "attempts": 2, "provider_refused": 1,
                         "provider_cap_suspensions": 1}}, "changed the executions"),
        ({"after_run1": {"executions": 1, "attempts": 2, "provider_refused": 1,
                         "provider_cap_suspensions": 1}}, "exactly one execution"),
        ({"after_run1": {"executions": 1, "attempts": 1, "provider_refused": 0,
                         "provider_cap_suspensions": 0}}, "provider-refused"),
    ],
)  # fmt: skip
def test_ilmar_checks(change: dict[str, Any], problem: str | None) -> None:
    out = good_ilmar() | change
    if "after_run1" in change and "after_run2" not in change:
        out["after_run2"] = dict(change["after_run1"])
    found = cap_drill.ilmar_checks(out)
    if problem is None:
        assert found == []
    else:
        assert any(problem in p for p in found), found


def test_facts_count_ilmars_events() -> None:
    log = [
        {"type": "execution.created"},
        {"type": "budget.reserved"},
        {"type": "execution.transitioned", "reason": "agent-failed"},
        {"type": "execution.transitioned", "reason": "provider-refused"},
        {"type": "instance.suspended", "reason": "provider-cap"},
    ]
    assert cap_drill.facts_of(log) == {
        "executions": 1, "attempts": 1, "provider_refused": 1, "provider_cap_suspensions": 1,
    }  # fmt: skip


def test_ilmar_never_runs_unless_the_cap_refused_directly(tmp_path: Path) -> None:
    def no_run(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("nothing may run")

    def no_http(*args: Any) -> Any:
        raise AssertionError("nothing may be called")

    for state in ({}, {"exhaust": {"end": "allowance"}}, {"exhaust": {"end": "other-refusal"}}):
        with pytest.raises(cap_drill.DrillError, match="Ilmar does not run"):
            cap_drill.through_ilmar(run=no_run, http=no_http, env={KEY_ENV: KEY}, inputs=INPUTS,
                                    state=state, work=tmp_path)  # fmt: skip


KEY_ENV = cap_drill.KEY_ENV


class FakeGitHub:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []
        self.labels: set[str] = set()
        self.pulls = 3

    def __call__(
        self, method: str, url: str, headers: Mapping[str, str], body: bytes | None, timeout: float
    ) -> cap_drill.Response:
        path = urllib.parse.urlsplit(url).path
        self.calls.append((method, path))
        assert headers["Authorization"] == f"Bearer {TOKEN}"
        base = f"/repos/{cap_drill.DISPOSABLE}"
        if method == "GET" and path == f"{base}/issues/7":
            return 200, {}, json.dumps({"state": "open", "user": {"login": "Obtuse8053"},
                                    "labels": []}).encode()  # fmt: skip
        if method == "GET" and path == base:
            return 200, {}, b'{"id": 4242}'
        if method == "GET" and path == f"{base}/pulls":
            return 200, {}, json.dumps([{}] * self.pulls).encode()
        if method == "POST" and path.endswith("/labels"):
            self.labels |= set(json.loads(body or b"{}")["labels"])
            return 200, {}, b"[]"
        if method == "DELETE":
            self.labels.discard(path.rsplit("/", 1)[1])
            return 404, {}, b""
        if method in ("POST", "PATCH"):
            return 201, {}, b"{}"
        raise AssertionError((method, path))


def test_through_ilmar_runs_the_coordinator_twice_and_cleans_up(tmp_path: Path) -> None:
    log: list[dict[str, Any]] = []
    calls: list[list[str]] = []

    def run(argv: list[str], **kwargs: Any) -> Any:
        calls.append(argv)
        env = kwargs.get("env") or {}
        if argv[0] == "podman":
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        assert env.get(cap_drill.KEY_ENV) == KEY
        assert cap_drill.ADMIN_ENV not in env  # the Admin key never reaches Ilmar
        command = argv[3]
        if command == "config":
            return SimpleNamespace(returncode=0, stdout="OK", stderr="")
        if command == "run":
            if not log:
                log.extend([{"type": "execution.created"}, {"type": "budget.reserved"},
                            {"type": "execution.transitioned", "reason": "provider-refused"},
                            {"type": "instance.suspended", "reason": "provider-cap"}])  # fmt: skip
                return SimpleNamespace(returncode=1, stdout="[provider-refused]", stderr="")
            return SimpleNamespace(returncode=78, stdout="suspended", stderr="")
        if command == "budget":
            return SimpleNamespace(returncode=78, stdout="suspended: provider-cap", stderr="")
        if command == "events":
            lines = "\n".join(json.dumps(e) for e in log)
            return SimpleNamespace(returncode=0 if log else 2, stdout=lines, stderr="")
        raise AssertionError(argv)

    hub = FakeGitHub()
    env = {cap_drill.KEY_ENV: KEY, cap_drill.GITHUB_ENV: TOKEN, cap_drill.ADMIN_ENV: ADMIN_KEY,
           "PATH": "/usr/bin", "GITHUB_RUN_ID": "1"}  # fmt: skip
    state = {"exhaust": {"end": "spend-limit-refused"}, "preflight": {"limit_cents": "80"}}
    out = cap_drill.through_ilmar(run=run, http=hub, env=env, inputs=INPUTS, state=state,
                                  work=tmp_path)  # fmt: skip
    assert out["failed_checks"] == [], out
    assert [c[3:5] for c in calls if c[0] != "podman" and c[3] == "run"] == [
        ["run", "--once"], ["run", "--once"]]  # fmt: skip
    assert hub.labels == set()  # the label was removed again
    assert ("PATCH", f"/repos/{cap_drill.DISPOSABLE}/issues/7") in hub.calls
    assert KEY not in json.dumps(out)
    assert TOKEN not in json.dumps(out)


# -- the guard, the marker, the record ------------------------------------------------------


def authorization(**change: Any) -> dict[str, Any]:
    comment = {
        "issue_url": "https://api.github.com/repos/Obtuse8053/Ilmar/issues/93",
        "user": {"login": "Obtuse8053"}, "author_association": "OWNER",
        "created_at": "2026-11-16T10:00:00Z", "updated_at": "2026-11-16T10:00:00Z",
        "body": f"Go ahead.\ncap-drill: authorize {SHA} workspace=ilmar-cap-test allowance=1.00\n",
    }  # fmt: skip
    return comment | change


def github_get(comment: Mapping[str, Any], *, marker: bool = False, reached: bool = False) -> Any:
    def get(path: str, token: str) -> Any:
        if path.endswith("/issues/comments/123"):
            return comment
        if "/issues/93/comments" in path:
            return [{"body": f"{cap_drill.MARKER} https://x/runs/9"}] if marker else []
        if path.endswith("/runs?per_page=100"):
            return {"workflow_runs": [{"id": 9}, {"id": 10}]}
        if "/runs/9/jobs" in path:
            step = {"name": "Preflight (§3, read-only)",
                    "conclusion": "failure" if reached else "skipped"}  # fmt: skip
            return {"jobs": [{"name": "Drill", "steps": [step]}]}
        raise AssertionError(path)

    return get


@pytest.mark.parametrize(
    ("comment", "says"),
    [
        (authorization(), None),
        (authorization(user={"login": "someone"}), "maintainer's"),
        (authorization(author_association="MEMBER"), "maintainer's"),
        (authorization(updated_at="2026-11-16T11:00:00Z"), "edited"),
        (authorization(created_at="2026-11-01T10:00:00Z", updated_at="2026-11-01T10:00:00Z"),
         "seven days"),
        (authorization(body=f"cap-drill: authorize {SHA} workspace=ilmar-test allowance=1.00"),
         "must say"),
        (authorization(body=f"cap-drill: authorize {SHA} workspace=ilmar-cap-test allowance=2.00"),
         "must say"),
        (authorization(body=f"cap-drill: authorize {'b' * 40} workspace=ilmar-cap-test "
                            "allowance=1.00"), "must say"),
        (authorization(issue_url="https://api.github.com/repos/Obtuse8053/Ilmar/issues/94"),
         "not on #93"),
    ],
)  # fmt: skip
def test_the_authorization(comment: dict[str, Any], says: str | None) -> None:
    problems = cap_drill.check_authorization(github_get(comment), "t", INPUTS, NOW)
    if says is None:
        assert problems == []
    else:
        assert any(says in p for p in problems), problems


def test_once_a_marker_or_a_run_that_reached_the_preflight_refuses() -> None:
    env = {"GITHUB_RUN_ID": "10"}
    assert cap_drill.check_once(github_get(authorization()), "t", env) == []
    assert cap_drill.check_once(github_get(authorization(), marker=True), "t", env)
    assert cap_drill.check_once(github_get(authorization(), reached=True), "t", env)


def test_the_inputs_are_validated_and_the_allowance_is_capped_at_one_dollar() -> None:
    env = dict(DISPATCH)
    inputs = cap_drill.read_inputs(env)
    assert inputs["allowance_cents"] == "100"
    with pytest.raises(cap_drill.DrillError, match="allowance_cents"):
        cap_drill.read_inputs(env | {"CAP_DRILL_ALLOWANCE_CENTS": "101"})
    with pytest.raises(cap_drill.DrillError, match="model must be one of"):
        cap_drill.read_inputs(env | {"CAP_DRILL_MODEL": "claude-opus-5-5"})
    with pytest.raises(cap_drill.DrillError, match="attested_cap_usd"):
        cap_drill.read_inputs(env | {"CAP_DRILL_ATTESTED_CAP_USD": "1.01"})


@pytest.mark.parametrize(
    ("state", "result"),
    [
        ({"stop": {"result": "no-headroom", "message": "m"}}, "no-headroom"),
        ({"preflight": {"limit_cents": "80"}, "exhaust": {"end": "allowance"}}, "inconclusive"),
        ({"preflight": {"limit_cents": "80"}, "exhaust": {"end": "spend-limit-refused"},
          "ilmar": {"failed_checks": []}, "cost": {"spend": "within-allowance"}},
         "cap-enforced-through-ilmar"),
        ({"preflight": {"limit_cents": "80"}, "exhaust": {"end": "spend-limit-refused"},
          "ilmar": {"failed_checks": []}, "cost": {"spend": "pending"}}, "inconclusive"),
        ({"preflight": {"limit_cents": "80"}, "exhaust": {"end": "spend-limit-refused"},
          "ilmar": {"failed_checks": []}, "cost": {"spend": "overshoot"}}, "inconclusive"),
        ({"preflight": {"limit_cents": "80"}, "exhaust": {"end": "spend-limit-refused"},
          "ilmar": {"failed_checks": []}}, "inconclusive"),
        ({"preflight": {"limit_cents": "80"}, "exhaust": {"end": "spend-limit-refused"},
          "ilmar": {"failed_checks": ["x"]}}, "ilmar-check-failed"),
    ],
)  # fmt: skip
def test_the_record_result(state: dict[str, Any], result: str) -> None:
    assert cap_drill.record(state, INPUTS, {})["result"] == result


def test_the_main_flow_stops_at_the_preflight_and_never_spends(tmp_path: Path) -> None:
    fake = FakeAnthropic(limit="100")
    state = tmp_path / "state.json"
    env = DISPATCH | {cap_drill.KEY_ENV: KEY, cap_drill.ADMIN_ENV: ADMIN_KEY,
                      "CAP_DRILL_ATTESTED_CAP_USD": "1.00"}  # fmt: skip

    def main(*argv: str) -> int:
        return cap_drill.main([*argv, "--state", str(state)], environ=env, http=fake,
                              now=lambda: NOW)  # fmt: skip

    assert main("preflight") == 1
    assert json.loads(state.read_text())["stop"]["result"] == "no-headroom"
    assert main("exhaust") == 1  # refused: the preflight did not pass
    assert fake.messages == 0
    assert main("finish", str(tmp_path / "record.json")) == 0
    text = (tmp_path / "record.json").read_text()
    assert json.loads(text)["result"] == "no-headroom"
    assert KEY not in text
    assert ADMIN_KEY not in text
    assert WS not in text


def test_the_main_flow_reaches_the_cap_and_reports_costs_apart(tmp_path: Path) -> None:
    fake = FakeAnthropic(limit="80")
    state = tmp_path / "state.json"
    env = DISPATCH | {cap_drill.KEY_ENV: KEY, cap_drill.ADMIN_ENV: ADMIN_KEY}

    def main(*argv: str) -> int:
        return cap_drill.main([*argv, "--state", str(state)], environ=env, http=fake,
                              now=lambda: NOW + timedelta(hours=1))  # fmt: skip

    assert main("preflight") == 0
    assert main("exhaust") == 0
    assert main("actual-cost") == 0
    saved = json.loads(state.read_text())
    assert saved["exhaust"]["end"] == "spend-limit-refused"
    assert Decimal(saved["cost"]["actual_cents"]) == fake.charged
    assert saved["cost"]["spend"] == "within-allowance"
    assert Decimal(saved["exhaust"]["estimated_cents"]) == fake.charged.quantize(Decimal("0.0001"))


# -- the workflow ---------------------------------------------------------------------------


SECRET = re.compile(r"\$\{\{\s*secrets\.([A-Z_]+)")


def steps() -> dict[str, str]:
    """Each named step's block, by name."""
    blocks = re.split(r"\n      - (?=name: |uses: )", WORKFLOW)
    named = {}
    for block in blocks[1:]:
        found = re.search(r"name: (.+)", block)
        if found:
            named[found.group(1).strip()] = block
    return named


def test_the_workflow_is_manual_only_with_no_default_permissions() -> None:
    triggers = WORKFLOW.split("\non:\n", 1)[1].split("\npermissions:", 1)[0]
    assert re.findall(r"^  ([a-z_]+):", triggers, flags=re.MULTILINE) == ["workflow_dispatch"]
    assert "\npermissions: {}\n" in WORKFLOW
    assert "continue-on-error" not in WORKFLOW
    assert WORKFLOW.count("environment: cap-drill") == 1


def test_secrets_reach_only_the_steps_that_need_them() -> None:
    expected = {
        "Preflight (§3, read-only)": {"CAP_DRILL_ADMIN_KEY", "CAP_DRILL_KEY"},
        "Reach the cap (§4)": {"CAP_DRILL_KEY"},
        "Ilmar's coordinator (§5)": {"CAP_DRILL_KEY", "CAP_DRILL_GITHUB_TOKEN"},
        "Actual cost (§6, read-only)": {"CAP_DRILL_ADMIN_KEY"},
    }
    found = {name: set(SECRET.findall(block)) for name, block in steps().items()}
    assert {name: s for name, s in found.items() if s} == expected
    assert len(SECRET.findall(WORKFLOW)) == 6


def test_no_input_is_expanded_in_a_shell() -> None:
    for line in WORKFLOW.splitlines():
        if line.strip().startswith(("run:", "python", "podman", "exec")):
            assert "${{" not in line, line


def test_paid_steps_run_only_if_everything_before_them_succeeded() -> None:
    named = steps()
    for name in ("Preflight (§3, read-only)", "Reach the cap (§4)", "Ilmar's coordinator (§5)"):
        assert "if:" not in named[name], name
    assert "if: always()" in named["Actual cost (§6, read-only)"]
