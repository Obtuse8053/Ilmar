# SPDX-License-Identifier: Apache-2.0
"""Check the Gate 1 disposable repository before a trial (#93, ADR-0041).

Run by the operator, on the host, before ``ilmar gate trial --disposable``:

    git clone --mirror https://github.com/Obtuse8053/ilmar-disposable.git /tmp/d.git
    python3 scripts/gate1_repo_check.py --mirror /tmp/d.git \\
        --repository Obtuse8053/ilmar-disposable --report gate1-repo-check.json

It reads, and changes nothing:

* **every object in every ref's history** (``git cat-file`` on a bare mirror: no
  checkout, no hooks, no filters run). No blob may look like a credential, with
  the same shapes the event log refuses (GitHub tokens, Anthropic keys, and
  their base64 forms; ``ilmar.domain.events.looks_like_a_credential``).
* **every path in every commit.** None may be a workflow (``.github/``), an
  agent setting (``.claude/``, ``.mcp.json``, ``CLAUDE.md``, ``AGENTS.md``), a
  submodule (``.gitmodules``) or a filter (``.gitattributes``).
* **the repository's settings, with your ``gh`` login:** it must be private,
  carry the ``ilmar-disposable`` topic, and have no Actions secrets, Actions
  variables or environments. Its immutable ID is printed for
  ``[autonomy] repository_id``.

Exit 0 when every check passes, 1 when one fails, 2 when it cannot check.
The report (``--report``) holds no credential: a finding names the object and
the kind of match, never the matched text.

This is a check of what Ilmar can see. It does not replace the operator's own
review: a credential in an unusual encoding, or one held only in an issue or
pull-request comment, is not detected.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

FORBIDDEN_PREFIXES = (".github/", ".claude/")
FORBIDDEN_NAMES = (".mcp.json", ".gitmodules", ".gitattributes", "CLAUDE.md", "AGENTS.md")
TOPIC = "ilmar-disposable"
_MAX_BLOB = 8 * 1024 * 1024

Run = Callable[..., "subprocess.CompletedProcess[bytes]"]


def _run(argv: Sequence[str], data: bytes | None = None) -> subprocess.CompletedProcess[bytes]:
    """Run a fixed tool with no user or system git configuration."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull, GIT_TERMINAL_PROMPT="0")
    return subprocess.run(  # noqa: S603 - fixed argv built here
        list(argv), input=data, capture_output=True, check=False, env=env, timeout=600
    )


def _git(run: Run, mirror: Path, *args: str, data: bytes | None = None) -> bytes:
    git = shutil.which("git") or "git"
    done = run([git, "-c", "core.hooksPath=/dev/null", "--git-dir", str(mirror), *args], data)
    if done.returncode != 0:
        reason = done.stderr.decode(errors="replace")[:200]
        raise RuntimeError(f"git {args[0]} failed: {reason}")
    return done.stdout


def forbidden_path(path: str) -> bool:
    name = path.rsplit("/", 1)[-1]
    return name in FORBIDDEN_NAMES or any(f"/{p}" in f"/{path}" for p in FORBIDDEN_PREFIXES)


def check_history(
    mirror: Path, *, looks_like: Callable[[str], bool], run: Run = _run
) -> list[dict[str, str]]:
    """Findings in every object reachable from every ref."""
    findings: list[dict[str, str]] = []
    listing = _git(run, mirror, "rev-list", "--all", "--objects").decode()
    if not listing.strip():
        raise RuntimeError("the mirror has no objects: is it a clone of the repository?")
    paths: dict[str, str] = {}
    for line in listing.splitlines():
        sha, _, path = line.partition(" ")
        if path:
            paths[sha] = path
            if forbidden_path(path):
                findings.append({"object": sha, "path": path, "kind": "forbidden path"})
    batch = ("\n".join(paths) + "\n").encode()
    form = "--batch-check=%(objectname) %(objecttype) %(objectsize)"
    for line in _git(run, mirror, "cat-file", form, data=batch).decode().splitlines():
        sha, kind, size = line.split(" ")
        if kind != "blob":
            continue
        where = paths.get(sha, "")
        if int(size) > _MAX_BLOB:
            findings.append({"object": sha, "path": where, "kind": "too large to scan"})
            continue
        content = _git(run, mirror, "cat-file", "blob", sha).decode("utf-8", errors="replace")
        if looks_like(content):
            findings.append({"object": sha, "path": where, "kind": "credential shape"})
    return findings


def check_settings(repository: str, *, run: Run = _run) -> tuple[dict[str, Any], list[str]]:
    """The repository's ID and visibility, and every setting that fails."""
    gh = shutil.which("gh") or "gh"

    def api(path: str) -> Any:
        done = run([gh, "api", path], None)
        if done.returncode != 0:
            raise RuntimeError(
                f"gh api {path} failed: {done.stderr.decode(errors='replace')[:200]}"
            )
        return json.loads(done.stdout)

    repo = api(f"repos/{repository}")
    failures = []
    if repo.get("private") is not True:
        failures.append("the repository is not private")
    if TOPIC not in (repo.get("topics") or []):
        failures.append(f"the repository lacks the {TOPIC!r} topic")
    for path, key in ((f"repos/{repository}/actions/secrets", "total_count"),
                      (f"repos/{repository}/actions/variables", "total_count"),
                      (f"repos/{repository}/environments", "total_count")):  # fmt: skip
        count = api(path).get(key, 0)
        if count:
            failures.append(f"{path.rsplit('/', 1)[-1]}: {count} defined (expected none)")
    facts = {
        "repository": repo.get("full_name"),
        "repository_id": repo.get("id"),
        "private": repo.get("private"),
        "default_branch": repo.get("default_branch"),
    }
    return facts, failures


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").split("\n", 1)[0])
    parser.add_argument("--mirror", type=Path, required=True, help="a `git clone --mirror`")
    parser.add_argument("--repository", required=True, help="owner/name")
    parser.add_argument("--report", type=Path, help="write the JSON report here")
    parser.add_argument("--skip-settings", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    try:
        from ilmar.domain.events import looks_like_a_credential
    except ImportError:
        print("error: run from an environment where Ilmar is installed", file=sys.stderr)
        return 2
    try:
        findings = check_history(args.mirror, looks_like=looks_like_a_credential)
        facts, failures = ({}, []) if args.skip_settings else check_settings(args.repository)
    except (RuntimeError, OSError, subprocess.SubprocessError, ValueError) as error:
        print(f"error: cannot check: {error}", file=sys.stderr)
        return 2
    report = {"check": "gate1-disposable-repository", **facts,
              "history_findings": findings, "setting_failures": failures,
              "passed": not findings and not failures}  # fmt: skip
    text = json.dumps(report, indent=2, sort_keys=True)
    if args.report:
        args.report.write_text(text + "\n")
    print(text)
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
