# SPDX-License-Identifier: Apache-2.0
"""The one-time provider cap drill through Ilmar (#93, docs/operations/cap-drill-workflow.md).

    python3 scripts/cap_drill.py guard              # guard job (no secrets)
    python3 scripts/cap_drill.py prepare DIR        # prepare job (no secrets)
    python3 scripts/cap_drill.py drill-context      # drill job, before any secret
    python3 scripts/cap_drill.py load-images TAR    # drill job, before any secret
    python3 scripts/cap_drill.py preflight          # CAP_DRILL_ADMIN_READ, CAP_DRILL_KEY; free
    python3 scripts/cap_drill.py mark               # the one-time marker on #93 (GH_TOKEN)
    python3 scripts/cap_drill.py exhaust            # CAP_DRILL_KEY
    python3 scripts/cap_drill.py through-ilmar      # CAP_DRILL_KEY, CAP_DRILL_GITHUB_TOKEN
    python3 scripts/cap_drill.py actual-cost        # CAP_DRILL_ADMIN_READ
    python3 scripts/cap_drill.py finish             # no secrets: the sanitized record
    python3 scripts/cap_drill.py post FILE          # post job: the record on #93

Each stage reads and extends one state file that never holds a secret. A
stage that stops exits 1, so GitHub skips every later stage that spends;
``finish`` always runs and writes the record.
The free preflight runs before ``mark``: a run that stops there leaves the
one-time authorization unused.

Rules this file keeps (tests/unit/test_cap_drill.py checks them):

* **No paid request before the preflight passed.** The preflight reads the
  workspace, the key's workspace (twice: the provider's response header to a
  free token count, and the Admin API's key list), the workspace's spend
  limit and its month-to-date cost. Admin API calls are GET only. If anything
  cannot be read or verified, the run stops.
* **No retries.** Every request is sent once. The first non-success ends the
  stage.
* **The allowance is a ceiling.** Before each paid request, the month-to-date
  cost read in the preflight, plus the drill's estimated spend, plus the next
  request's worst case, plus the reserve kept for Ilmar's own attempt must
  stay within the allowance (USD 1.00 at most).
* **Secrets stay in their stage.** Each stage reads only its own variables,
  redacts them from everything it keeps, and sends each key only to the
  endpoints that need it.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Iterator, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from decimal import ROUND_CEILING, Decimal
from pathlib import Path
from typing import Any

import live_ci
from live_run import RunError

ROOT = Path(__file__).resolve().parents[1]
UPSTREAM = "Obtuse8053/Ilmar"
MAINTAINER = "Obtuse8053"
DISPOSABLE = "Obtuse8053/ilmar-disposable"
ISSUE = 93
WORKFLOW_FILE = ".github/workflows/cap-drill.yml"
ENVIRONMENT = "cap-drill"
GATE1_CONFIG = ROOT / "deploy" / "gate1" / "ilmar.gate1.toml"
CLAUDE_VERSION = "2.1.318"

WORKSPACE = "ilmar-cap-test"
ALLOWANCE_CENTS = Decimal(115)
"""The test allowance, independent of the provider's limit: USD 1.15. Inputs may only
lower it. It bounds the workspace's month-to-date provider spend plus the drill's
accounting reservations (see ``exhaust``)."""
PROVIDER_LIMIT_MAX_CENTS = Decimal(100)
"""The workspace's own monthly limit must be at most USD 1.00."""
MAX_REQUESTS = 60
MAX_TOKENS = 8
SAFETY = Decimal("1.25")
"""Worst cases are estimated high: prices can change, and tokens are counted, not billed."""
MARKER = "cap-drill: started"
AUTHORIZE = re.compile(
    r"^cap-drill: authorize (?P<sha>[0-9a-f]{40}) workspace=(?P<ws>[a-z0-9-]+) "
    r"allowance=(?P<usd>\d+\.\d\d)$",
    re.MULTILINE,
)
AUTHORIZATION_URL = re.compile(
    rf"https://github\.com/{re.escape(UPSTREAM)}/issues/{ISSUE}#issuecomment-(?P<id>\d{{1,15}})"
)
AUTHORIZATION_MAX_AGE = timedelta(days=7)

KEY_ENV = "CAP_DRILL_KEY"
ADMIN_ENV = "CAP_DRILL_ADMIN_READ"
GITHUB_ENV = "CAP_DRILL_GITHUB_TOKEN"
SECRET_ENVS = (KEY_ENV, ADMIN_ENV, GITHUB_ENV)

ANTHROPIC = "https://api.anthropic.com"
ANTHROPIC_VERSION = "2023-06-01"
SPEND_BETA = "spend-limit-reads-2026-09-26"
WORKSPACES = "/v1/organizations/workspaces"
API_KEYS = "/v1/organizations/api_keys"
SPEND_LIMITS = "/v1/organizations/spend_limits"
COST_REPORT = "/v1/organizations/cost_report"
ADMIN_PATHS = frozenset({WORKSPACES, API_KEYS, SPEND_LIMITS, COST_REPORT})
"""The only Admin API paths this file calls, and only with GET."""
COUNT_TOKENS = "/v1/messages/count_tokens"  # free
MESSAGES = "/v1/messages"
MODEL_PATHS = frozenset({COUNT_TOKENS, MESSAGES})
MAX_PAGES = 20
MAX_BODY = 1 << 20
MESSAGE_CHARS = 300

READY = "ilmar:ready"
ILMAR_LABELS = ("ilmar:ready", "ilmar:claimed")
SHA = re.compile(r"[0-9a-f]{40}")
PRICES: dict[str, tuple[Decimal, Decimal]] = {
    # USD per million input and output tokens, Anthropic's published list prices
    # (R36; check against platform.claude.com/docs/en/about-claude/pricing in review).
    # One rate at every prompt length: the worst case needs no tiers.
    "claude-haiku-4-5": (Decimal(1), Decimal(5)),
    "claude-haiku-4-5-20251001": (Decimal(1), Decimal(5)),
}
"""The allow-list: the drill uses only these models, at these reviewed prices."""
CREDENTIAL_SHAPES = live_ci.CREDENTIAL_SHAPES
RESULTS = (
    "cap-enforced-through-ilmar",
    "ilmar-check-failed",
    "inconclusive",
    "cap-too-high",
    "no-headroom",
    "cap-unverified",
)

Response = tuple[int, Mapping[str, str], bytes]
"""Status, response headers (lower-case names) and body."""
Http = Callable[[str, str, Mapping[str, str], bytes | None, float], Response]
Run = Callable[..., Any]


class DrillError(Exception):
    """The drill stops here; the message is safe to keep (it holds no secret)."""


class StopError(DrillError):
    """A preflight stop, before any paid request, with its result value."""

    def __init__(self, result: str, message: str) -> None:
        super().__init__(message)
        self.result = result


