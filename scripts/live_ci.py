# SPDX-License-Identifier: Apache-2.0
"""Run #44 and the live end-to-end tests (#11d) inside the manual ``live`` workflow.

    python scripts/live_ci.py            # in the live job
    python scripts/live_ci.py --post DIR # in the post job: records only

The GitHub Actions counterpart of ``scripts/live_run.py``. It runs the same
stages, record checks and posting. The only difference is where the
credentials come from: the protected ``live`` environment's secrets, which
Actions puts in this step's environment and masks in its logs. They are never
prompted for, printed or written.

It refuses to run outside GitHub Actions. Locally, ``scripts/live_run.py``
with its terminal-only prompt is the way.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from live_run import (
    ANTHROPIC_KEY_ENV,
    GITHUB_TOKEN_ENV,
    RunError,
    check_prerequisites,
    new_records,
    post_records,
    run_stages,
    test_environment,
    validate_credentials,
)

VERSION = re.compile(r"\d+\.\d+\.\d+")
SHA = re.compile(r"[0-9a-f]{40}")
ROOT = Path(__file__).resolve().parents[1]
API = "https://api.github.com"
ENVIRONMENT = "live"
PROXY_IMAGE = (
    "docker.io/library/python:3.12-alpine"
    "@sha256:213242b537a1c166a0c53e903461fc18d5518e7196c93dbecda452660a848918"
)
"""Pinned by digest: it runs the egress proxy and the verification commands."""
SUMMARY = "run-summary.json"
RECORDS = re.compile(
    r"(issue-44-smoke-test|preflight|end-to-end-[a-z0-9-]+|crash-drill-[a-z0-9-]+|run-summary)\.json"
)
CREDENTIAL_SHAPES = re.compile(r"github_pat_[A-Za-z0-9_]{20,}|gh[pousr]_[A-Za-z0-9]{30,}"
                               r"|sk-ant-[A-Za-z0-9_-]{20,}")  # fmt: skip
MAX_RECORD_BYTES = 64 * 1024


# -- the dispatch context and the protected environment (fail closed) -------------------


def check_context(env: Mapping[str, str]) -> list[str]:
    """Started by hand, on main, at exactly the commit the maintainer named."""
    problems = []
    if env.get("GITHUB_ACTIONS") != "true":
        problems.append("not running in GitHub Actions")
    if env.get("GITHUB_EVENT_NAME") != "workflow_dispatch":
        problems.append("not started by workflow_dispatch")
    if env.get("GITHUB_REF") != "refs/heads/main":
        problems.append("not running on refs/heads/main")
    expected = env.get("ILMAR_EXPECTED_SHA", "")
    if not SHA.fullmatch(expected):
        problems.append("expected_sha must be the full 40-character commit")
    elif env.get("GITHUB_SHA") != expected:
        problems.append("the workflow is not running the expected commit")
    if not VERSION.fullmatch(env.get("ILMAR_TEST_CLAUDE_VERSION", "")):
        problems.append("claude_version must be x.y.z")
    return problems


def check_environment(environment: Mapping[str, Any], policies: Mapping[str, Any]) -> list[str]:
    """The `live` environment must be set up exactly as docs/operations/live-testing.md says.

    GitHub releases environment secrets to any job its rules allow, so the
    rules are the boundary: required reviewers, main only, no admin bypass.
    """
    problems = []
    rules = environment.get("protection_rules") or []
    reviewers = [r for r in rules if r.get("type") == "required_reviewers"]
    if not reviewers or not any(r.get("reviewers") for r in reviewers):
        problems.append("the environment has no required reviewers")
    if environment.get("can_admins_bypass") is not False:
        problems.append("administrators can bypass the environment's rules")
    branch = environment.get("deployment_branch_policy") or {}
    if branch != {"protected_branches": False, "custom_branch_policies": True}:
        problems.append("deployments are not limited to selected branches")
    allowed = [(p.get("name"), p.get("type", "branch")) for p in
               policies.get("branch_policies") or []]  # fmt: skip
    if allowed != [("main", "branch")]:
        problems.append(f"the allowed branches must be exactly main, not {allowed}")
    return problems


MAIN_RULES = frozenset({"pull_request", "non_fast_forward", "deletion"})


def check_main_rules(rules: Sequence[Mapping[str, Any]]) -> list[str]:
    """`main` changes only through pull requests: a workflow pushed there directly
    would receive the secrets legitimately. Read from the branch's rulesets."""
    present = {str(rule.get("type")) for rule in rules}
    if missing := sorted(MAIN_RULES - present):
        return [f"main lacks the ruleset rules {', '.join(missing)} "
                "(require a pull request; block force pushes and deletion)"]  # fmt: skip
    return []


