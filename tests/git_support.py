# SPDX-License-Identifier: Apache-2.0
"""Plain git for test setup: how a human would push to a remote. Never used by Ilmar."""

from __future__ import annotations

import os
import subprocess
from collections.abc import Mapping
from pathlib import Path


class PlainGit:
    """Git as a *human* would run it, for test setup only; isolated from the user's config."""

    def __init__(self, home: Path) -> None:
        home.mkdir(parents=True, exist_ok=True)
        self.env = {
            "PATH": os.environ["PATH"],
            "HOME": str(home),
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_AUTHOR_NAME": "Human",
            "GIT_AUTHOR_EMAIL": "human@example.com",
            "GIT_COMMITTER_NAME": "Human",
            "GIT_COMMITTER_EMAIL": "human@example.com",
        }

    def __call__(self, *args: str, cwd: Path) -> str:
        done = subprocess.run(  # noqa: S603 - test setup
            ["git", "-c", "init.defaultBranch=main", "-c", "protocol.file.allow=always", *args],  # noqa: S607
            cwd=cwd,
            env=self.env,
            capture_output=True,
            text=True,
            check=True,
        )
        return done.stdout.strip()


def write_files(root: Path, files: Mapping[str, str]) -> None:
    for name, content in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