# -- secrets and redaction ----------------------------------------------------------------


def secret(env: Mapping[str, str], name: str) -> str:
    value = env.get(name, "")
    if not value or value != value.strip() or len(value) < 20:
        raise DrillError(f"{name} is not set to a credential")
    return value


def redact(text: str, secrets: Sequence[str], limit: int = MESSAGE_CHARS) -> str:
    """``text`` without any of ``secrets`` or anything shaped like a credential."""
    for value in secrets:
        if value:
            text = text.replace(value, "[redacted]")
    text = CREDENTIAL_SHAPES.sub("[redacted]", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:limit]


def holds_secret(text: str, secrets: Sequence[str]) -> bool:
    return any(v and v in text for v in secrets) or bool(CREDENTIAL_SHAPES.search(text))


# -- state ---------------------------------------------------------------------------------


def load_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    data = json.loads(path.read_text())
    if not isinstance(data, dict):
        raise DrillError("the state file is not an object")
    return data


def save_state(path: Path, state: Mapping[str, Any], secrets: Sequence[str]) -> None:
    text = json.dumps(state, indent=2, sort_keys=True) + "\n"
    if holds_secret(text, secrets):
        raise DrillError("a credential reached the state; nothing was written")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


# -- HTTP ----------------------------------------------------------------------------------


class _NoRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args: Any, **kwargs: Any) -> urllib.request.Request | None:
        return None  # never follow a redirect with a credential


def urllib_http(
    method: str, url: str, headers: Mapping[str, str], body: bytes | None, timeout: float
) -> Response:
    """One request, never retried. A network failure is an error, not a status."""
    if not url.startswith(("https://api.anthropic.com/", "https://api.github.com/")):
        raise DrillError("refused: an unexpected origin")
    request = urllib.request.Request(  # noqa: S310 - the origins are checked above
        url, data=body, method=method, headers=dict(headers)
    )
    opener = urllib.request.build_opener(_NoRedirects)
    try:
        with opener.open(request, timeout=timeout) as response:
            head = {k.lower(): v for k, v in response.headers.items()}
            return int(response.status), head, response.read(MAX_BODY)
    except urllib.error.HTTPError as error:
        head = {k.lower(): v for k, v in error.headers.items()}
        return int(error.code), head, error.read(MAX_BODY)
    except (urllib.error.URLError, TimeoutError, OSError) as error:
        raise DrillError(f"{method} {urllib.parse.urlsplit(url).path}: "
                         f"{type(error).__name__}") from None  # fmt: skip


def decode(body: bytes) -> Any:
    try:
        return json.loads(body)
    except ValueError:
        return None


def error_of(body: bytes) -> tuple[str, str]:
    """The provider's error ``type`` and message, as given (redact before keeping)."""
    data = decode(body)
    error = data.get("error") if isinstance(data, dict) else None
    if not isinstance(error, dict):
        return "unknown", body[:2000].decode("utf-8", "replace")
    return str(error.get("type", "unknown")), str(error.get("message", ""))


class Admin:
    """The Admin API, read-only: GET on four paths and nothing else."""

    def __init__(self, key: str, http: Http) -> None:
        self._key = key
        self._http = http

    def get(self, path: str, params: Sequence[tuple[str, str]], *, beta: str | None = None) -> Any:
        if path not in ADMIN_PATHS:
            raise DrillError(f"refused: {path} is not a read this drill makes")
        headers = {"x-api-key": self._key, "anthropic-version": ANTHROPIC_VERSION}
        if beta:
            headers["anthropic-beta"] = beta
        url = f"{ANTHROPIC}{path}?{urllib.parse.urlencode(params)}"
        status, _, body = self._http("GET", url, headers, None, 30)
        if status != 200:
            kind, _ = error_of(body)  # the message is not kept: it may echo a request
            raise DrillError(f"GET {path}: HTTP {status} ({kind})")
        data = decode(body)
        if not isinstance(data, dict):
            raise DrillError(f"GET {path}: not a JSON object")
        return data


class Model:
    """The scratch workspace's key: token counting and tiny messages, nothing else."""

    def __init__(self, key: str, http: Http) -> None:
        self._key = key
        self._http = http

    def post(self, path: str, payload: Mapping[str, Any]) -> Response:
        if path not in MODEL_PATHS:
            raise DrillError(f"refused: {path} is not a call this drill makes")
        headers = {"x-api-key": self._key, "anthropic-version": ANTHROPIC_VERSION,
                   "content-type": "application/json"}  # fmt: skip
        return self._http("POST", f"{ANTHROPIC}{path}", headers, json.dumps(payload).encode(), 120)


# -- §3 the read-only preflight -----------------------------------------------------------


def pages(admin: Admin, path: str, params: Sequence[tuple[str, str]]) -> Iterator[Any]:
    """Every item of a list read, following ``has_more``/``last_id`` or ``next_page``."""
    extra: list[tuple[str, str]] = []
    for _ in range(MAX_PAGES):
        page = admin.get(path, [*params, *extra])
        data = page.get("data")
        if not isinstance(data, list):
            raise DrillError(f"GET {path}: no data list")
        yield from data
        if page.get("next_page"):
            extra = [("page", str(page["next_page"]))]
        elif page.get("has_more") and page.get("last_id"):
            extra = [("after_id", str(page["last_id"]))]
        else:
            return
    raise DrillError(f"GET {path}: more than {MAX_PAGES} pages")


def find_workspace(admin: Admin, name: str = WORKSPACE) -> str:
    found = [
        str(w.get("id"))
        for w in pages(admin, WORKSPACES, [("limit", "100"), ("include_archived", "false")])
        if isinstance(w, dict) and w.get("name") == name and not w.get("archived_at")
    ]
    if len(found) != 1:
        raise StopError("cap-unverified", f"found {len(found)} active workspaces named {name}")
    return found[0]


def hint_matches(hint: object, key: str) -> bool:
    """The Admin API's partially redacted hint (``sk-ant-api03-R2D...igAA``) fits ``key``."""
    if not isinstance(hint, str):
        return False
    prefix, dots, suffix = hint.partition("...")
    return (
        bool(dots)
        and prefix.startswith("sk-ant-")
        and len(suffix) >= 4
        and len(key) > len(prefix) + len(suffix)
        and key.startswith(prefix)
        and key.endswith(suffix)
    )


def key_workspace(admin: Admin, key: str) -> str:
    """The workspace of the one active key in the organization whose hint fits ``key``.

    The key itself is never sent: only compared, here, with the hints."""
    matches = [
        k
        for k in pages(admin, API_KEYS, [("limit", "100"), ("status", "active")])
        if isinstance(k, dict) and hint_matches(k.get("partial_key_hint"), key)
    ]
    if len(matches) != 1:
        raise StopError("cap-unverified", f"{len(matches)} active keys in the organization fit "
                   f"{KEY_ENV}; exactly one must")  # fmt: skip
    scope = matches[0].get("scope")
    workspace = matches[0].get("workspace_id") or (
        scope.get("workspace_id") if isinstance(scope, dict) else None
    )
    if not isinstance(workspace, str) or not workspace:
        raise StopError("cap-unverified", f"{KEY_ENV} is not bound to a workspace")
    return workspace


