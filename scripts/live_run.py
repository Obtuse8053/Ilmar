# SPDX-License-Identifier: Apache-2.0
"""Run #44 and the live end-to-end tests (#11d) in one command.

    python3 scripts/live_run.py [--post]

* reads the non-secret settings written by ``scripts/live_setup.py``;
* asks for the GitHub token and the Anthropic key without echoing them.
  They live only in this process's memory and in the environment of the
  test process it starts: never in argv, a file, the shell history or the
  output;
* runs the #44 smoke test, then (only if it passed) the live end-to-end
  tests: the preflight, the published, merged and completed task, and the
  crash drill;
* with ``--post``, posts the credential-free records on #44 and #11 with your
  ``gh`` login.

Exit status: 0 if everything passed; otherwise that of the failing stage.
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import shutil
import subprocess
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path

from live_setup import SETTING_NAMES, SETTINGS

ROOT = Path(__file__).resolve().parents[1]
UPSTREAM = "Obtuse8053/Ilmar"
SMOKE = "tests/integration/test_claude_code_live.py"
LIVE = "tests/integration/test_live_end_to_end.py"
GITHUB_TOKEN_ENV = "ILMAR_LIVE_GITHUB_TOKEN"  # noqa: S105 - a variable name
ANTHROPIC_KEY_ENV = "ANTHROPIC_API_KEY"
DROPPED = ("PYTEST_ADDOPTS", "PYTEST_PLUGINS")
"""Never inherited: they could add ``--showlocals`` or plugins that print values."""


class RunError(Exception):
    pass


def read_settings(path: Path) -> dict[str, str]:
    if not path.exists():
        raise RunError(f"{path} is missing: run scripts/live_setup.py first")
    if path.stat().st_mode & 0o077:
        raise RunError(f"{path} must be private (chmod 600)")
    values: dict[str, str] = {}
    for line in path.read_text().splitlines():
        if not line.strip() or line.startswith("#"):
            continue
        key, _, value = line.partition("=")
        if key not in SETTING_NAMES:
            raise RunError(f"{path}: unexpected setting {key!r}; settings hold no secrets")
        values[key] = value
    if missing := [k for k in SETTING_NAMES if not values.get(k)]:
        raise RunError(f"{path} lacks {', '.join(missing)}: run scripts/live_setup.py again")
    return values


def ask_credentials(ask: Callable[[str], str]) -> dict[str, str]:
    token = ask("GitHub token (github_pat_…, input hidden): ").strip()
    if not token.startswith("github_pat_"):
        raise RunError("that is not a fine-grained GitHub token (github_pat_…)")
    key = ask("Anthropic key (input hidden): ").strip()
    if not key.startswith("sk-ant-") or key == token:
        raise RunError("that is not an Anthropic API key (sk-ant-…)")
    return {GITHUB_TOKEN_ENV: token, ANTHROPIC_KEY_ENV: key}


def test_environment(
    base: Mapping[str, str], settings: Mapping[str, str], credentials: Mapping[str, str]
) -> dict[str, str]:
    env = {k: v for k, v in base.items() if k not in DROPPED and k not in credentials}
    env.update(settings)
    env.update(credentials)
    env["ILMAR_LIVE_REQUIRED"] = "1"  # fail, never skip, when something is missing
    return env


def without_credentials(base: Mapping[str, str]) -> dict[str, str]:
    return {k: v for k, v in base.items() if k not in (GITHUB_TOKEN_ENV, ANTHROPIC_KEY_ENV)}


def pytest_command(target: str) -> list[str]:
    """No ``-l``/``--showlocals``; ``-o addopts=`` drops any configured extras."""
    return [sys.executable, "-m", "pytest", "-o", "addopts=", "--strict-markers",
            "-p", "no:cacheprovider", "-m", "integration", "-v", "-rs", "--tb=short",
            target]  # fmt: skip


def comment_body(title: str, record: Path) -> str:
    data = json.loads(record.read_text())  # written by the harness after a secret check
    return f"{title} `{record.name}`:\n\n```json\n{json.dumps(data, indent=2)}\n```\n"


def post(run: Callable[..., subprocess.CompletedProcess[str]], issue: int, body: str) -> None:
    gh = shutil.which("gh")
    if gh is None:
        raise RunError("gh is not installed; post the records by hand")
    done = run([gh, "issue", "comment", str(issue), "--repo", UPSTREAM, "--body-file", "-"],
               input=body, text=True, capture_output=True, check=False,
               env=without_credentials(os.environ))  # fmt: skip
    if done.returncode != 0:
        raise RunError(f"posting on #{issue} failed: {done.stderr.strip()[:200]}")


def main(
    argv: Sequence[str] | None = None,
    *,
    ask: Callable[[str], str] = getpass.getpass,
    run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--post", action="store_true", help="post the records on #44 and #11")
    parser.add_argument("--settings", default=str(SETTINGS))
    args = parser.parse_args(argv)
    try:
        settings = read_settings(Path(args.settings))
        credentials = ask_credentials(ask)
    except RunError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    env = test_environment(os.environ, settings, credentials)
    credentials.clear()
    reports = Path(settings["ILMAR_LIVE_REPORT_DIR"])
    started = time.time()
    for stage, target in (("#44 smoke test", SMOKE), ("#11d live end-to-end", LIVE)):
        print(f"\n== {stage} ==", flush=True)
        done = run(pytest_command(target), cwd=ROOT, env=env, check=False)
        if done.returncode != 0:
            print(f"\n{stage} FAILED (exit {done.returncode}). Nothing more was run.")
            print("The tests removed what they made; see docs/operations/live-testing.md.")
            return int(done.returncode)
    env.clear()
    records = sorted(r for r in reports.glob("*.json") if r.stat().st_mtime >= started)
    print("\nAll live tests passed. Records (no credentials):")
    for record in records:
        print(f"  {record}")
    if args.post:
        try:
            for record in records:
                issue = 44 if record.name.startswith("issue-44") else 11
                post(run, issue, comment_body("Live test record", record))
        except RunError as error:
            print(f"error: {error}", file=sys.stderr)
            return 1
        print("Posted on #44 and #11.")
    print("\nNow revoke or let expire the GitHub token and the Anthropic key.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
