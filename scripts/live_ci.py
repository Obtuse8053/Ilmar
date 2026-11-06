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
import os
import re
import subprocess
import sys
import time
from collections.abc import Callable, Mapping, Sequence
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
from live_setup import PROXY_IMAGE

VERSION = re.compile(r"\d+\.\d+\.\d+")


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


def main(
    argv: Sequence[str] | None = None,
    *,
    environ: Mapping[str, str] | None = None,
    run: Callable[..., Any] = subprocess.run,
) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--post", metavar="DIR", help="post the records in DIR on #44 and #11")
    args = parser.parse_args(argv)
    source = dict(os.environ if environ is None else environ)
    try:
        if args.post:
            if source.get("GITHUB_ACTIONS") != "true":
                raise RunError("live_ci.py runs only in GitHub Actions")
            records = sorted(Path(args.post).glob("*.json"))
            if not records:
                raise RunError(f"no records in {args.post}")
            post_records(run, records)
            print(f"Posted {len(records)} records on #44 and #11.")
            return 0
        settings = ci_settings(source)
        check_prerequisites(settings, run)
        credentials = ci_credentials(source)
    except RunError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    base = {k: v for k, v in source.items() if k not in credentials}
    env = test_environment(base, settings, credentials)
    credentials.clear()
    reports = Path(settings["ILMAR_LIVE_REPORT_DIR"])
    started = time.time()
    code = run_stages(env, reports, started, run)
    env.clear()
    if code == 0:
        print(f"{len(new_records(reports, started))} records ready for upload.")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
