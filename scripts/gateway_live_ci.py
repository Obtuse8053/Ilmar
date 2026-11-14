# SPDX-License-Identifier: Apache-2.0
"""The guarded real-API run of the gateway's spending controls (#111, P5).

Run only by ``.github/workflows/gateway-live.yml``, started by hand on main at
a commit the maintainer names. It reuses the boundaries of the main live
workflow (ADR-0023, ``scripts/live_ci.py``), with its own protected
environment, ``gateway-live``, which holds one secret: a model API key from a
workspace with a **small spend cap**.

* ``--guard`` (no secrets): the dispatch context, main's rules, that main
  still points at the commit, and the environment's protection rules.
* ``--guard-context`` (in the environment, before any secret): the context
  and the checked-out tree again, and main again.
* ``--run`` (the one step with the secret): checks the hash-locked
  dependencies, runs ``tests/integration/test_gateway_live_api.py`` with
  ``ILMAR_LIVE_REQUIRED=1``, then keeps only records that are checked to hold
  no credential: the key itself, or anything shaped like one.

Standard library only.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

import live_ci
from live_run import RunError

ENVIRONMENT = "gateway-live"
WORKFLOW_FILE = ".github/workflows/gateway-live.yml"
KEY_ENV = "ANTHROPIC_API_KEY"
TEST = "tests/integration/test_gateway_live_api.py"
RECORD = re.compile(r"issue-111-gateway-live-api\.json")
KEY_SHAPE = re.compile(r"sk-ant-[A-Za-z0-9_-]{20,}")


def check_context(env: Mapping[str, str]) -> list[str]:
    """Started by hand, on main, from this workflow file, at the named commit."""
    problems = []
    if env.get("GITHUB_ACTIONS") != "true":
        problems.append("not running in GitHub Actions")
    if env.get("GITHUB_EVENT_NAME") != "workflow_dispatch":
        problems.append("not started by workflow_dispatch")
    if env.get("GITHUB_REF") != "refs/heads/main":
        problems.append("not running on refs/heads/main")
    expected = env.get("ILMAR_EXPECTED_SHA", "")
    if not live_ci.SHA.fullmatch(expected):
        problems.append("expected_sha must be the full 40-character commit")
    elif env.get("GITHUB_SHA") != expected or env.get("GITHUB_WORKFLOW_SHA") != expected:
        problems.append("the workflow is not running the expected commit")
    repository = env.get("GITHUB_REPOSITORY", "")
    if env.get("GITHUB_WORKFLOW_REF") != f"{repository}/{WORKFLOW_FILE}@refs/heads/main":
        problems.append(f"this is not {WORKFLOW_FILE} on main")
    return problems


def guard(env: Mapping[str, str], get: Callable[[str, str], Any]) -> list[str]:
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
    return [
        *problems,
        *live_ci.check_environment(environment, policies),
        *live_ci.check_main_rules(rules),
        *live_ci.check_main_head(env, get),
    ]


def checked_records(directory: Path, key: str) -> list[Path]:
    """Keep only the expected record, and only if it holds no credential; a
    record that fails is deleted, never uploaded."""
    kept = []
    for record in sorted(directory.glob("*")) if directory.is_dir() else []:
        data = record.read_bytes() if record.is_file() and not record.is_symlink() else b""
        text = data.decode("utf-8", "replace")
        sound = (
            RECORD.fullmatch(record.name) is not None
            and 0 < len(data) <= live_ci.MAX_RECORD_BYTES
            and not (key and key in text)
            and not KEY_SHAPE.search(text)
            and not live_ci.CREDENTIAL_SHAPES.search(text)
        )
        if sound:
            try:
                json.loads(data)
            except ValueError:
                sound = False
        if sound:
            kept.append(record)
        else:
            record.unlink(missing_ok=True)
            print(f"removed {record.name}: not a sound, credential-free record", file=sys.stderr)
    return kept


def run_tests(env: Mapping[str, str], run: Callable[..., Any]) -> int:
    key = env.get(KEY_ENV, "")
    if not KEY_SHAPE.fullmatch(key):
        raise RunError(f"{KEY_ENV} is missing or not shaped like a model API key")
    site = Path(env.get("ILMAR_LIVE_SITE", ""))
    live_ci.check_site(site, live_ci.LOCK.read_text())
    records = Path(env.get("ILMAR_LIVE_REPORT_DIR", ""))
    if not records.is_absolute():
        raise RunError("ILMAR_LIVE_REPORT_DIR must be an absolute path")
    child = {
        "PATH": env.get("PATH", "/usr/bin:/bin"),
        "HOME": env.get("HOME", "/tmp"),  # noqa: S108 - a fallback only
        "PYTHONPATH": str(site),
        "PYTHONDONTWRITEBYTECODE": "1",
        KEY_ENV: key,
        "ILMAR_LIVE_REQUIRED": "1",
        "ILMAR_LIVE_REPORT_DIR": str(records),
    }
    command = [sys.executable, "-s", "-m", "pytest", "-p", "no:cacheprovider", "-m",
               "integration", "-v", TEST]  # fmt: skip
    # -s: no user site. A minimal environment: only the checked dependencies on
    # the path (pytest adds the repository's own from pyproject), and the key.
    done = run(command, cwd=str(live_ci.ROOT), env=child, check=False)
    checked_records(records, key)
    return int(done.returncode)


def main(
    argv: Sequence[str] | None = None,
    *,
    environ: Mapping[str, str] | None = None,
    run: Callable[..., Any] = subprocess.run,
    get: Callable[[str, str], Any] = live_ci.github_get,
) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--guard", action="store_true")
    mode.add_argument("--guard-context", action="store_true")
    mode.add_argument("--run", action="store_true")
    args = parser.parse_args(argv)
    source = dict(os.environ if environ is None else environ)
    try:
        if args.guard:
            if problems := guard(source, get):
                for problem in problems:
                    print(f"refused: {problem}", file=sys.stderr)
                print(
                    "See docs/operations/live-testing.md, 'Gateway real-API run'.", file=sys.stderr
                )
                return 1
            print(f"The context and the '{ENVIRONMENT}' environment are as required.")
            return 0
        if problems := check_context(source):
            raise RunError("; ".join(problems))
        if live_ci.checked_out_commit(run=run) != source["ILMAR_EXPECTED_SHA"]:
            raise RunError("the checked-out tree is not the expected commit")
        if args.guard_context:
            if problems := live_ci.check_main_head(source, get):
                raise RunError("; ".join(problems))
            print("Running the expected commit, and main still points at it.")
            return 0
        return run_tests(source, run)
    except RunError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
