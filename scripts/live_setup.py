# SPDX-License-Identifier: Apache-2.0
"""One-time setup for Ilmar's live tests (#44, #11d). Never sees a credential.

    python3 scripts/live_setup.py --claude-version X.Y.Z

* checks the host: rootless Podman, git, Python 3.11+, and the ``gh`` CLI
  logged in as you;
* makes sure ``<you>/ilmar-disposable`` (private, with the ``ilmar-disposable``
  topic) and the private canary ``<you>/ilmar-outside-canary`` exist. They are
  created if missing. An existing public one is refused, never changed;
* pulls the proxy and verification image and builds the pinned Claude Code
  image (locally, under Anthropic's terms; it is never published);
* writes the non-secret settings to ``~/.config/ilmar-live/settings.env``
  (mode 0600);
* prints the pre-filled link for the GitHub token and what to set for the
  Anthropic key.

It uses your ``gh`` login only for setup, never the live-test token, which you
create afterwards and give only to ``scripts/live_run.py``.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import urllib.parse
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

DISPOSABLE = "ilmar-disposable"
CANARY = "ilmar-outside-canary"
TOPIC = "ilmar-disposable"
PROXY_IMAGE = "docker.io/library/python:3.12-alpine"
SETTINGS = Path.home() / ".config" / "ilmar-live" / "settings.env"
ROOT = Path(__file__).resolve().parents[1]
VERSION = re.compile(r"\d+\.\d+\.\d+")
SETTING_NAMES = (
    "ILMAR_LIVE_REPO",
    "ILMAR_LIVE_OUTSIDE_REPO",
    "ILMAR_TEST_CLAUDE_VERSION",
    "ILMAR_TEST_CLAUDE_IMAGE",
    "ILMAR_TEST_PODMAN_PROXY_IMAGE",
    "ILMAR_LIVE_REPORT_DIR",
)


class SetupError(Exception):
    """Setup cannot continue; the message says what to fix."""


Run = Callable[[Sequence[str]], subprocess.CompletedProcess[str]]


def run_command(argv: Sequence[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 - fixed argv built here
        list(argv), capture_output=True, text=True, check=False, timeout=1800
    )


def tool(name: str) -> str:
    found = shutil.which(name)
    if found is None:
        raise SetupError(f"{name} is not installed")
    return found


@dataclass(frozen=True)
class RepoState:
    exists: bool
    private: bool = False
    topics: tuple[str, ...] = ()


def repo_state(run: Run, gh: str, full_name: str) -> RepoState:
    done = run([gh, "repo", "view", full_name, "--json", "isPrivate,repositoryTopics,name"])
    if done.returncode != 0:
        if "Could not resolve" in done.stderr or "not found" in done.stderr.lower():
            return RepoState(exists=False)
        raise SetupError(f"gh repo view {full_name} failed: {done.stderr.strip()[:200]}")
    data = json.loads(done.stdout)
    topics = tuple(t.get("name", "") for t in (data.get("repositoryTopics") or []))
    return RepoState(exists=True, private=bool(data.get("isPrivate")), topics=topics)


def ensure_repository(
    run: Run, gh: str, full_name: str, *, description: str, topic: str | None
) -> list[str]:
    """Create the private repository if missing; never change an existing public one."""
    actions = []
    state = repo_state(run, gh, full_name)
    if not state.exists:
        done = run([gh, "repo", "create", full_name, "--private", "--add-readme",
                    "--description", description])  # fmt: skip
        if done.returncode != 0:
            raise SetupError(f"cannot create {full_name}: {done.stderr.strip()[:200]}")
        actions.append(f"created {full_name} (private)")
        state = RepoState(exists=True, private=True)
    elif not state.private:
        raise SetupError(
            f"{full_name} exists and is public. The live tests need it private; this "
            "script never changes an existing repository's visibility. Make it private "
            "or delete it, then rerun."
        )
    if topic and topic not in state.topics:
        done = run([gh, "repo", "edit", full_name, "--add-topic", topic])
        if done.returncode != 0:
            detail = done.stderr.strip()[:200]
            raise SetupError(f"cannot add the topic to {full_name}: {detail}")
        actions.append(f"added topic {topic} to {full_name}")
    return actions


def token_link(owner: str) -> str:
    """GitHub's pre-filled form; the repository selection cannot be pre-filled."""
    query = urllib.parse.urlencode({
        "name": "ilmar-live",
        "description": f"Ilmar live tests: only {owner}/{DISPOSABLE}",
        "target_name": owner,
        "expires_in": "7",
        "contents": "write",
        "issues": "write",
        "pull_requests": "write",
    })  # fmt: skip
    return f"https://github.com/settings/personal-access-tokens/new?{query}"