def workspace_limit(admin: Admin, workspace: str) -> Decimal:
    """The lowest spend limit configured for ``workspace``, in cents.

    Listing Claude Console limits is an early-access preview ([R33]); without
    it this read fails, and the drill stops."""
    try:
        page = admin.get(
            SPEND_LIMITS,
            [("scope_type", "workspace"), ("workspace_id", workspace), ("limit", "100")],
            beta=SPEND_BETA,
        )
    except DrillError as error:
        raise StopError("cap-unverified", f"the workspace's spend limit cannot be read: {error}. "
                   "Listing Console spend limits is an early-access preview") from None  # fmt: skip
    rows = [
        r
        for r in page.get("data") or []
        if isinstance(r, dict)
        and isinstance(r.get("scope"), dict)
        and r["scope"].get("type") == "workspace"
        and r["scope"].get("workspace_id") == workspace
    ]
    if page.get("next_page"):
        raise StopError("cap-unverified", "the spend-limit read was unexpectedly paged")
    if not rows:
        raise StopError("cap-unverified", f"no spend limit is set on {WORKSPACE}")
    amounts = []
    for row in rows:
        amount = row.get("amount")
        if row.get("currency") != "USD" or not isinstance(amount, str) or not amount.isdigit():
            raise StopError("cap-unverified", "a workspace limit without a USD amount in cents")
        if row.get("is_enabled") is False:
            continue
        amounts.append(Decimal(amount))
    if not any(r.get("period") == "monthly" for r in rows) or not amounts:
        raise StopError("cap-unverified", f"no enabled monthly spend limit on {WORKSPACE}")
    return min(amounts)


def month_start(now: datetime) -> datetime:
    return now.astimezone(UTC).replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def month_to_date(admin: Admin, workspace: str, now: datetime) -> Decimal:
    """The workspace's cost this UTC month, in cents, from the cost report."""
    start = month_start(now).strftime("%Y-%m-%dT%H:%M:%SZ")
    params = [("starting_at", start), ("bucket_width", "1d"), ("group_by[]", "workspace_id"),
              ("limit", "31")]  # fmt: skip
    total = Decimal(0)
    for bucket in pages(admin, COST_REPORT, params):
        for row in (bucket.get("results") or []) if isinstance(bucket, dict) else []:
            if not isinstance(row, dict) or row.get("workspace_id") != workspace:
                continue
            if row.get("currency", "USD") != "USD":
                raise StopError("cap-unverified", "the cost report is not in USD")
            try:
                total += Decimal(str(row.get("amount")))
            except ArithmeticError:
                raise StopError(
                    "cap-unverified", "the cost report has an unreadable amount"
                ) from None
    return total


WORKSPACE_HEADER = "anthropic-workspace-id"
PROBE = "Cap drill preflight."


def key_binding(model: Model, name: str) -> str:
    """The workspace the provider reports for ``CAP_DRILL_KEY``, from a free call.

    Claude API responses carry an ``anthropic-workspace-id`` header ([R35]).
    No workspace header is sent, so a key that is not bound to one workspace
    cannot be steered into the right one: it reports another, or none."""
    status, headers, body = model.post(
        COUNT_TOKENS, {"model": name, "messages": [{"role": "user", "content": PROBE}]}
    )
    if status != 200:
        kind, _ = error_of(body)
        raise StopError("cap-unverified", f"the key's workspace could not be read: HTTP "
                        f"{status} ({kind})")  # fmt: skip
    workspace = headers.get(WORKSPACE_HEADER, "")
    if not re.fullmatch(r"wrkspc_[A-Za-z0-9]{1,64}", workspace):
        raise StopError("cap-unverified", f"the provider did not report {KEY_ENV}'s workspace")
    return workspace


def preflight(
    admin: Admin, model: Model, key: str, inputs: Mapping[str, Any], now: datetime
) -> dict[str, Any]:
    """§3: everything that must be verified before the first paid request.

    Only GET requests to the Admin API and one free token-count call."""
    try:
        workspace = find_workspace(admin)
        if key_binding(model, inputs["model"]) != workspace:
            raise StopError("cap-unverified", f"{KEY_ENV} is not bound to {WORKSPACE}")
        if key_workspace(admin, key) != workspace:
            raise StopError("cap-unverified", f"the Admin API does not list {KEY_ENV} in "
                            f"{WORKSPACE}")  # fmt: skip
        limit = workspace_limit(admin, workspace)
        spent = month_to_date(admin, workspace, now)
    except StopError:
        raise
    except DrillError as error:
        raise StopError("cap-unverified", str(error)) from None
    allowance = Decimal(inputs["allowance_cents"])
    largest = Decimal(inputs["max_request_cents"])
    reserve = Decimal(inputs["ilmar_reserve_cents"])
    facts = {"workspace_id": workspace, "limit_cents": str(limit),
             "month_to_date_before_cents": str(spent), "read_at": iso(now),
             "allowance_cents": str(allowance)}  # fmt: skip
    attested = Decimal(inputs["attested_cap_usd"]) * 100
    if limit > min(PROVIDER_LIMIT_MAX_CENTS, allowance):
        raise StopError("cap-too-high", f"the workspace limit is {usd(limit)}, above "
                        f"{usd(min(PROVIDER_LIMIT_MAX_CENTS, allowance))}")  # fmt: skip
    if attested != limit:
        raise StopError("cap-unverified", f"the attested cap {usd(attested)} is not the "
                        f"{usd(limit)} limit the provider reports")  # fmt: skip
    if limit + largest + reserve > allowance:
        # The limit is reached only by a request that crosses it: provider spend can
        # reach the limit plus one request; Ilmar's reservation is accounting on top.
        raise StopError("no-headroom", f"reaching the {usd(limit)} limit can cost up to "
                        f"{usd(limit + largest + reserve)} with the last request and Ilmar's "
                        f"reserve, above the {usd(allowance)} allowance; lower the workspace "
                        f"limit to {usd(allowance - largest - reserve)} or less")  # fmt: skip
    return facts


# -- §4 reaching the cap -------------------------------------------------------------------


def cost_cents(input_tokens: int, output_tokens: int, inputs: Mapping[str, Any]) -> Decimal:
    """USD per million tokens to cents."""
    per_in, per_out = PRICES[inputs["model"]]
    usd_value = (Decimal(input_tokens) * per_in + Decimal(output_tokens) * per_out) / 10**6
    return usd_value * 100  # fmt: skip