class _NoRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args: Any, **kwargs: Any) -> urllib.request.Request | None:
        return None  # a redirect is an error here, never followed with the token


def github_get(path: str, token: str) -> Any:
    request = urllib.request.Request(  # noqa: S310 - fixed https origin
        API + path,
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "ilmar-live-guard",
        },
    )
    opener = urllib.request.build_opener(_NoRedirects)
    try:
        with opener.open(request, timeout=30) as response:
            return json.loads(response.read())
    except urllib.error.HTTPError as error:
        raise RunError(f"GET {path}: HTTP {error.code}") from None
    except (urllib.error.URLError, TimeoutError) as error:
        raise RunError(f"GET {path}: {type(error).__name__}") from None


def guard(env: Mapping[str, str], get: Callable[[str, str], Any] = github_get) -> list[str]:
    """Everything that must hold before a job may ask for the secrets."""
    problems = check_context(env)
    repository = env.get("GITHUB_REPOSITORY", "")
    token = env.get("GH_TOKEN", "")
    if not re.fullmatch(r"[A-Za-z0-9-]+/[A-Za-z0-9._-]+", repository) or not token:
        return [*problems, "GITHUB_REPOSITORY and GH_TOKEN are needed to read the environment"]
    try:
        environment = get(f"/repos/{repository}/environments/{ENVIRONMENT}", token)
        policies = get(
            f"/repos/{repository}/environments/{ENVIRONMENT}/deployment-branch-policies", token
        )
        rules = get(f"/repos/{repository}/rules/branches/main", token)
    except RunError as error:
        return [*problems, f"cannot read the '{ENVIRONMENT}' environment or main's rules: {error}"]
    if not isinstance(rules, list):
        return [*problems, "main's rules could not be read"]
    return [*problems, *check_environment(environment, policies), *check_main_rules(rules)]


def checked_out_commit(root: Path = ROOT, run: Callable[..., Any] = subprocess.run) -> str:
    done = run(["git", "-C", str(root), "rev-parse", "HEAD"], capture_output=True, text=True,
               check=False, env={"PATH": os.environ.get("PATH", "/usr/bin:/bin")})  # fmt: skip
    return str(done.stdout).strip()


# -- records ------------------------------------------------------------------------------


def write_summary(reports: Path, summary: Mapping[str, Any], secrets: Sequence[str]) -> Path:
    """The run's outcome, bound to its commit; posting requires it to say "passed"."""
    text = json.dumps(summary, indent=2, sort_keys=True) + "\n"
    if any(value and value in text for value in secrets) or CREDENTIAL_SHAPES.search(text):
        raise RunError("a credential reached the run summary; nothing was written")
    reports.mkdir(parents=True, exist_ok=True)
    path = reports / SUMMARY
    path.write_text(text)
    return path