def settings_text(owner: str, version: str, report_dir: Path) -> str:
    values = {
        "ILMAR_LIVE_REPO": f"{owner}/{DISPOSABLE}",
        "ILMAR_LIVE_OUTSIDE_REPO": f"{owner}/{CANARY}",
        "ILMAR_TEST_CLAUDE_VERSION": version,
        "ILMAR_TEST_CLAUDE_IMAGE": f"localhost/ilmar-claude-code:{version}",
        "ILMAR_TEST_PODMAN_PROXY_IMAGE": PROXY_IMAGE,
        "ILMAR_LIVE_REPORT_DIR": str(report_dir),
    }
    lines = ["# Ilmar live-test settings. No secrets: the credentials are asked for at run time."]
    lines += [f"{key}={values[key]}" for key in SETTING_NAMES]
    return "\n".join(lines) + "\n"


def write_settings(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_suffix(".tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w") as handle:
        handle.write(text)
    os.replace(temporary, path)


def check_host(run: Run, which: Callable[[str], str] = tool) -> tuple[str, str]:
    if sys.version_info < (3, 11):  # noqa: UP036 - the script may run on an older python3
        raise SetupError("Python 3.11 or newer is needed")
    podman, gh = which("podman"), which("gh")
    which("git")
    rootless = run([podman, "info", "--format", "{{.Host.Security.Rootless}}"])
    if rootless.stdout.strip() != "true":
        raise SetupError("Podman must run rootless (as your user, not root)")
    if run([gh, "auth", "status"]).returncode != 0:
        raise SetupError("log in to the gh CLI first: gh auth login")
    return podman, gh


def build_images(run: Run, podman: str, version: str) -> list[str]:
    actions = []
    if run([podman, "image", "exists", PROXY_IMAGE]).returncode != 0:
        if run([podman, "pull", PROXY_IMAGE]).returncode != 0:
            raise SetupError(f"cannot pull {PROXY_IMAGE}")
        actions.append(f"pulled {PROXY_IMAGE}")
    image = f"localhost/ilmar-claude-code:{version}"
    if run([podman, "image", "exists", image]).returncode != 0:
        built = run([podman, "build", "--build-arg", f"CLAUDE_CODE_VERSION={version}",
                     "-t", image, str(ROOT / "images" / "claude-code")])  # fmt: skip
        if built.returncode != 0:
            raise SetupError(f"building {image} failed: {built.stderr.strip()[-300:]}")
        actions.append(f"built {image}")
    return actions


def main(argv: Sequence[str] | None = None, *, run: Run = run_command) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--claude-version", required=True, help="pinned Claude Code x.y.z")
    parser.add_argument("--report-dir", default=str(Path.home() / "ilmar-live-reports"))
    parser.add_argument("--settings", default=str(SETTINGS))
    args = parser.parse_args(argv)
    if not VERSION.fullmatch(args.claude_version):
        print("error: --claude-version must be x.y.z", file=sys.stderr)
        return 2
    try:
        podman, gh = check_host(run)
        owner = run([gh, "api", "user", "--jq", ".login"]).stdout.strip()
        if not re.fullmatch(r"[A-Za-z0-9-]+", owner):
            raise SetupError("cannot tell your GitHub login from gh")
        actions = ensure_repository(
            run, gh, f"{owner}/{DISPOSABLE}", topic=TOPIC,
            description="Disposable repository for Ilmar's live tests; safe to delete",
        )  # fmt: skip
        actions += ensure_repository(
            run, gh, f"{owner}/{CANARY}", topic=None,
            description="Must NOT be reachable by Ilmar's live-test token",
        )  # fmt: skip
        actions += build_images(run, podman, args.claude_version)
        text = settings_text(owner, args.claude_version, Path(args.report_dir))
        write_settings(Path(args.settings), text)
    except SetupError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    for action in actions:
        print(f"  {action}")
    print(f"settings written to {args.settings} (no secrets)\n")
    print("Now create the two credentials (keep them in your password manager, nowhere else):")
    print(f"1. GitHub token: open\n   {token_link(owner)}\n"
          f"   Under 'Repository access' choose 'Only select repositories' -> {DISPOSABLE}.\n"
          "   Keep every other permission at 'No access'. Generate and copy it.")  # fmt: skip
    print("2. Anthropic key: in the Anthropic Console create a workspace 'ilmar-live',")
    print("   set a low monthly spend limit (e.g. USD 5), and create an API key in it.")
    print("\nThen run:  python3 scripts/live_run.py")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