def filler(lines: int) -> str:
    """A fixed, deterministic prompt of ``lines`` lines."""
    return "".join(f"Cap drill line {i:06d}: reply with OK.\n" for i in range(lines))


def count_tokens(model: Model, name: str, prompt: str) -> int:
    status, _, body = model.post(COUNT_TOKENS, {"model": name,
                              "messages": [{"role": "user", "content": prompt}]})  # fmt: skip
    data = decode(body)
    if status != 200 or not isinstance(data, dict) or not isinstance(data.get("input_tokens"), int):
        kind, _ = error_of(body)
        raise DrillError(f"token counting failed: HTTP {status} ({kind})")
    return int(data["input_tokens"])


def size_prompt(model: Model, inputs: Mapping[str, Any]) -> tuple[str, int, Decimal]:
    """A prompt whose worst-case request cost is at most ``max_request_cents``."""
    target = Decimal(inputs["max_request_cents"])
    lines = 100
    for _ in range(4):
        prompt = filler(lines)
        tokens = count_tokens(model, inputs["model"], prompt)
        worst = (cost_cents(tokens, MAX_TOKENS, inputs) * SAFETY).quantize(
            Decimal("0.0001"), rounding=ROUND_CEILING
        )
        if Decimal("0.5") * target <= worst <= target:
            return prompt, tokens, worst
        lines = max(1, int(lines * float(target * Decimal("0.9") / max(worst, Decimal("1e-6")))))
        lines = min(lines, 50_000)
    if worst <= target:
        return prompt, tokens, worst
    raise DrillError("no prompt fits the per-request worst case")


def exhaust(
    model: Model,
    inputs: Mapping[str, Any],
    classify: Callable[[str], str | None],
    key: str,
    month_to_date_cents: str,
    limit_cents: str,
) -> dict[str, Any]:
    """Send small paid requests, one at a time, until the provider refuses.

    While the workspace's estimated spend is below its limit, each request uses
    the sized prompt (worst case at most ``max_request_cents``). Once the
    estimate has reached the limit, only the fixed probe is sent: a tiny request
    whose refusal is the signal, and whose worst case is a fraction of a cent.
    So at a USD 1.00 limit, provider spend stays within the limit plus one
    sized request, and the refusing request needs almost no headroom.

    The ceiling mixes two kinds of figure, kept apart in the record:

    * **provider spending**: the month-to-date cost read in the preflight and
      this step's estimated cost of each sent request;
    * **accounting reservations**: the next request's worst case before it is
      sent, and the reserve held for Ilmar's own attempt. Neither is spending.

    ``classify`` is Ilmar's own provider-error classifier: it returns ``"cap"``,
    ``"auth"``, ``"rate"`` or ``None``."""
    prompt, tokens, worst = size_prompt(model, inputs)
    probe_tokens = count_tokens(model, inputs["model"], PROBE)
    probe_worst = (cost_cents(probe_tokens, MAX_TOKENS, inputs) * SAFETY).quantize(
        Decimal("0.0001"), rounding=ROUND_CEILING
    )
    before = Decimal(month_to_date_cents)
    limit = Decimal(limit_cents)
    allowance = Decimal(inputs["allowance_cents"])
    reserve = Decimal(inputs["ilmar_reserve_cents"])
    spent = Decimal(0)
    sent = probes = 0
    result: dict[str, Any] = {"prompt_tokens": tokens, "worst_case_cents": str(worst),
                              "probe_worst_case_cents": str(probe_worst)}  # fmt: skip
    while True:
        probing = before + spent >= limit
        text, size, next_worst = (
            (PROBE, probe_tokens, probe_worst) if probing else (prompt, tokens, worst)
        )
        if sent >= MAX_REQUESTS:
            result["end"] = "request-cap"
            break
        if before + spent + next_worst + reserve > allowance:
            result["end"] = "allowance"  # the next request could exceed it: never sent
            break
        status, _, body = model.post(MESSAGES, {
            "model": inputs["model"], "max_tokens": MAX_TOKENS,
            "messages": [{"role": "user", "content": text}],
        })  # fmt: skip
        sent += 1
        probes += probing
        if status == 200:
            usage = (decode(body) or {}).get("usage") or {}
            used_in = int(usage.get("input_tokens", size))
            used_out = int(usage.get("output_tokens", MAX_TOKENS))
            spent += cost_cents(used_in, used_out, inputs)
            continue
        kind, message = error_of(body)
        cause = classify(f"{status} {kind} {message}")
        result |= {"status": status, "error_type": kind,
                   "message": redact(message, [key]), "classified": cause}  # fmt: skip
        result["end"] = "spend-limit-refused" if cause == "cap" else "other-refusal"
        break
    result |= {
        "requests": sent,
        "probes": probes,
        "estimated_cents": str(spent.quantize(Decimal("0.0001"))),
        # Accounting, not spending: the reserve held back for Ilmar's attempt was
        # never sent to the provider by this step.
        "accounting_reserve_cents": str(reserve),
        "ceiling_cents": str(allowance),
    }
    return result


def ilmar_classify(text: str) -> str | None:
    from ilmar.adapters.claude_code.provider_errors import ProviderFailure, classify

    found = classify(text)
    return {ProviderFailure.CAP: "cap", ProviderFailure.AUTH: "auth",
            ProviderFailure.RATE_LIMIT: "rate", None: None}[found]  # fmt: skip


# -- §5 Ilmar's own coordinator ------------------------------------------------------------


def drill_config(
    gate1: str,
    *,
    state_dir: Path,
    repository_id: int,
    model: str,
    limit_cents: Decimal,
    reserve_cents: Decimal,
) -> str:
    """The Gate 1 configuration, changed only where the drill must differ."""
    limit_usd = str((limit_cents / 100).quantize(Decimal("0.01")))
    reserve_usd = str((reserve_cents / 100).quantize(Decimal("0.01")))
    edits = [
        ('state_dir = "/var/tmp/ilmar-gate1"', f'state_dir = "{state_dir}"'),
        ('instance_id = "gate1"', 'instance_id = "cap-drill"'),
        ("repository_id = 0 ", f"repository_id = {repository_id} "),
        ('"/var/tmp/ilmar-gate1/executions.sqlite3"', f'"{state_dir}/executions.sqlite3"'),
        ('api_key_env = "ANTHROPIC_API_KEY"', f'api_key_env = "{KEY_ENV}"'),
        ("max_turns = 60 }", f'max_turns = 1, model = "{model}" }}'),
        ("daily_usd = 5\n", f"daily_usd = {limit_usd}\n"),
        ("monthly_usd = 10\n", f"monthly_usd = {limit_usd}\n"),
        ("reserve_per_execution_usd = 2\n", f"reserve_per_execution_usd = {reserve_usd}\n"),
        ("provider_cap_attested_usd = 10 ", f"provider_cap_attested_usd = {limit_usd} "),
        ('"/var/tmp/ilmar-gate1-KILL"', f'"{state_dir}-KILL"'),
    ]
    text = gate1
    for old, new in edits:
        if text.count(old) != 1:
            raise DrillError(f"the Gate 1 configuration changed: {old!r} is not there once")
        text = text.replace(old, new)
    entry = 'token_env = "ILMAR_GATE1_GITHUB_TOKEN"'
    if text.count(entry) != 2:
        raise DrillError("the Gate 1 configuration changed: two GitHub token entries expected")
    text = text.replace(entry, f'token_env = "{GITHUB_ENV}"')
    if f'expected_version = "{CLAUDE_VERSION}"' not in text:
        raise DrillError(f"the Gate 1 configuration does not pin Claude Code {CLAUDE_VERSION}")
    return text


