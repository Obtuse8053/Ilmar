# SPDX-License-Identifier: Apache-2.0
"""Check the Gate 1 disposable repository before a trial (#93, ADR-0041).

Run by the operator, on the host, before ``ilmar gate trial --disposable``,
on a fresh mirror made just before:

    git clone --mirror https://github.com/Obtuse8053/ilmar-disposable.git /tmp/d.git
    python3 scripts/gate1_repo_check.py --mirror /tmp/d.git \\
        --repository Obtuse8053/ilmar-disposable --report gate1-repo-check.json

It reads, and changes nothing. Every check must pass, and none can be skipped:
a report that says ``"passed": true`` is the only acceptable Gate 1 evidence.

1. **The mirror is of that repository, as it is now.** Its ``origin`` is the
   repository's GitHub URL, and its branches are exactly the repository's
   branches, at the same commits (read through your ``gh`` login).
2. **Every path in every commit reachable from any ref.** Each commit's whole
   tree is listed (``git ls-tree -r``), so a file is found under every path it
   has ever had, even when several paths share one object. None may be a
   workflow (``.github/``), an agent setting (``.claude/``, ``.mcp.json``,
   ``CLAUDE.md``, ``AGENTS.md``), a submodule (``.gitmodules`` or a submodule
   entry) or a filter (``.gitattributes``).
3. **Every blob reachable from any ref** is scanned for the credential shapes
   the event log refuses (GitHub tokens, Anthropic keys, and their base64
   forms; ``ilmar.domain.events.looks_like_a_credential``). It is read with
   ``git cat-file`` from the bare mirror: no checkout, no hooks, no filters,
   and no user or system git configuration.
4. **The repository's settings:** private, with the ``ilmar-disposable``
   topic, and with no Actions secrets, Actions variables or environments. A
   missing or malformed answer fails the check: it is never taken as "none".

Exit 0 when every check passes, 1 when one fails, 2 when it cannot check.
The report (``--report``) holds no credential: a finding names the object, the
path and the kind of match, never the matched text.

**What this cannot prove.** Pattern scanning finds only the credential shapes
it knows. It cannot prove that a repository holds no secrets: a password, a
key from another provider, or a known key that is split, re-encoded or
compressed passes it. It also does not read issue or pull-request comments.
Your own review of the repository is still required. The report records only
what was checked, and says so.
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
LIMITATIONS = (
    "Credential-pattern scanning cannot prove the absence of arbitrary secrets "
    "(unknown formats, split, re-encoded or compressed values), and issue and "
    "pull-request comments are not read. The operator's own review is still required."
)
_MAX_BLOB = 8 * 1024 * 1024
_MAX_REFS = 100

Run = Callable[..., "subprocess.CompletedProcess[bytes]"]


class CheckError(Exception):
    """The check could not be completed: never a pass."""


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
        raise CheckError(f"git {args[0]} failed: {reason}")
    return bytes(done.stdout)


def forbidden_path(path: str) -> bool:
    name = path.rsplit("/", 1)[-1]
    return name in FORBIDDEN_NAMES or any(f"/{p}" in f"/{path}" for p in FORBIDDEN_PREFIXES)


def mirror_branches(mirror: Path, *, run: Run = _run) -> dict[str, str]:
    form = "--format=%(refname) %(objectname)"
    listing = _git(run, mirror, "for-each-ref", form, "refs/heads/").decode()
    return dict(line.split(" ", 1) for line in listing.splitlines() if line)


def check_history(
    mirror: Path, *, looks_like: Callable[[str], bool], run: Run = _run
) -> list[dict[str, str]]:
    """Findings at every path of every commit, and in every blob, reachable from any ref."""
    commits = _git(run, mirror, "rev-list", "--all").decode().split()
    if not commits:
        raise CheckError("the mirror has no commits: is it a clone of the repository?")
    findings: list[dict[str, str]] = []
    blobs: dict[str, str] = {}
    seen: set[tuple[str, str, str]] = set()
    for commit in commits:
        tree = _git(run, mirror, "ls-tree", "-r", "-z", "--full-tree", commit)
        for entry in tree.split(b"\0"):
            if not entry:
                continue
            meta, _, raw_path = entry.partition(b"\t")
            _mode, kind, sha = meta.decode().split(" ")
            path = raw_path.decode("utf-8", errors="replace")
            problem = "submodule" if kind == "commit" else (
                "forbidden path" if forbidden_path(path) else None
            )  # fmt: skip
            if problem is not None and (path, sha, problem) not in seen:
                seen.add((path, sha, problem))
                findings.append({"object": sha, "path": path, "commit": commit, "kind": problem})
            if kind == "blob":
                blobs.setdefault(sha, path)
    if not blobs:
        return findings
    batch = ("\n".join(blobs) + "\n").encode()
    form = "--batch-check=%(objectname) %(objecttype) %(objectsize)"
    for line in _git(run, mirror, "cat-file", form, data=batch).decode().splitlines():
        sha, kind, size = line.split(" ")
        where = blobs.get(sha, "")
        if kind != "blob":
            raise CheckError(f"object {sha} at {where} is not a blob")
        if int(size) > _MAX_BLOB:
            findings.append({"object": sha, "path": where, "kind": "too large to scan"})
            continue
        content = _git(run, mirror, "cat-file", "blob", sha).decode("utf-8", errors="replace")
        if looks_like(content):
            findings.append({"object": sha, "path": where, "kind": "credential shape"})
    return findings


def _api(run: Run, path: str) -> Any:
    gh = shutil.which("gh") or "gh"
    done = run([gh, "api", path], None)
    if done.returncode != 0:
        reason = done.stderr.decode(errors="replace")[:200]
        raise CheckError(f"gh api {path} failed: {reason}")
    try:
        return json.loads(done.stdout)
    except ValueError:
        raise CheckError(f"gh api {path} did not answer JSON") from None


def _count(answer: Any, path: str) -> int:
    count = answer.get("total_count") if isinstance(answer, dict) else None
    if type(count) is not int or count < 0:
        raise CheckError(f"{path}: no total_count in the answer; cannot tell whether any exist")
    return count


def check_settings(repository: str, *, run: Run = _run) -> tuple[dict[str, Any], list[str]]:
    """The repository's identity, and every setting that fails. Fails closed."""
    repo = _api(run, f"repos/{repository}")
    if not isinstance(repo, dict) or type(repo.get("id")) is not int:
        raise CheckError(f"repos/{repository}: no repository ID in the answer")
    if str(repo.get("full_name", "")).lower() != repository.lower():
        raise CheckError(f"repos/{repository} answered for {repo.get('full_name')!r}")
    failures = []
    if repo.get("private") is not True:
        failures.append("the repository is not private (or its visibility was not given)")
    topics = repo.get("topics")
    if not isinstance(topics, list) or TOPIC not in topics:
        failures.append(f"the repository lacks the {TOPIC!r} topic (or its topics were not given)")
    for kind in ("actions/secrets", "actions/variables", "environments"):
        path = f"repos/{repository}/{kind}"
        if count := _count(_api(run, path), path):
            failures.append(f"{kind}: {count} defined (expected none)")
    facts = {
        "repository": repo["full_name"],
        "repository_id": repo["id"],
        "private": repo.get("private"),
        "fork": repo.get("fork"),
        "default_branch": repo.get("default_branch"),
    }
    return facts, failures


