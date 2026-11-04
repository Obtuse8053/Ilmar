# SPDX-License-Identifier: Apache-2.0
"""Deterministic, safe names derived from task and execution identifiers (ADR-0012).

Task keys come from issue trackers and are untrusted text. Everything derived
from them here uses only ``[A-Za-z0-9._-]``, cannot traverse directories, and
is collision-resistant: two different tasks never map to the same name, because
every slug ends in a hash of the full task reference.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata

from ilmar.domain.tasks import TaskRef

_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
_UNSAFE = re.compile(r"[^A-Za-z0-9._-]+")
_DOTS = re.compile(r"\.{2,}")
_HASH_LENGTH = 12
MAX_SLUG_LENGTH = 48
CONTAINER_NAME_LIMIT = 128


def safe_identifier(value: str, what: str) -> str:
    """Validate an identifier Ilmar generated itself (execution id, instance id).

    Identifiers are used verbatim in paths and resource names, so they must be
    short and contain no dots, slashes or leading punctuation.
    """
    if not _IDENTIFIER.match(value):
        raise ValueError(f"{what} must match [A-Za-z0-9][A-Za-z0-9_-]{{0,63}}, got {value!r}")
    return value


def task_slug(task: TaskRef, *, max_length: int = MAX_SLUG_LENGTH) -> str:
    """A readable, filesystem- and git-safe slug that is unique per task.

    Format: ``<readable-part>-<12 hex of sha256(str(task))>``. The readable part
    is the NFKC-normalised key with unsafe characters replaced by ``_``, runs of
    dots collapsed, and leading or trailing punctuation removed; it may be empty.
    """
    if max_length < _HASH_LENGTH + 2:
        raise ValueError("max_length too small for a unique slug")
    digest = hashlib.sha256(str(task).encode()).hexdigest()[:_HASH_LENGTH]
    readable = unicodedata.normalize("NFKC", task.key)
    readable = _UNSAFE.sub("_", readable)
    readable = _DOTS.sub(".", readable)
    readable = readable[: max_length - _HASH_LENGTH - 1].strip("._-")
    return f"{readable}-{digest}" if readable else digest


def branch_name(task: TaskRef, *, prefix: str = "ilmar") -> str:
    """The task branch, e.g. ``ilmar/Obtuse8053_Ilmar_7-1a2b3c4d5e6f``."""
    safe_identifier(prefix, "branch prefix")
    return f"{prefix}/{task_slug(task)}"


def container_name(instance_id: str, execution_id: str, attempt: int) -> str:
    """``ilmar-<instance>-<execution>-<attempt>``: unique per execution attempt."""
    safe_identifier(instance_id, "instance id")
    safe_identifier(execution_id, "execution id")
    if attempt < 1:
        raise ValueError("attempt starts at 1")
    name = f"ilmar-{instance_id}-{execution_id}-{attempt}"
    if len(name) > CONTAINER_NAME_LIMIT:
        raise ValueError(f"container name longer than {CONTAINER_NAME_LIMIT}: {name!r}")
    return name


def verification_container_name(instance_id: str, execution_id: str, attempt: int, run: int) -> str:
    """``ilmar-<instance>-<execution>-<attempt>-verify-<run>``: one sandbox per verification run."""
    if run < 1:
        raise ValueError("run starts at 1")
    name = f"{container_name(instance_id, execution_id, attempt)}-verify-{run}"
    if len(name) > CONTAINER_NAME_LIMIT:
        raise ValueError(f"container name longer than {CONTAINER_NAME_LIMIT}: {name!r}")
    return name