def github(
    http: Http, token: str, method: str, path: str, payload: Mapping[str, Any] | None = None
) -> Any:
    headers = {"Accept": "application/vnd.github+json", "Authorization": f"Bearer {token}",
               "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "ilmar-cap-drill"}  # fmt: skip
    body = json.dumps(payload).encode() if payload is not None else None
    status, _, raw = http(method, f"https://api.github.com{path}", headers, body, 30)
    if status == 404 and method == "DELETE":
        return None
    if status >= 300:
        raise DrillError(f"{method} {path}: HTTP {status}")
    return decode(raw) if raw else None


def events(run: Run, ilmar: Sequence[str], config: Path, env: Mapping[str, str]) -> list[Any]:
    """Ilmar's own event log, read-only (``ilmar events``)."""
    done = run([*ilmar, "events", "--config", str(config)], capture_output=True, text=True,
               env=dict(env), check=False)  # fmt: skip
    if done.returncode == 2:  # no store yet
        return []
    if done.returncode != 0:
        raise DrillError(f"ilmar events exited {done.returncode}")
    return [json.loads(line) for line in done.stdout.splitlines() if line.strip()]


def facts_of(log: Sequence[Mapping[str, Any]]) -> dict[str, int]:
    def count(kind: str, reason: str | None = None) -> int:
        return sum(1 for e in log if e.get("type") == kind
                   and (reason is None or e.get("reason") == reason))  # fmt: skip

    return {
        "executions": count("execution.created"),
        "attempts": count("budget.reserved"),
        "provider_refused": count("execution.transitioned", "provider-refused"),
        "provider_cap_suspensions": count("instance.suspended", "provider-cap"),
    }


def containers(run: Run) -> int:
    done = run(["podman", "ps", "--all", "--quiet", "--filter", "label=io.ilmar.managed=true"],
               capture_output=True, text=True, check=False)  # fmt: skip
    if done.returncode != 0:
        raise DrillError("podman ps failed")
    return len([line for line in done.stdout.splitlines() if line.strip()])


def through_ilmar(
    *,
    run: Run,
    http: Http,
    env: Mapping[str, str],
    inputs: Mapping[str, Any],
    state: Mapping[str, Any],
    work: Path,
) -> dict[str, Any]:
    """§5: one labelled issue, ``ilmar run --once`` twice, ``ilmar budget``; then clean up."""
    if (state.get("exhaust") or {}).get("end") != "spend-limit-refused":
        raise DrillError("refused: the cap was not reached directly, so Ilmar does not run")
    key, token = secret(env, KEY_ENV), secret(env, GITHUB_ENV)
    issue = int(inputs["issue"])
    found = github(http, token, "GET", f"/repos/{DISPOSABLE}/issues/{issue}")
    labels = {str(label.get("name")) for label in found.get("labels") or []}
    if (found.get("state") != "open" or "pull_request" in found
            or (found.get("user") or {}).get("login") != MAINTAINER
            or labels & set(ILMAR_LABELS)):  # fmt: skip
        raise DrillError(f"{DISPOSABLE}#{issue} must be your open, unlabelled issue")
    repository = github(http, token, "GET", f"/repos/{DISPOSABLE}")
    pulls = len(github(http, token, "GET", f"/repos/{DISPOSABLE}/pulls?state=all&per_page=100"))
    state_dir = work / "state"
    config = work / "ilmar.cap-drill.toml"
    config.write_text(drill_config(
        GATE1_CONFIG.read_text(), state_dir=state_dir, repository_id=int(repository["id"]),
        model=inputs["model"], limit_cents=Decimal(state["preflight"]["limit_cents"]),
        reserve_cents=Decimal(inputs["ilmar_reserve_cents"]),
    ))  # fmt: skip
    child = {name: env[name] for name in ("PATH", "HOME", "XDG_RUNTIME_DIR", "PYTHONPATH",
             "TMPDIR") if env.get(name)} | {KEY_ENV: key, GITHUB_ENV: token}  # fmt: skip
    ilmar = [sys.executable, "-m", "ilmar"]  # PYTHONPATH: the locked, checked wheels
    secrets = [key, token]

    def call(*argv: str) -> tuple[int, str]:
        done = run([*ilmar, *argv, "--config", str(config)], capture_output=True, text=True,
                   env=child, check=False, timeout=2700)  # fmt: skip
        return int(done.returncode), redact(f"{done.stdout}\n{done.stderr}", secrets, 1200)

    code, output = call("config", "validate", "--check-environment")
    if code != 0:
        raise DrillError(f"the drill configuration is invalid: {output}")
    out: dict[str, Any] = {}
    github(http, token, "POST", f"/repos/{DISPOSABLE}/issues/{issue}/labels", {"labels": [READY]})
    try:
        out["run1_exit"], out["run1_output"] = call("run", "--once")
        first = facts_of(events(run, ilmar, config, child))
        out["budget_exit"], out["budget_output"] = call("budget")
        before = containers(run)
        out["run2_exit"], out["run2_output"] = call("run", "--once")
        second = facts_of(events(run, ilmar, config, child))
        after = containers(run)
    finally:
        for label in ILMAR_LABELS:
            github(http, token, "DELETE", f"/repos/{DISPOSABLE}/issues/{issue}/labels/{label}")
    pulls_after = len(
        github(http, token, "GET", f"/repos/{DISPOSABLE}/pulls?state=all&per_page=100")
    )
    run_url = (f"{env.get('GITHUB_SERVER_URL', 'https://github.com')}/{UPSTREAM}"
               f"/actions/runs/{env.get('GITHUB_RUN_ID', '')}")  # fmt: skip
    github(http, token, "POST", f"/repos/{DISPOSABLE}/issues/{issue}/comments",
           {"body": f"Ilmar cap drill: {run_url}"})  # fmt: skip
    github(http, token, "PATCH", f"/repos/{DISPOSABLE}/issues/{issue}", {"state": "closed"})
    out |= {"after_run1": first, "after_run2": second, "containers_before_run2": before,
            "containers_after_run2": after,
            "change_requests_opened": pulls_after - pulls}  # fmt: skip
    out["failed_checks"] = ilmar_checks(out)
    return out


def ilmar_checks(out: Mapping[str, Any]) -> list[str]:
    """What §5 requires; an empty list means every check held."""
    first, second = out["after_run1"], out["after_run2"]
    problems = []
    if first["executions"] != 1 or first["attempts"] != 1:
        problems.append("the first pass did not start exactly one execution and one attempt")
    if first["provider_refused"] != 1:
        problems.append("the execution did not end provider-refused")
    if first["provider_cap_suspensions"] != 1:
        problems.append("Ilmar did not suspend with provider-cap")
    if out["budget_exit"] != 78 or "provider-cap" not in out["budget_output"]:
        problems.append("ilmar budget did not exit 78 with provider-cap")
    if out["run2_exit"] != 78:
        problems.append("the second pass did not exit 78")
    if second != first:
        problems.append("the second pass changed the executions, attempts or suspensions")
    if out["containers_after_run2"] != out["containers_before_run2"]:
        problems.append("the second pass started a container")
    if out["change_requests_opened"] != 0:
        problems.append("a change request was opened")
    return problems


# -- §6 and §7: costs and the record --------------------------------------------------------


def actual_cost(admin: Admin, state: Mapping[str, Any], now: datetime) -> dict[str, Any]:
    pre = state["preflight"]
    after = month_to_date(admin, pre["workspace_id"], now)
    delta = after - Decimal(pre["month_to_date_before_cents"])
    estimated = Decimal((state.get("exhaust") or {}).get("estimated_cents", "0"))
    pending = delta <= 0 < estimated  # the cost report has not caught up yet
    allowance = Decimal(pre["allowance_cents"])
    if pending:
        verified = "pending"
    elif Decimal(pre["month_to_date_before_cents"]) + delta > allowance:
        verified = "overshoot"
    else:
        verified = "within-allowance"
    return {"read_at": iso(now), "actual_cents": "pending" if pending else str(delta),
            "month_to_date_after_cents": str(after), "spend": verified,
            "above_provider_limit": None if pending
            else after > Decimal(pre["limit_cents"])}  # fmt: skip


def record(
    state: Mapping[str, Any], inputs: Mapping[str, Any], env: Mapping[str, str]
) -> dict[str, Any]:
    """The sanitized record: no IDs of the organization, no keys, no request bodies."""
    pre = state.get("preflight") or {}
    ex = state.get("exhaust") or {}
    ilmar = state.get("ilmar") or {}
    if state.get("stop"):
        result = state["stop"]["result"]
    elif ex.get("end") != "spend-limit-refused":
        result = "inconclusive"
    elif ilmar.get("failed_checks") == [] and (state.get("cost") or {}).get("spend") == (
        "within-allowance"
    ):
        result = "cap-enforced-through-ilmar"
    elif ilmar.get("failed_checks") == []:
        # Ilmar behaved, but without trusted billing data within the allowance the
        # run is never reported as a verified drill (§7).
        result = "inconclusive"
    else:
        result = "ilmar-check-failed" if ilmar else "inconclusive"
    assert result in RESULTS  # noqa: S101 - an internal invariant
    return {
        "drill": "provider-cap-through-ilmar",
        "run": f"https://github.com/{UPSTREAM}/actions/runs/{env.get('GITHUB_RUN_ID', '')}",
        "expected_sha": inputs.get("expected_sha"),
        "workspace": WORKSPACE,
        "allowance_usd": usd(Decimal(inputs.get("allowance_cents", ALLOWANCE_CENTS))),
        "provider_limit_max_usd": usd(PROVIDER_LIMIT_MAX_CENTS),
        "limit_read_usd": usd(Decimal(pre["limit_cents"])) if pre.get("limit_cents") else None,
        "month_to_date_before_usd": usd(Decimal(pre["month_to_date_before_cents"]))
        if pre.get("month_to_date_before_cents") else None,
        "read_at": pre.get("read_at"),
        "model": inputs.get("model"),
        "prices_usd_per_mtok": [str(p) for p in PRICES.get(str(inputs.get("model")), ())],
        "requests": ex.get("requests"),
        "provider_spend": {
            "estimated_usd": usd(Decimal(ex["estimated_cents"])) if ex.get("estimated_cents")
            else None,
            "actual": (state.get("cost") or {}),
        },
        "accounting_reservations": {
            "note": "held back in the drill's ceiling and Ilmar's ledger; not provider spend",
            "drill_reserve_for_ilmar_usd": usd(Decimal(ex["accounting_reserve_cents"]))
            if ex.get("accounting_reserve_cents") else None,
            "ilmar_ledger_reservations": (ilmar.get("after_run2") or {}).get("attempts"),
            "ilmar_reserve_per_attempt_usd": usd(Decimal(inputs["ilmar_reserve_cents"]))
            if inputs.get("ilmar_reserve_cents") else None,
        },
        "provider_final": {k: ex.get(k) for k in ("status", "error_type", "message",
                                                    "classified", "end")},
        "ilmar": dict(ilmar),
        "stop": (state.get("stop") or {}).get("message"),
        "result": result,
    }  # fmt: skip


def usd(cents: Decimal) -> str:
    return f"${(cents / 100).quantize(Decimal('0.0001'))}"


def iso(now: datetime) -> str:
    return now.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


# -- inputs, the guard, the marker ---------------------------------------------------------


def read_inputs(env: Mapping[str, str]) -> dict[str, Any]:
    """The dispatch inputs, from the environment (never expanded in a shell)."""
    problems = []
    inputs: dict[str, Any] = {
        "expected_sha": env.get("CAP_DRILL_EXPECTED_SHA", ""),
        "authorization": env.get("CAP_DRILL_AUTHORIZATION", ""),
        "model": env.get("CAP_DRILL_MODEL", ""),
    }
    if not SHA.fullmatch(inputs["expected_sha"]):
        problems.append("expected_sha must be a full commit SHA")
    if not AUTHORIZATION_URL.fullmatch(inputs["authorization"]):
        problems.append(f"authorization must be a comment URL on #{ISSUE}")
    if inputs["model"] not in PRICES:
        problems.append(f"model must be one of {', '.join(sorted(PRICES))}")
    numbers = {"attested_cap_usd": ("0.01", "1.00"),
               "allowance_cents": ("1", str(ALLOWANCE_CENTS)), "max_request_cents": ("0.1", "10"),
               "ilmar_reserve_cents": ("1", "50"), "issue": ("1", "100000")}  # fmt: skip
    defaults = {"allowance_cents": str(ALLOWANCE_CENTS), "max_request_cents": "5",
                "ilmar_reserve_cents": "10"}  # fmt: skip
    for name, (low, high) in numbers.items():
        raw = env.get(f"CAP_DRILL_{name.upper()}", "") or defaults.get(name, "")
        try:
            value = Decimal(raw)
        except ArithmeticError:
            problems.append(f"{name} must be a number")
            continue
        if not (Decimal(low) <= value <= Decimal(high)) or not value.is_finite():
            problems.append(f"{name} must be between {low} and {high}")
        inputs[name] = raw
    if problems:
        raise DrillError("; ".join(problems))
    return inputs


def check_context(env: Mapping[str, str], inputs: Mapping[str, Any]) -> list[str]:
    problems = []
    if env.get("GITHUB_ACTIONS") != "true":
        problems.append("not running in GitHub Actions")
    if env.get("GITHUB_EVENT_NAME") != "workflow_dispatch":
        problems.append("not started by workflow_dispatch")
    if env.get("GITHUB_REF") != "refs/heads/main":
        problems.append("not running on refs/heads/main")
    if env.get("GITHUB_REPOSITORY") != UPSTREAM:
        problems.append(f"not running in {UPSTREAM}")
    if env.get("GITHUB_SHA") != inputs["expected_sha"]:
        problems.append("the workflow is not running the expected commit")
    if env.get("GITHUB_WORKFLOW_SHA") != inputs["expected_sha"]:
        problems.append("the workflow file is not from the expected commit")
    if env.get("GITHUB_WORKFLOW_REF") != f"{UPSTREAM}/{WORKFLOW_FILE}@refs/heads/main":
        problems.append(f"this is not {WORKFLOW_FILE} on main")
    return problems


def comments(get: Callable[[str, str], Any], token: str) -> list[Any]:
    found: list[Any] = []
    for page in range(1, MAX_PAGES + 1):
        batch = get(f"/repos/{UPSTREAM}/issues/{ISSUE}/comments?per_page=100&page={page}", token)
        if not isinstance(batch, list):
            raise DrillError(f"the comments on #{ISSUE} could not be read")
        found += batch
        if len(batch) < 100:
            return found
    raise DrillError(f"too many comments on #{ISSUE}")


def check_authorization(
    get: Callable[[str, str], Any], token: str, inputs: Mapping[str, Any], now: datetime
) -> list[str]:
    match = AUTHORIZATION_URL.fullmatch(inputs["authorization"])
    if match is None:
        return ["the authorization is not a comment URL on #93"]
    comment = get(f"/repos/{UPSTREAM}/issues/comments/{match['id']}", token)
    problems = []
    if not isinstance(comment, dict):
        return ["the authorization comment could not be read"]
    if not str(comment.get("issue_url", "")).endswith(f"/repos/{UPSTREAM}/issues/{ISSUE}"):
        problems.append(f"the authorization is not on #{ISSUE}")
    if (comment.get("user") or {}).get("login") != MAINTAINER or comment.get(
        "author_association"
    ) != "OWNER":
        problems.append("the authorization is not the maintainer's")
    if comment.get("updated_at") != comment.get("created_at"):
        problems.append("the authorization was edited")
    try:
        created = datetime.fromisoformat(str(comment.get("created_at")).replace("Z", "+00:00"))
    except ValueError:
        created = datetime.min.replace(tzinfo=UTC)
    if not timedelta(0) <= now - created <= AUTHORIZATION_MAX_AGE:
        problems.append("the authorization is older than seven days")
    said = AUTHORIZE.search(str(comment.get("body", "")))
    allowance = usd(Decimal(inputs["allowance_cents"])).removeprefix("$")
    if (
        said is None
        or said["sha"] != inputs["expected_sha"]
        or said["ws"] != WORKSPACE
        or (Decimal(said["usd"]) != Decimal(allowance))
    ):
        problems.append(f"the authorization must say 'cap-drill: authorize <expected_sha> "
                        f"workspace={WORKSPACE} allowance={Decimal(allowance):.2f}'")  # fmt: skip
    return problems


def check_once(get: Callable[[str, str], Any], token: str, env: Mapping[str, str]) -> list[str]:
    """Refused if a marker is on #93, or any earlier run started a paid step.

    The free preflight does not use the authorization: a run that stopped
    there (for example without spend-limit read access) may be dispatched
    again. The marker is posted after the preflight and before any paid step."""
    problems = []
    if any(str(c.get("body", "")).startswith(MARKER) for c in comments(get, token)
           if isinstance(c, dict)):  # fmt: skip
        problems.append(f"a '{MARKER}' marker is already on #{ISSUE}: the drill ran before")
    runs = get(f"/repos/{UPSTREAM}/actions/workflows/cap-drill.yml/runs?per_page=100", token)
    for run in (runs or {}).get("workflow_runs") or []:
        if str(run.get("id")) == env.get("GITHUB_RUN_ID"):
            continue
        jobs = get(f"/repos/{UPSTREAM}/actions/runs/{run.get('id')}/jobs?per_page=100", token)
        for job in (jobs or {}).get("jobs") or []:
            for step in job.get("steps") or []:
                if str(step.get("name", "")).startswith(PAID_STEPS) and step.get(
                    "conclusion"
                ) not in (None, "skipped"):
                    problems.append(f"run {run.get('id')} already started a paid step")
    return problems


def guard(env: Mapping[str, str], get: Callable[[str, str], Any], now: datetime) -> list[str]:
    try:
        inputs = read_inputs(env)
    except DrillError as error:
        return [str(error)]
    token = env.get("GH_TOKEN", "")
    problems = check_context(env, inputs)
    if not token:
        return [*problems, "GH_TOKEN is needed"]
    try:
        environment = get(f"/repos/{UPSTREAM}/environments/{ENVIRONMENT}", token)
        policies = get(
            f"/repos/{UPSTREAM}/environments/{ENVIRONMENT}/deployment-branch-policies", token
        )
        rules = get(f"/repos/{UPSTREAM}/rules/branches/main", token)
        problems += live_ci.check_environment(environment, policies)
        problems += live_ci.check_main_rules(rules if isinstance(rules, list) else [])
        problems += live_ci.check_main_head({**env, "ILMAR_EXPECTED_SHA": inputs["expected_sha"],
                                             "GITHUB_REPOSITORY": UPSTREAM}, get)  # fmt: skip
        problems += check_authorization(get, token, inputs, now)
        problems += check_once(get, token, env)
    except (RunError, DrillError) as error:
        problems.append(f"cannot read GitHub: {error}")
    return problems


PAID_STEPS = ("Mark", "Reach the cap", "Ilmar's coordinator")
"""Workflow steps that use the authorization: the marker, then everything that spends."""


def run_url(env: Mapping[str, str]) -> str:
    return f"https://github.com/{UPSTREAM}/actions/runs/{env.get('GITHUB_RUN_ID', '')}"


def mark(env: Mapping[str, str], http: Http, get: Callable[[str, str], Any]) -> None:
    """Use the authorization: only after a passed preflight, and only if still unused."""
    token = env.get("GH_TOKEN", "")
    if problems := check_once(get, token, env):
        raise DrillError("; ".join(problems))
    github(http, token, "POST", f"/repos/{UPSTREAM}/issues/{ISSUE}/comments",
           {"body": f"{MARKER} {run_url(env)}"})  # fmt: skip
    markers = [c.get("body") for c in comments(get, token)
               if isinstance(c, dict) and str(c.get("body", "")).startswith(MARKER)]  # fmt: skip
    if markers != [f"{MARKER} {run_url(env)}"]:
        raise DrillError(f"another '{MARKER}' marker is on #{ISSUE}: refusing to spend")


# -- the command line ----------------------------------------------------------------------


def main(
    argv: Sequence[str] | None = None,
    *,
    environ: Mapping[str, str] | None = None,
    http: Http = urllib_http,
    run: Run = subprocess.run,
    get: Callable[[str, str], Any] = live_ci.github_get,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").split("\n\n")[0])
    parser.add_argument("stage", choices=("guard", "mark", "prepare", "drill-context",
                        "load-images", "preflight", "exhaust", "through-ilmar", "actual-cost",
                        "finish", "post"))  # fmt: skip
    parser.add_argument("path", nargs="?")
    parser.add_argument("--state", default=None)
    args = parser.parse_args(argv)
    env = dict(os.environ if environ is None else environ)
    state_path = Path(args.state or Path(env.get("RUNNER_TEMP", ".")) / "cap-drill" / "state.json")
    secrets = [env.get(name, "") for name in SECRET_ENVS]
    try:
        if args.stage == "guard":
            if problems := guard(env, get, now()):
                for problem in problems:
                    print(f"refused: {problem}", file=sys.stderr)
                return 1
            print("Authorized, once, for this commit; the environment is protected.")
            return 0
        if args.stage == "prepare":
            outputs = live_ci.prepare(Path(args.path), {**env, "ILMAR_TEST_CLAUDE_VERSION":
                                      CLAUDE_VERSION}, run)  # fmt: skip
            live_ci.write_outputs(env, outputs)
            return 0
        inputs = read_inputs(env)
        if args.stage == "drill-context":
            problems = check_context(env, inputs)
            problems += live_ci.check_main_head({**env, "ILMAR_EXPECTED_SHA":
                                                 inputs["expected_sha"]}, get)  # fmt: skip
            if live_ci.checked_out_commit(run=run) != inputs["expected_sha"]:
                problems.append("the checked-out tree is not the expected commit")
            if problems:
                raise DrillError("; ".join(problems))
            print("Running the authorized commit.")
            return 0
        if args.stage == "load-images":
            live_ci.load_images(Path(args.path), {**env, "ILMAR_TEST_CLAUDE_VERSION":
                                CLAUDE_VERSION}, run)  # fmt: skip
            return 0
        state = load_state(state_path)
        if args.stage == "preflight":
            admin = Admin(secret(env, ADMIN_ENV), http)
            try:
                key = secret(env, KEY_ENV)
                state["preflight"] = preflight(admin, Model(key, http), key, inputs, now())
            except StopError as stop:
                state["stop"] = {"result": stop.result, "message": redact(str(stop), secrets)}
                save_state(state_path, state, secrets)
                print(f"stopped before any paid request ({stop.result}): {stop}", file=sys.stderr)
                return 1
            save_state(state_path, state, secrets)
            pre = state["preflight"]
            print(f"Verified: {KEY_ENV} belongs to {WORKSPACE}; its limit is "
                  f"{usd(Decimal(pre['limit_cents']))}; spent this month "
                  f"{usd(Decimal(pre['month_to_date_before_cents']))}.")  # fmt: skip
            return 0
        if args.stage == "mark":
            if "preflight" not in state or "stop" in state:
                raise DrillError("refused: the preflight did not pass; the authorization "
                                 "stays unused")  # fmt: skip
            mark(env, http, get)
            state["marked"] = run_url(env)
            save_state(state_path, state, secrets)
            print(f"Marked #{ISSUE}: this authorization is now used.")
            return 0
        if args.stage == "exhaust":
            if "preflight" not in state or "stop" in state:
                raise DrillError("refused: the preflight did not pass")
            if state.get("marked") != run_url(env):
                raise DrillError("refused: this run does not hold the one-time marker")
            key = secret(env, KEY_ENV)
            before = state["preflight"]["month_to_date_before_cents"]
            limit = state["preflight"]["limit_cents"]
            state["exhaust"] = exhaust(Model(key, http), inputs, ilmar_classify, key, before, limit)
            save_state(state_path, state, secrets)
            print(f"{state['exhaust']['requests']} requests; end: {state['exhaust']['end']}.")
            return 0 if state["exhaust"]["end"] == "spend-limit-refused" else 1
        if args.stage == "through-ilmar":
            work = state_path.parent / "ilmar"
            work.mkdir(parents=True, exist_ok=True)
            try:
                state["ilmar"] = through_ilmar(run=run, http=http, env=env, inputs=inputs,
                                               state=state, work=work)  # fmt: skip
            finally:
                save_state(state_path, state, secrets)
            failed = state["ilmar"]["failed_checks"]
            for problem in failed:
                print(f"check failed: {problem}", file=sys.stderr)
            return 1 if failed else 0
        if args.stage == "actual-cost":
            if "preflight" not in state:
                raise DrillError("nothing to compare: the preflight did not pass")
            state["cost"] = actual_cost(Admin(secret(env, ADMIN_ENV), http), state, now())
            save_state(state_path, state, secrets)
            return 0
        if args.stage == "finish":
            text = json.dumps(record(state, inputs, env), indent=2, sort_keys=True) + "\n"
            if holds_secret(text, secrets):
                raise DrillError("a credential reached the record; nothing was written")
            target = Path(args.path or state_path.parent / "cap-drill-record.json")
            target.write_text(text)
            print(text)
            return 0
        if args.stage == "post":
            text = Path(args.path).read_text()
            data = json.loads(text)
            if (holds_secret(text, secrets) or len(text) > live_ci.MAX_RECORD_BYTES
                    or data.get("expected_sha") != env.get("GITHUB_SHA")
                    or data.get("result") not in RESULTS):  # fmt: skip
                raise DrillError("the record is not a checked record of this commit")
            github(http, env["GH_TOKEN"], "POST", f"/repos/{UPSTREAM}/issues/{ISSUE}/comments",
                   {"body": f"Cap drill record ({data['result']}):\n\n"
                            f"```json\n{text}```\n"})  # fmt: skip
            return 0
    except (DrillError, RunError) as error:
        print(f"error: {redact(str(error), secrets, 600)}", file=sys.stderr)
        return 1
    raise AssertionError(args.stage)  # pragma: no cover


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