def check_provenance(
    mirror: Path, repository: str, *, run: Run = _run
) -> tuple[dict[str, str], list[str]]:
    """The mirror is of ``repository`` and holds exactly its branches, now."""
    failures = []
    origin = _git(run, mirror, "config", "--get", "remote.origin.url").decode().strip()
    expected = {f"https://github.com/{repository}{end}".lower() for end in ("", ".git")}
    if origin.lower() not in expected:
        failures.append(f"the mirror's origin is {origin!r}, not {repository}")
    path = f"repos/{repository}/git/matching-refs/heads?per_page={_MAX_REFS}"
    answer = _api(run, path)
    if not isinstance(answer, list) or len(answer) >= _MAX_REFS:
        raise CheckError(f"{path}: not a complete list of branches")
    live: dict[str, str] = {}
    for ref in answer:
        name = ref.get("ref") if isinstance(ref, dict) else None
        target = ref.get("object") if isinstance(ref, dict) else None
        sha = target.get("sha") if isinstance(target, dict) else None
        if not isinstance(name, str) or not isinstance(sha, str):
            raise CheckError(f"{path}: a malformed ref in the answer")
        live[name] = sha
    if not live:
        raise CheckError(f"{path}: the repository has no branches")
    mirrored = mirror_branches(mirror, run=run)
    if mirrored != live:
        failures.append(
            "the mirror's branches are not the repository's branches now: make a fresh mirror"
        )
    return mirrored, failures


def main(argv: Sequence[str] | None = None, *, run: Run = _run) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").split("\n", 1)[0])
    parser.add_argument("--mirror", type=Path, required=True, help="a fresh `git clone --mirror`")
    parser.add_argument("--repository", required=True, help="owner/name")
    parser.add_argument("--report", type=Path, help="write the JSON report here")
    args = parser.parse_args(argv)
    try:
        from ilmar.domain.events import looks_like_a_credential
    except ImportError:
        print("error: run from an environment where Ilmar is installed", file=sys.stderr)
        return 2
    try:
        facts, setting_failures = check_settings(args.repository, run=run)
        branches, provenance_failures = check_provenance(args.mirror, args.repository, run=run)
        findings = check_history(args.mirror, looks_like=looks_like_a_credential, run=run)
    except (CheckError, OSError, subprocess.SubprocessError, ValueError) as error:
        print(f"error: cannot check, so nothing passed: {error}", file=sys.stderr)
        return 2
    failures = provenance_failures + setting_failures
    report = {
        "check": "gate1-disposable-repository",
        **facts,
        "branches": branches,
        "history_findings": findings,
        "failures": failures,
        "passed": not findings and not failures,
        "limitations": LIMITATIONS,
    }
    text = json.dumps(report, indent=2, sort_keys=True)
    if args.report:
        args.report.write_text(text + "\n")
    print(text)
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
