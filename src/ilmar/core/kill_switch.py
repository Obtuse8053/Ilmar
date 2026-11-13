# SPDX-License-Identifier: Apache-2.0
"""The kill-switch file (ADR-0029 §4, #18 part 2).

While the configured file exists, Ilmar suspends with ``kill-switch``: no new
claim and no new container. ``ilmar resume`` lifts that suspension only once
the file is gone.

Presence is decided by ``lstat`` alone and fails closed: a file, a directory,
a broken symlink, or a path that cannot be checked (permission denied, a
component that is not a directory) all count as present. Only "no such file"
counts as absent. Nothing is followed and nothing is read.
"""

from __future__ import annotations

import errno
import os
from collections.abc import Callable
from pathlib import Path

ABSENT = frozenset({errno.ENOENT})


def engaged(path: Path) -> bool:
    """Whether the kill switch at ``path`` is engaged; any doubt means yes."""
    try:
        os.lstat(path)
    except OSError as error:
        return error.errno not in ABSENT
    return True


def checker(path: Path | None) -> Callable[[], bool]:
    """A check bound to ``path``; never engaged when no file is configured."""
    if path is None:
        return lambda: False
    return lambda: engaged(path)