def checked_records(directory: Path, commit: str) -> list[Path]:
    """The records the post job may publish, or RunError: never anything unexpected."""
    records = sorted(directory.glob("*"))
    if not records:
        raise RunError(f"no records in {directory}")
    for record in records:
        if not record.is_file() or record.is_symlink() or not RECORDS.fullmatch(record.name):
            raise RunError(f"unexpected file in the records: {record.name}")
        data = record.read_bytes()
        if len(data) > MAX_RECORD_BYTES:
            raise RunError(f"{record.name} is too large")
        if CREDENTIAL_SHAPES.search(data.decode("utf-8", "replace")):
            raise RunError(f"{record.name} looks like it holds a credential; nothing was posted")
        json.loads(data)
    summary = (
        json.loads((directory / SUMMARY).read_text()) if (directory / SUMMARY).exists() else {}
    )
    if summary.get("outcome") != "passed":
        raise RunError("the run did not pass; its records are not posted")
    if summary.get("commit") != commit:
        raise RunError("the records are not from this commit")
    return records


def ci_settings(env: Mapping[str, str]) -> dict[str, str]:
    """The non-secret settings, from the workflow's environment."""
    if env.get("GITHUB_ACTIONS") != "true":
        raise RunError("live_ci.py runs only in GitHub Actions; locally use scripts/live_run.py")
    owner = env.get("GITHUB_REPOSITORY_OWNER", "")
    version = env.get("ILMAR_TEST_CLAUDE_VERSION", "")
    if not re.fullmatch(r"[A-Za-z0-9-]+", owner):
        raise RunError("GITHUB_REPOSITORY_OWNER is not set")
    if not VERSION.fullmatch(version):
        raise RunError("ILMAR_TEST_CLAUDE_VERSION must be a pinned x.y.z version")
    temp = env.get("RUNNER_TEMP", "")
    if not temp:
        raise RunError("RUNNER_TEMP is not set")
    return {
        "ILMAR_LIVE_REPO": f"{owner}/ilmar-disposable",
        "ILMAR_LIVE_OUTSIDE_REPO": f"{owner}/ilmar-outside-canary",
        "ILMAR_TEST_CLAUDE_VERSION": version,
        "ILMAR_TEST_CLAUDE_IMAGE": f"localhost/ilmar-claude-code:{version}",
        "ILMAR_TEST_PODMAN_PROXY_IMAGE": PROXY_IMAGE,
        "ILMAR_LIVE_REPORT_DIR": str(Path(temp) / "live-reports"),
    }


def ci_credentials(env: Mapping[str, str]) -> dict[str, str]:
    missing = [name for name in (GITHUB_TOKEN_ENV, ANTHROPIC_KEY_ENV) if not env.get(name)]
    if missing:
        raise RunError(
            f"secrets not set in the 'live' environment: {', '.join(missing)} "
            "(Settings → Environments → live → Environment secrets)"
        )
    return validate_credentials(env[GITHUB_TOKEN_ENV].strip(), env[ANTHROPIC_KEY_ENV].strip())


def cleanup(env: Mapping[str, str]) -> list[str]:
    """After a failure or cancellation: close and delete what live runs left in the
    disposable repository. Only `ilmar-live/*` branches, `ilmar-live:*` labels,
    their issues and the pull requests into those branches; only in a repository
    the guard accepts (name, privacy, topic)."""
    sys.path.insert(0, str(ROOT / "tests"))
    import live_harness as h
    from ilmar.adapters.github.client import urllib_transport

    settings = ci_settings(env)
    repo = settings["ILMAR_LIVE_REPO"]
    github = h.LiveGitHub(h.Secret(GITHUB_TOKEN_ENV, env[GITHUB_TOKEN_ENV]), urllib_transport)
    data = github.ok("GET", f"/repos/{repo}")
    if (
        data.get("name") != h.REPOSITORY_NAME
        or data.get("private") is not True
        or (h.REPOSITORY_TOPIC not in (data.get("topics") or []))
    ):
        raise RunError(f"{repo} is not the disposable repository; nothing was cleaned")
    done: list[str] = []
    refs = github.ok("GET", f"/repos/{repo}/git/matching-refs/heads/ilmar-live/") or []
    labels = github.ok("GET", f"/repos/{repo}/labels?per_page=100") or []
    for label in [x["name"] for x in labels if str(x.get("name", "")).startswith("ilmar-live:")]:
        quoted = urllib.parse.quote(label, safe="")
        issues = github.ok("GET", f"/repos/{repo}/issues?state=open&labels={quoted}") or []
        for issue in issues:
            github.call("PATCH", f"/repos/{repo}/issues/{issue['number']}", {"state": "closed"})
        github.call("DELETE", f"/repos/{repo}/labels/{quoted}")
        done.append(f"label {label} ({len(issues)} issues closed)")
    for ref in refs:
        base = str(ref["ref"]).removeprefix("refs/heads/")
        for request in h.change_requests_for(github, repo, base):
            if request.get("state") == "open":
                github.call("PATCH", f"/repos/{repo}/pulls/{request['number']}",
                            {"state": "closed"})  # fmt: skip
            head = request.get("head", {}).get("ref", "")
            if head.startswith("ilmar/"):
                github.call("DELETE", f"/repos/{repo}/git/refs/heads/{head}")
        github.call("DELETE", f"/repos/{repo}/git/refs/heads/{base}")
        done.append(f"branch {base}")
    return done


