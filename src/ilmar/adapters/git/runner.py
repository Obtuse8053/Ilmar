# SPDX-License-Identifier: Apache-2.0
"""Hardened host-side git (ADR-0010 §2).

Every command runs against a coordinator-owned git directory, with a freshly
built environment. Nothing from the host user's or the system's git
configuration applies, and nothing can execute:

* ``GIT_CONFIG_NOSYSTEM``, ``GIT_CONFIG_GLOBAL=/dev/null`` and a private
  ``HOME``: no system, global or XDG configuration;
* hooks off (``core.hooksPath=/dev/null``), as are fsmonitor, pagers, the
  editor, credential helpers and prompts; ``core.attributesFile`` is
  ``/dev/null``;
* only the protocols in ``allowed_protocols`` (``https`` by default),
  ``http.followRedirects=false``, and ``transfer.fsckObjects`` on;
* no inherited ``GIT_*`` variable (``GIT_DIR``, ``GIT_SSH_COMMAND``,
  ``GIT_TRACE``…). The caller passes only the extra configuration it needs
  through ``GIT_CONFIG_COUNT``/``KEY``/``VALUE``, which keeps secrets off the
  command line.

This module never runs git with a working tree: callers use plumbing only.
"""

from __future__ import annotations

import contextlib
import os
import re
import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from ilmar.domain.errors import ProviderError

_SAFE_CONFIG = (
    ("core.hooksPath", "/dev/null"),
    ("core.fsmonitor", "false"),
    ("core.pager", "cat"),
    ("core.editor", "false"),
    ("core.askPass", ""),
    ("core.attributesFile", "/dev/null"),
    ("core.excludesFile", "/dev/null"),
    ("core.sshCommand", "false"),
    ("core.symlinks", "true"),
    ("core.autocrlf", "false"),
    ("core.quotePath", "false"),
    ("core.untrackedCache", "false"),
    ("credential.helper", ""),
    ("diff.external", ""),
    ("http.followRedirects", "false"),
    ("transfer.fsckObjects", "true"),
    ("fetch.fsckObjects", "true"),
    ("submodule.recurse", "false"),
    ("gc.auto", "0"),
    ("maintenance.auto", "false"),
    ("commit.gpgSign", "false"),
    ("tag.gpgSign", "false"),
    ("init.defaultBranch", "main"),
    ("protocol.allow", "never"),
    ("safe.bareRepository", "explicit"),
)
_PROTOCOL = re.compile(r"^[a-z][a-z0-9+.-]*$")


@dataclass(frozen=True, slots=True)
class GitResult:
    returncode: int
    stdout: bytes
    stderr: bytes

    def text(self) -> str:
        return self.stdout.decode("utf-8", errors="replace")

    def error(self) -> str:
        lines = self.stderr.decode("utf-8", errors="replace").strip().splitlines()
        return lines[-1][:300] if lines else f"exit {self.returncode}"


class HardenedGit:
    """Runs ``git`` against one coordinator-owned bare repository."""

    def __init__(
        self,
        git_dir: Path,
        *,
        home: Path,
        git: str = "git",
        allowed_protocols: Sequence[str] = ("https",),
        timeout: float = 600.0,
    ) -> None:
        for protocol in allowed_protocols:
            if not _PROTOCOL.match(protocol) or protocol in {"ext", "fd"}:
                raise ValueError(f"protocol not allowed: {protocol!r}")
        self.git_dir = git_dir
        self._home = home
        self._git = git
        self._protocols = tuple(allowed_protocols)
        self._timeout = timeout

    def environment(self, extra_config: Mapping[str, str] = {}) -> dict[str, str]:
        """The complete environment for one git process."""
        env = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": str(self._home),
            "XDG_CONFIG_HOME": str(self._home / "xdg"),
            "LANG": "C",
            "LC_ALL": "C",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_ASKPASS": "",
            "SSH_ASKPASS": "",
            "GIT_PAGER": "cat",
            "GIT_EDITOR": "false",
            "GIT_NO_REPLACE_OBJECTS": "1",
            "GIT_OPTIONAL_LOCKS": "0",
            "GIT_ATTR_NOSYSTEM": "1",
        }
        config = list(_SAFE_CONFIG)
        config += [(f"protocol.{p}.allow", "always") for p in self._protocols]
        config += list(extra_config.items())
        env["GIT_CONFIG_COUNT"] = str(len(config))
        for index, (key, value) in enumerate(config):
            env[f"GIT_CONFIG_KEY_{index}"] = key
            env[f"GIT_CONFIG_VALUE_{index}"] = value
        return env

    def run(
        self,
        *args: str,
        input: bytes | None = None,
        extra_config: Mapping[str, str] = {},
        check: bool = True,
    ) -> GitResult:
        argv = [self._git, "--no-pager", f"--git-dir={self.git_dir}", *args]
        try:
            done = subprocess.run(  # noqa: S603 - fixed executable, arguments built here
                argv,
                input=input,
                stdin=None if input is not None else subprocess.DEVNULL,
                capture_output=True,
                env=self.environment(extra_config),
                cwd=self._home,
                timeout=self._timeout,
                check=False,
            )
        except subprocess.TimeoutExpired:
            raise ProviderError(f"git {args[0]} timed out") from None
        except OSError as error:
            raise ProviderError(f"cannot run git: {error.strerror}") from None
        result = GitResult(done.returncode, done.stdout, done.stderr)
        if check and result.returncode != 0:
            raise ProviderError(f"git {args[0]} failed: {result.error()}")
        return result

    def init_bare(self) -> None:
        """Create the bare repository (idempotent)."""
        self._home.mkdir(mode=0o700, parents=True, exist_ok=True)
        if (self.git_dir / "HEAD").is_file():
            return
        self.git_dir.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        argv = [self._git, "init", "--quiet", "--bare", str(self.git_dir)]
        try:
            done = subprocess.run(  # noqa: S603
                argv,
                capture_output=True,
                env=self.environment(),
                cwd=self._home,
                timeout=self._timeout,
                check=False,
            )
        except OSError as error:
            raise ProviderError(f"cannot run git: {error.strerror}") from None
        if done.returncode != 0:
            raise ProviderError(f"git init failed: {GitResult(1, b'', done.stderr).error()}")
        # The template may have installed sample hooks; none may stay.
        hooks = self.git_dir / "hooks"
        if hooks.is_dir():
            for sample in hooks.iterdir():
                with contextlib.suppress(OSError):
                    sample.unlink()

    def has_commit(self, revision: str) -> bool:
        if not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", revision):
            return False
        result = self.run("cat-file", "-e", f"{revision}^{{commit}}", check=False)
        return result.returncode == 0
