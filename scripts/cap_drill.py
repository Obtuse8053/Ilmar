# SPDX-License-Identifier: Apache-2.0
"""One-time, fail-closed Gate 1 Anthropic cap drill. Never called from CI tests.

Only .github/workflows/cap-drill.yml may drive the paid steps. No command
sends a model request before the Admin limit and model-key binding pass.
The workflow is manually dispatched, approved, and pinned to main.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation, ROUND_UP
from pathlib import Path
from typing import Any, Mapping

ROOT = Path(__file__).resolve().parents[1]
REPO = "Obtuse8053/Ilmar"
DISPOSABLE = "Obtuse8053/ilmar-disposable"
ISSUE = 93
WORKSPACE = "ilmar-cap-test"
CEILING_CENTS = 100
MODEL = "claude-opus-5-5"
API_VERSION = "2026-10-21"
BETA = "spend-limit-reads-2026-09-26"
INPUT_PRICE = Decimal("4")  # USD / million input tokens; review before dispatch
OUTPUT_PRICE = Decimal("20")  # USD / million output tokens; review before dispatch
MAX_OUTPUT = 8
MAX_REQUESTS = 120
PROMPT = "Reply OK. The following text is harmless padding:\n" + ("red blue green yellow " * 2200)
SHA = re.compile(r"[0-9a-f]{40}\Z")
WORKSPACE_ID = re.compile(r"wrkspc_[A-Za-z0-9]+\Z")
KEY_SHAPE = re.compile(r"(sk-ant-[A-Za-z0-9_-]{12,}|github_pat_[A-Za-z0-9_]{12,})")
CAP_MESSAGE = re.compile(r"(workspace|organization).*(usage|spend|billing|credit).*(limit|cap)|"
                         r"(usage|spend|billing|credit).*(limit|cap)", re.I)
MODEL_BODY: dict[str, Any] = {
    "model": MODEL, "max_tokens": MAX_OUTPUT, "messages": [{"role": "user", "content": PROMPT}]
}


class DrillError(Exception):
    """Safe, generic failure that cannot contain a credential or API response."""


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        return None


def api(
    path: str, key: str, *, admin: bool, method: str = "GET",
    payload: Mapping[str, Any] | None = None, beta: bool = False,
) -> tuple[int, dict[str, Any], Mapping[str, str]]:
    """One request, with no redirects, logging, retry or credential-bearing error."""
    if not path.startswith("/") or "//" in path or not key:
        raise DrillError("invalid API request")
    origin = "https://api.anthropic.com"
    headers = {
        "anthropic-version": API_VERSION,
        "x-api-key": key,
        "accept": "application/json",
    }
    if beta:
        headers["anthropic-beta"] = BETA
    data = None if payload is None else json.dumps(payload, separators=(",", ":")).encode()
    if data is not None:
        headers["content-type"] = "application/json"
    request = urllib.request.Request(origin + path, data=data, headers=headers, method=method)
    opener = urllib.request.build_opener(_NoRedirect)
    try:
        with opener.open(request, timeout=25) as response:
            raw = response.read(262145)
            status = response.status
            response_headers = dict(response.headers.items())
    except urllib.error.HTTPError as error:
        status = error.code
        raw = error.read(262145)
        response_headers = dict(error.headers.items())
    except (urllib.error.URLError, TimeoutError, OSError):
        raise DrillError("Anthropic request failed; no retry") from None
    if len(raw) > 262144:
        raise DrillError("Anthropic response too large")
    try:
        body = json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        raise DrillError("Anthropic returned invalid JSON") from None
    if not isinstance(body, dict):
        raise DrillError("Anthropic response must be an object")
    return status, body, {k.lower(): v for k, v in response_headers.items()}


def github(path: str, *, method: str = "GET", payload: Any = None) -> Any:
    """GitHub API under its fixed origin; never follows cross-origin redirects."""
    token = os.environ.get("GH_TOKEN", "")
    if not token or not path.startswith("/") or "//" in path:
        raise DrillError("GitHub guard credentials/path missing")
    body = None if payload is None else json.dumps(payload).encode()
    req = urllib.request.Request(
        "https://api.github.com" + path,
        data=body,
        headers={"authorization": f"Bearer {token}", "accept": "application/vnd.github+json",
                 "x-github-api-version": "2022-11-28", "user-agent": "ilmar-cap-drill",
                 "content-type": "application/json"},
        method=method,
    )
    try:
        with urllib.request.build_opener(_NoRedirect).open(req, timeout=25) as response:
            result = json.loads(response.read(262145))
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, ValueError):
        raise DrillError("GitHub guard request failed") from None
    return result


def run_context(env: Mapping[str, str]) -> None:
    if (
        env.get("GITHUB_ACTIONS") != "true"
        or env.get("GITHUB_EVENT_NAME") != "workflow_dispatch"
        or env.get("GITHUB_REF") != "refs/heads/main"
        or env.get("GITHUB_REPOSITORY") != REPO
        or env.get("GITHUB_RUN_ATTEMPT") != "1"
        or not SHA.fullmatch(env.get("ILMAR_EXPECTED_SHA", ""))
        or env.get("ILMAR_EXPECTED_SHA") != env.get("GITHUB_SHA")
    ):
        raise DrillError("not the first manual run on the reviewed main commit")


def authorization(env: Mapping[str, str]) -> None:
    run_context(env)
    sha = env["ILMAR_EXPECTED_SHA"]
    main = github(f"/repos/{REPO}/git/ref/heads/main")
    if main.get("object", {}).get("sha") != sha:
        raise DrillError("main moved since the cap drill was reviewed")
    link = env.get("ILMAR_CAP_AUTHORIZATION", "")
    match = re.fullmatch(
        r"https://github\.com/Obtuse8053/Ilmar/issues/93#issuecomment-(\d+)", link
    )
    if not match:
        raise DrillError("a maintainer #93 authorization comment URL is required")
    comment = github(f"/repos/{REPO}/issues/comments/{match[1]}")
    body = str(comment.get("body", ""))
    if (
        comment.get("user", {}).get("login") != "Obtuse8053"
        or sha not in body
        or WORKSPACE not in body
        or "$1.00" not in body
    ):
        raise DrillError("authorization does not match maintainer, SHA, workspace and cap")
    if os.environ.get("ILMAR_CAP_CENTS") != "100":
        raise DrillError("only an attested USD 1.00 cap is authorized")


def markers() -> list[str]:
    found: list[str] = []
    for page in range(1, 21):
        comments = github(f"/repos/{REPO}/issues/{ISSUE}/comments?per_page=100&page={page}")
        if not isinstance(comments, list):
            raise DrillError("cannot verify one-time markers")
        found.extend(str(c.get("body", "")) for c in comments
                     if str(c.get("body", "")).startswith("ilmar-cap-drill: started "))
        if len(comments) < 100:
            return found
    raise DrillError("issue comment history too large to verify once-only marker")


def guard(env: Mapping[str, str]) -> None:
    authorization(env)
    if markers():
        raise DrillError("the one-time cap drill has already started")


def mark(env: Mapping[str, str]) -> None:
    guard(env)
    run_url = f"https://github.com/{REPO}/actions/runs/{env['GITHUB_RUN_ID']}"
    github(f"/repos/{REPO}/issues/{ISSUE}/comments", method="POST",
           payload={"body": f"ilmar-cap-drill: started {run_url}"})
    if markers() != [f"ilmar-cap-drill: started {run_url}"]:
        raise DrillError("could not confirm the once-only marker")


def check_paid_context(env: Mapping[str, str]) -> None:
    run_context(env)
    run_url = f"https://github.com/{REPO}/actions/runs/{env['GITHUB_RUN_ID']}"
    if markers() != [f"ilmar-cap-drill: started {run_url}"]:
        raise DrillError("one-time authorization marker does not match this run")
    head = github(f"/repos/{REPO}/git/ref/heads/main")
    if head.get("object", {}).get("sha") != env["ILMAR_EXPECTED_SHA"]:
        raise DrillError("main moved before credentials were used")


def read_workspaces(admin_key: str) -> str:
    status, data, _ = api("/v1/organizations/workspaces?limit=1000", admin_key, admin=True)
    if status != 200:
        raise DrillError("workspace identity cannot be verified via Admin API")
    matches = [w for w in data.get("data", [])
               if isinstance(w, dict) and w.get("name") == WORKSPACE and w.get("archived_at") is None]
    if len(matches) != 1 or not WORKSPACE_ID.fullmatch(str(matches[0].get("id", ""))):
        raise DrillError("workspace name not unique or its identity is invalid")
    if data.get("has_more"):
        raise DrillError("workspace listing incomplete")
    return str(matches[0]["id"])


def validate_limit(data: Mapping[str, Any], workspace_id: str, *, ceiling: int = 100) -> int:
    """Exactly one enabled monthly workspace limit in USD; never infer a cap."""
    records = data.get("data")
    if not isinstance(records, list) or data.get("next_page"):
        raise DrillError("spend limit read is incomplete")
    matches = [
        x for x in records if isinstance(x, dict) and
        x.get("scope") == {"type": "workspace", "workspace_id": workspace_id} and
        x.get("period") == "monthly" and x.get("currency") == "USD" and
        x.get("is_enabled") is True
    ]
    if len(matches) != 1 or not re.fullmatch(r"\d+", str(matches[0].get("amount", ""))):
        raise DrillError("workspace spending limit missing or ambiguous")
    value = int(matches[0]["amount"])
    if not 0 < value <= ceiling:
        raise DrillError("cap-too-high: workspace limit exceeds USD 1.00")
    return value


def verified_cap(admin_key: str, workspace_id: str) -> int:
    path = "/v1/organizations/spend_limits?" + urllib.parse.urlencode(
        {"workspace_id": workspace_id, "limit": "1000"}
    )
    status, data, _ = api(path, admin_key, admin=True, beta=True)
    if status != 200:
        raise DrillError("cap-unverified: spend limit reads unavailable (early access)")
    return validate_limit(data, workspace_id)


def verify_model_key(key: str, workspace_id: str) -> int:
    """A free count call, without a workspace selection header, proves key binding."""
    status, body, headers = api(
        "/v1/messages/count_tokens", key, admin=False, method="POST", payload=MODEL_BODY
    )
    if (
        status != 200 or headers.get("anthropic-workspace-id") != workspace_id
        or type(body.get("input_tokens")) is not int or body["input_tokens"] <= 0
    ):
        raise DrillError("model key could not be proved scoped to the capped workspace")
    return int(body["input_tokens"])


def month_spend(admin_key: str, workspace_id: str, *, now: datetime | None = None) -> Decimal:
    """Admin cost report, expressed in USD (the API's amount is cents)."""
    now = datetime.now(UTC) if now is None else now.astimezone(UTC)
    start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    end = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    query = urllib.parse.urlencode({
        "starting_at": start.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "ending_at": end.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "bucket_width": "1d", "group_by[]": "workspace_id", "limit": "31",
    })
    total = Decimal("0")
    for _ in range(5):
        status, data, _ = api("/v1/organizations/cost_report?" + query, admin_key, admin=True)
        if status != 200 or not isinstance(data.get("data"), list):
            raise DrillError("workspace month-to-date costs cannot be verified")
        for bucket in data["data"]:
            for result in bucket.get("results", []):
                if result.get("workspace_id") != workspace_id:
                    continue
                if result.get("currency") != "USD":
                    raise DrillError("unexpected cost-report currency")
                try:
                    amount = Decimal(str(result["amount"])) / 100
                except (InvalidOperation, KeyError):
                    raise DrillError("invalid cost-report amount") from None
                if amount < 0 or not amount.is_finite():
                    raise DrillError("invalid negative cost report")
                total += amount
        if not data.get("has_more"):
            return total
        page = data.get("next_page")
        if not isinstance(page, str) or not page:
            raise DrillError("cost report pagination invalid")
        query += "&page=" + urllib.parse.quote(page, safe="")
    raise DrillError("cost report exceeds bounded pagination")


def max_cost(input_tokens: int, *, output_tokens: int = MAX_OUTPUT) -> Decimal:
    if type(input_tokens) is not int or not 0 < input_tokens < 100000:
        raise DrillError("token count invalid")
    amount = (Decimal(input_tokens) * INPUT_PRICE + Decimal(output_tokens) * OUTPUT_PRICE)
    return (amount * Decimal("1.10") / 1_000_000).quantize(Decimal("0.000001"), rounding=ROUND_UP)


def reported_cost(usage: Mapping[str, Any]) -> Decimal:
    a, b = usage.get("input_tokens"), usage.get("output_tokens")
    if type(a) is not int or type(b) is not int or a < 0 or not 0 <= b <= MAX_OUTPUT:
        raise DrillError("untrusted Anthropic usage")
    amount = (Decimal(a) * INPUT_PRICE + Decimal(b) * OUTPUT_PRICE) / 1_000_000
    return amount.quantize(Decimal("0.000001"), rounding=ROUND_UP)


def is_cap_refusal(status: int, body: Mapping[str, Any]) -> bool:
    error = body.get("error")
    return (
        status == 400 and isinstance(error, dict) and
        isinstance(error.get("message"), str) and
        bool(CAP_MESSAGE.search(error["message"])) and
        error.get("type") in ("invalid_request_error", "permission_error")
    )


def _private_json(path: Path, state: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink():
        raise DrillError("state file is a symlink")
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w") as handle:
        json.dump(state, handle, sort_keys=True)
        handle.write("\n")


def _state(path: Path) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise DrillError("verified preflight state is missing")
    data = json.loads(path.read_text())
    if not isinstance(data, dict):
        raise DrillError("invalid preflight state")
    return data


def preflight(path: Path) -> None:
    check_paid_context(os.environ)
    admin, model_key = os.environ.get("CAP_DRILL_ADMIN_READ", ""), os.environ.get("CAP_DRILL_KEY", "")
    if not admin or not model_key or admin == model_key:
        raise DrillError("Admin read and model keys are both required")
    workspace_id = read_workspaces(admin)
    limit = verified_cap(admin, workspace_id)
    tokens = verify_model_key(model_key, workspace_id)
    spend = month_spend(admin, workspace_id)
    if spend >= Decimal(limit) / 100:
        raise DrillError("the workspace already exhausted its cap")
    _private_json(path, {
        "phase": "verified", "workspace_id": workspace_id, "cap_cents": limit,
        "spent_before_usd": str(spend), "input_tokens": tokens,
        "estimated_spend_usd": "0", "requests": 0, "model": MODEL,
        "outcome": "cap-unverified",
    })


def exhaust(path: Path) -> None:
    check_paid_context(os.environ)
    state = _state(path)
    if state.get("phase") != "verified" or state.get("cap_cents") != CEILING_CENTS:
        raise DrillError("cap verification did not finish at USD 1.00")
    key = os.environ.get("CAP_DRILL_KEY", "")
    if not key:
        raise DrillError("model key missing")
    worst = max_cost(state["input_tokens"])
    spent = Decimal(str(state["spent_before_usd"]))
    estimated = Decimal("0")
    cap = Decimal(CEILING_CENTS) / 100
    for n in range(MAX_REQUESTS):
        if spent + estimated + worst > cap:
            state["outcome"] = "inconclusive: conservative cost ceiling"
            _private_json(path, state)
            raise DrillError("no confirmed provider refusal within the USD 1.00 ceiling")
        status, data, _ = api("/v1/messages", key, admin=False, method="POST", payload=MODEL_BODY)
        state["requests"] = n + 1
        if status != 200:
            state["provider_status"] = status
            state["provider_error_type"] = str(data.get("error", {}).get("type", ""))[:60]
            if not is_cap_refusal(status, data):
                state["outcome"] = "inconclusive: not a workspace spending refusal"
                _private_json(path, state)
                raise DrillError("provider returned a non-cap refusal; no retry")
            state["phase"] = "cap-refused"
            state["outcome"] = "cap-observed-awaiting-ilmar"
            _private_json(path, state)
            return
        usage = data.get("usage")
        if not isinstance(usage, dict):
            raise DrillError("no trustworthy usage report; stop spending")
        estimated += reported_cost(usage)
        state["estimated_spend_usd"] = str(estimated)
        if spent + estimated > cap:
            raise DrillError("accounting exceeded local authorization; stop")
        _private_json(path, state)
    state["outcome"] = "inconclusive: request cap reached"
    _private_json(path, state)
    raise DrillError("provider did not refuse within request limit")


def _config(path: Path, *, repo_id: int, state_dir: Path) -> Path:
    original = (ROOT / "deploy/gate1/ilmar.gate1.toml").read_text()
    changes = {
        'state_dir = "/var/tmp/ilmar-gate1"': f'state_dir = "{state_dir}"',
        'instance_id = "gate1"': 'instance_id = "cap-drill"',
        'repository_id = 0': f'repository_id = {repo_id}',
        'path = "/var/tmp/ilmar-gate1/executions.sqlite3"':
            f'path = "{state_dir / "executions.sqlite3"}"',
        'kill_switch_file = "/var/tmp/ilmar-gate1-KILL"':
            f'kill_switch_file = "{state_dir / "KILL"}"',
        "daily_usd = 5": "daily_usd = 1",
        "monthly_usd = 10": "monthly_usd = 1",
        "reserve_per_execution_usd = 2": "reserve_per_execution_usd = 1",
        "provider_cap_attested_usd = 10": "provider_cap_attested_usd = 1",
    }
    for before, after in changes.items():
        if original.count(before) != 1:
            raise DrillError("Gate 1 configuration changed; review before cap drill")
        original = original.replace(before, after)
    _private_json(path.with_suffix(".marker.json"), {"template": "Gate 1", "repo_id": repo_id})
    path.write_text(original)
    path.chmod(0o600)
    return path


def _repo_issue(github_key: str, number: int) -> int:
    """Validate exact private disposable target before adding an eligibility label."""
    saved = os.environ.get("GH_TOKEN")
    os.environ["GH_TOKEN"] = github_key
    try:
        repo = github(f"/repos/{DISPOSABLE}")
        if (
            not repo.get("private") or "ilmar-disposable" not in repo.get("topics", [])
            or repo.get("full_name") != DISPOSABLE or type(repo.get("id")) is not int
        ):
            raise DrillError("disposable repository not verified")
        issue = github(f"/repos/{DISPOSABLE}/issues/{number}")
        labels = [l.get("name") for l in issue.get("labels", [])]
        if issue.get("state") != "open" or issue.get("pull_request") or labels:
            raise DrillError("disposable issue must be open and have no labels")
        return int(repo["id"])
    finally:
        if saved is None:
            os.environ.pop("GH_TOKEN", None)
        else:
            os.environ["GH_TOKEN"] = saved


def _issue_label(key: str, number: int, *, add: bool) -> None:
    old = os.environ.get("GH_TOKEN")
    os.environ["GH_TOKEN"] = key
    try:
        path = f"/repos/{DISPOSABLE}/issues/{number}/labels"
        if add:
            github(path, method="POST", payload={"labels": ["ilmar:ready"]})
        else:
            # Delete one label only, and never expose a credential on failure.
            try:
                github(path + "/ilmar%3Aready", method="DELETE")
            except DrillError:
                pass
    finally:
        if old is None:
            os.environ.pop("GH_TOKEN", None)
        else:
            os.environ["GH_TOKEN"] = old


def _run_cli(path: Path, command: str, env: dict[str, str]) -> int:
    argv = [sys.executable, "-m", "ilmar.cli", command]
    argv.extend(["--once"] if command == "run" else [])
    argv.extend(["--config", str(path)])
    try:
        result = subprocess.run(argv, cwd=ROOT, env=env, capture_output=True,
                                timeout=1200, check=False)
    except (subprocess.TimeoutExpired, OSError):
        raise DrillError("Ilmar CLI did not finish; no retry") from None
    # Never print stdout/stderr: Claude can reflect untrusted provider content.
    secret_values = (env["ANTHROPIC_API_KEY"], env["ILMAR_GATE1_GITHUB_TOKEN"])
    if any(v.encode() in result.stdout + result.stderr for v in secret_values):
        raise DrillError("Ilmar output contained a credential; nothing uploaded")
    return int(result.returncode)


def through_ilmar(path: Path, issue: int) -> None:
    check_paid_context(os.environ)
    state = _state(path)
    if state.get("phase") != "cap-refused":
        raise DrillError("real provider cap refusal not established")
    model_key, gh_key = os.environ.get("CAP_DRILL_KEY", ""), os.environ.get("CAP_DRILL_GITHUB_TOKEN", "")
    if not model_key or not gh_key:
        raise DrillError("disposable credentials missing")
    repo_id = _repo_issue(gh_key, issue)
    work = Path(tempfile.mkdtemp(prefix="ilmar-cap-", dir=os.environ.get("RUNNER_TEMP", "/tmp")))
    work.chmod(0o700)
    config = _config(work / "gate1.toml", repo_id=repo_id, state_dir=work / "state")
    (work / "state").mkdir(mode=0o700)
    env = {k: v for k, v in os.environ.items()
           if k not in ("CAP_DRILL_KEY", "CAP_DRILL_GITHUB_TOKEN", "CAP_DRILL_ADMIN_READ",
                        "GH_TOKEN", "GITHUB_TOKEN")}
    env["ANTHROPIC_API_KEY"] = model_key
    env["ILMAR_GATE1_GITHUB_TOKEN"] = gh_key
    from ilmar.adapters.sqlite import SQLiteExecutionStore
    from ilmar.domain.execution import ExecutionState

    labeled = False
    try:
        _issue_label(gh_key, issue, add=True)
        labeled = True
        first = _run_cli(config, "run", env)
        database = work / "state/executions.sqlite3"
        with SQLiteExecutionStore.open_read_only(database) as store:
            suspension = store.suspension()
            events = store.events()
            executions = store.list_in_states(set(ExecutionState))
        if (
            suspension is None or suspension.reason.value != "provider-cap"
            or not any(e.reason == "provider-refused" for e in events)
            or not any(e.reason == "provider-cap" for e in events)
            or any(e.change_request_url for e in executions)
        ):
            raise DrillError("real coordinator did not record required provider-cap behavior")
        budget = _run_cli(config, "budget", env)
        if budget != 78:
            raise DrillError("ilmar budget did not report exit 78")
        before = [(e.id, e.attempt) for e in executions]
        second = _run_cli(config, "run", env)
        with SQLiteExecutionStore.open_read_only(database) as store:
            after = [(e.id, e.attempt) for e in store.list_in_states(set(ExecutionState))]
        if second != 78 or after != before:
            raise DrillError("suspended coordinator retried or changed execution state")
        state.update({"phase": "ilmar-verified", "outcome": "cap-enforced-through-ilmar",
                      "ilmar_first_exit": first, "budget_exit": budget,
                      "second_run_exit": second, "executions": len(before)})
        _private_json(path, state)
    finally:
        if labeled:
            _issue_label(gh_key, issue, add=False)
        # Caller deletes work on exit; no evidence path points to it.
        env.clear()


def record(path: Path, target: Path) -> None:
    """Write only a bounded allow-listed record, checked against every provided key."""
    try:
        state = _state(path)
    except (DrillError, ValueError, OSError):
        state = {}
    allow = ("phase", "outcome", "cap_cents", "spent_before_usd", "estimated_spend_usd",
             "requests", "model", "provider_status", "provider_error_type",
             "ilmar_first_exit", "budget_exit", "second_run_exit", "executions")
    result = {k: state[k] for k in allow if k in state}
    result["run"] = os.environ.get("GITHUB_RUN_ID", "")
    result["sha"] = os.environ.get("ILMAR_EXPECTED_SHA", "")
    result["workspace"] = WORKSPACE
    result.setdefault("outcome", "inconclusive")
    serialized = json.dumps(result, sort_keys=True)
    secrets = [os.environ.get(k, "") for k in
               ("CAP_DRILL_KEY", "CAP_DRILL_ADMIN_READ", "CAP_DRILL_GITHUB_TOKEN")]
    if any(secret and secret in serialized for secret in secrets) or KEY_SHAPE.search(serialized):
        raise DrillError("credential-looking value in evidence; record withheld")
    _private_json(target, result)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("phase", choices=("guard", "mark", "preflight", "exhaust",
                                         "through-ilmar", "record"))
    parser.add_argument("--state", default=os.environ.get("RUNNER_TEMP", "/tmp") + "/cap-state.json")
    parser.add_argument("--record", default=os.environ.get("RUNNER_TEMP", "/tmp") + "/cap-drill.json")
    parser.add_argument("--issue", type=int, default=0)
    args = parser.parse_args(argv)
    try:
        state = Path(args.state)
        if args.phase == "guard":
            guard(os.environ)
        elif args.phase == "mark":
            mark(os.environ)
        elif args.phase == "preflight":
            preflight(state)
        elif args.phase == "exhaust":
            exhaust(state)
        elif args.phase == "through-ilmar":
            if not 1 <= args.issue <= 1000000:
                raise DrillError("a disposable issue number is required")
            through_ilmar(state, args.issue)
        else:
            record(state, Path(args.record))
    except (DrillError, ValueError, KeyError, OSError) as error:
        # A fixed, audited error vocabulary only; no API body, key or subprocess output.
        safe = str(error)
        if KEY_SHAPE.search(safe):
            safe = "sensitive value withheld"
        print(f"cap drill stopped: {safe}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