def main(
    argv: Sequence[str] | None = None,
    *,
    environ: Mapping[str, str] | None = None,
    run: Callable[..., Any] = subprocess.run,
    get: Callable[[str, str], Any] = github_get,
) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--guard", action="store_true", help="check the context and environment")
    mode.add_argument(
        "--guard-context", action="store_true", help="check the context and the checked-out commit"
    )
    mode.add_argument("--cleanup", action="store_true", help="remove leftovers of live runs")
    mode.add_argument("--post", metavar="DIR", help="post the records in DIR on #44 and #11")
    args = parser.parse_args(argv)
    source = dict(os.environ if environ is None else environ)
    try:
        if args.guard:
            if problems := guard(source, get):
                for problem in problems:
                    print(f"refused: {problem}", file=sys.stderr)
                print("See docs/operations/live-testing.md, 'GitHub settings'.", file=sys.stderr)
                return 1
            print("The context and the 'live' environment are as required.")
            return 0
        if problems := check_context(source):
            raise RunError("; ".join(problems))
        if args.guard_context:
            if checked_out_commit(run=run) != source["ILMAR_EXPECTED_SHA"]:
                raise RunError("the checked-out tree is not the expected commit")
            print("Running the expected commit on main.")
            return 0
        if args.post:
            records = checked_records(Path(args.post), source["GITHUB_SHA"])
            post_records(run, records)
            print(f"Posted {len(records)} records on #44 and #11.")
            return 0
        if args.cleanup:
            ci_credentials(source)  # shape-checked before use
            for item in cleanup(source):
                print(f"removed {item}")
            return 0
        if checked_out_commit(run=run) != source["ILMAR_EXPECTED_SHA"]:
            raise RunError("the checked-out tree is not the expected commit")
        settings = ci_settings(source)
        check_prerequisites(settings, run)
        credentials = ci_credentials(source)
    except RunError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    base = {k: v for k, v in source.items() if k not in credentials}
    env = test_environment(base, settings, credentials)
    secrets = list(credentials.values())
    credentials.clear()
    reports = Path(settings["ILMAR_LIVE_REPORT_DIR"])
    started = time.time()
    code = 1
    try:
        code = run_stages(env, reports, started, run)
    finally:
        env.clear()
        write_summary(reports, {
            "outcome": "passed" if code == 0 else "failed",
            "commit": source["ILMAR_EXPECTED_SHA"],
            "claude_code_version": settings["ILMAR_TEST_CLAUDE_VERSION"],
            "workflow_run": source.get("GITHUB_RUN_ID", ""),
            "at": datetime.now(UTC).isoformat(),
        }, secrets)  # fmt: skip
        secrets.clear()
    if code == 0:
        print(f"{len(new_records(reports, started))} records ready for upload.")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
