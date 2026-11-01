# SPDX-License-Identifier: Apache-2.0
"""Isolated execution environments (containers) and commands run inside them."""

from __future__ import annotations

import enum
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

DEFAULT_CONTAINER_WORKDIR = PurePosixPath("/workspace")


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
class ContainerSpec:
    """How to start an isolated workspace.

    ``host_workspace`` is a directory on the coordinator's host that is mounted
    read-write at ``container_workdir``. Nothing else from the host is shared.
    """

    image: str
    host_workspace: Path
    container_workdir: PurePosixPath = DEFAULT_CONTAINER_WORKDIR
    env: Mapping[str, str] = field(default_factory=dict)
    network: NetworkPolicy = NetworkPolicy.NONE
    limits: ResourceLimits = field(default_factory=ResourceLimits)


@dataclass(frozen=True, slots=True)
class ContainerHandle:
    """Refers to a running container. ``id`` is opaque to the core."""

    runtime: str
    id: str


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
