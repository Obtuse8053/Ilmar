# SPDX-License-Identifier: Apache-2.0
"""Isolated execution environments (containers) and commands run inside them."""

from __future__ import annotations

import enum
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

DEFAULT_CONTAINER_WORKDIR = PurePosixPath("/workspace")
_RESOURCE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")


class NetworkPolicy(enum.Enum):
    """Network access granted to a container.

    An allow-list mode (egress only to named hosts) is planned once the first
    real runtime adapter exists; see ADR-0006.
    """

    NONE = "none"
    UNRESTRICTED = "unrestricted"


@dataclass(frozen=True, slots=True)
class ResourceLimits:
    """Upper bounds on container resources. ``None`` means the runtime default."""

    cpus: float | None = None
    memory_mb: int | None = None
    max_processes: int | None = None

    def __post_init__(self) -> None:
        for name in ("cpus", "memory_mb", "max_processes"):
            value = getattr(self, name)
            if value is not None and value <= 0:
                raise ValueError(f"{name} must be positive")


@dataclass(frozen=True, slots=True)
class ResourceOwner:
    """Who owns a runtime resource (ADR-0012).

    The same values are recorded in the coordinator's resource ledger and
    attached to the resource as labels. A resource may be replaced, stopped or
    removed only by a caller presenting an equal owner.
    """

    instance_id: str
    execution_id: str
    attempt: int
    token: str

    def __post_init__(self) -> None:
        if not (self.instance_id and self.execution_id and self.token):
            raise ValueError("instance_id, execution_id and token must be non-empty")
        if self.attempt < 1:
            raise ValueError("attempt starts at 1")


@dataclass(frozen=True, slots=True)
class ContainerSpec:
    """How to start an isolated workspace.

    ``host_workspace`` is a directory on the coordinator's host that is mounted
    read-write at ``container_workdir``. Nothing else from the host is shared.
    ``name`` is derived deterministically by the core (ADR-0012); ``owner`` is
    attached to the container so that only its owner can replace or remove it.
    """

    image: str
    host_workspace: Path
    name: str
    owner: ResourceOwner
    container_workdir: PurePosixPath = DEFAULT_CONTAINER_WORKDIR
    env: Mapping[str, str] = field(default_factory=dict)
    network: NetworkPolicy = NetworkPolicy.NONE
    limits: ResourceLimits = field(default_factory=ResourceLimits)

    def __post_init__(self) -> None:
        if not _RESOURCE_NAME.match(self.name):
            raise ValueError(f"invalid container name: {self.name!r}")


@dataclass(frozen=True, slots=True)
class ContainerHandle:
    """Refers to one specific container.

    ``id`` is the runtime's immutable identifier and is what operations act on;
    ``name`` is the deterministic name it was created under. A new container
    with the same name has a different ``id``.
    """

    runtime: str
    id: str
    name: str


@dataclass(frozen=True, slots=True)
class ContainerInfo:
    """What the runtime reports about a container found by name."""

    handle: ContainerHandle
    owner: ResourceOwner | None
    """``None`` when the container carries no (or incomplete) ownership labels."""
    running: bool


@dataclass(frozen=True, slots=True)
class CommandResult:
    exit_code: int
    stdout: str
    stderr: str
    duration_seconds: float
    timed_out: bool = False

    @property
    def succeeded(self) -> bool:
        return self.exit_code == 0 and not self.timed_out
