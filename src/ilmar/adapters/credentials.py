# SPDX-License-Identifier: Apache-2.0
"""Secrets from systemd's credentials directory (``[secrets] source``).

Under a unit with ``LoadCredentialEncrypted=<name>:<file>``, systemd decrypts
each credential at start into the service's private, in-memory
``$CREDENTIALS_DIRECTORY``. With ``source = "systemd-credentials"``, Ilmar
reads each secret its adapters name from there, by that name, instead of from
the environment. A value is never copied into Ilmar's own environment, which
every child process would inherit. There is no fallback to the environment:
anything missing or unexpected is a configuration error, and nothing starts.

Each file must be a regular file owned by this user, readable by nobody else,
at most 16 KiB, and hold one line. The directory itself must be absolute,
owned by this user and not writable by anyone else.
"""

from __future__ import annotations

import os
import re
import stat
from collections.abc import Iterable, Mapping
from pathlib import Path

from ilmar.domain.errors import ConfigError

DIRECTORY_VARIABLE = "CREDENTIALS_DIRECTORY"
MAX_BYTES = 16 * 1024
_NAME = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]{0,254}$")
_READ = os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)


def read_credentials(names: Iterable[str], *, environ: Mapping[str, str]) -> dict[str, str]:
    """Each of ``names`` read from ``$CREDENTIALS_DIRECTORY``. Raises ``ConfigError``.

    ``environ`` is consulted for the directory's path only."""
    directory = _directory(environ)
    return {name: _read(directory, name) for name in sorted(set(names))}


def _directory(environ: Mapping[str, str]) -> Path:
    raw = environ.get(DIRECTORY_VARIABLE, "")
    if not raw:
        raise ConfigError(
            f'[secrets] source = "systemd-credentials", but ${DIRECTORY_VARIABLE} is not set: '
            "start Ilmar from its systemd unit with LoadCredentialEncrypted="
        )
    directory = Path(raw)
    if not directory.is_absolute():
        raise ConfigError(f"${DIRECTORY_VARIABLE} must be an absolute path")
    try:
        info = os.stat(directory, follow_symlinks=False)
    except OSError as error:
        raise ConfigError(f"${DIRECTORY_VARIABLE} cannot be read: {error.strerror}") from None
    if not stat.S_ISDIR(info.st_mode):
        raise ConfigError(f"${DIRECTORY_VARIABLE} is not a directory")
    if info.st_uid != os.geteuid() or info.st_mode & 0o022:
        raise ConfigError(
            f"${DIRECTORY_VARIABLE} must be owned by this user and writable by nobody else"
        )
    return directory


def _read(directory: Path, name: str) -> str:
    if not _NAME.match(name) or name in (".", ".."):
        raise ConfigError(f"{name!r} is not a valid systemd credential name")
    where = f"systemd credential {name}"
    try:
        fd = os.open(directory / name, _READ)
    except FileNotFoundError:
        raise ConfigError(
            f"{where} is missing: add LoadCredentialEncrypted={name}:<file> to the unit"
        ) from None
    except OSError as error:  # a symlink (ELOOP), permissions, …
        raise ConfigError(f"{where} cannot be opened: {error.strerror}") from None
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise ConfigError(f"{where} is not a regular file")
        if info.st_uid != os.geteuid() or info.st_mode & 0o077:
            raise ConfigError(f"{where} must be owned by this user and readable by nobody else")
        if info.st_size > MAX_BYTES:
            raise ConfigError(f"{where} is larger than {MAX_BYTES} bytes")
        data = os.read(fd, MAX_BYTES + 1)
    finally:
        os.close(fd)
    if len(data) > MAX_BYTES:
        raise ConfigError(f"{where} is larger than {MAX_BYTES} bytes")
    try:
        value = data.decode("utf-8").removesuffix("\n")
    except UnicodeDecodeError:
        raise ConfigError(f"{where} is not UTF-8 text") from None
    if not value or any(c in value for c in "\r\n\0"):
        raise ConfigError(f"{where} must hold one non-empty line")
    return value
