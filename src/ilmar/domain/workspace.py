# SPDX-License-Identifier: Apache-2.0
"""Isolated execution environments (containers) and commands run inside them."""

from __future__ import annotations

import enum
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Protocol

DEFAULT_CONTAINER_WORKDIR = PurePosixPath("/workspace")
_RESOURCE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")


class NetworkPolicy(enum.Enum):
    """Network access granted to a container (ADR-0011 §5).

    * ``NONE``: no network at all (the default).
    * ``ALLOWLIST``: HTTPS (``CONNECT host:443``) through a filtering proxy to
      the hostnames in ``ContainerSpec.allowed_hosts`` only.
    * ``UNRESTRICTED``: needs an explicit deployment opt-in (ADR-0014).
    """

    NONE = "none"
    ALLOWLIST = "allowlist"
    UNRESTRICTED = "unrestricted"


_HOST_LABEL = re.compile(r"^(?!-)[a-z0-9-]{1,63}(?<!-)$")


def normalise_hostname(host: str) -> str:
    """A lowercase DNS hostname without a trailing dot; ``ValueError`` otherwise.

    IP literals are refused: an allow-list names hosts, never addresses.
    """
    name = host.strip().lower().removesuffix(".")
    labels = name.split(".")
    if (
        not name
        or len(name) > 253
        or len(labels) < 2
        or not all(_HOST_LABEL.match(label) for label in labels)
        or labels[-1].isdigit()
    ):
        raise ValueError(f"not a valid hostname: {host!r}")
    return name


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
class DirectoryIdentity:
    """Immutable identity of a host directory: survives renames, changes if replaced."""

    device: int
    inode: int


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
    ``workspace_identity`` is the identity the resource ledger recorded for it;
    a runtime refuses to mount a directory with any other identity (ADR-0012).
    ``name`` is derived deterministically by the core (ADR-0012); ``owner`` is
    attached to the container so that only its owner can replace or remove it.
    """

    image: str
    host_workspace: Path
    name: str
    owner: ResourceOwner
    workspace_identity: DirectoryIdentity
    container_workdir: PurePosixPath = DEFAULT_CONTAINER_WORKDIR
    env: Mapping[str, str] = field(default_factory=dict)
    """The container's own environment. Runtimes store it with the container's
    definition (Podman: in its database, shown by ``inspect``), so it never holds a
    secret; per-command values go to ``ContainerRuntime.run(env=...)`` (ADR-0023)."""
    network: NetworkPolicy = NetworkPolicy.NONE
    allowed_hosts: tuple[str, ...] = ()
    """Hostnames reachable on port 443 under ``ALLOWLIST``; empty for other policies."""
    limits: ResourceLimits = field(default_factory=ResourceLimits)

    def __post_init__(self) -> None:
        if not _RESOURCE_NAME.match(self.name):
            raise ValueError(f"invalid container name: {self.name!r}")
        hosts = tuple(sorted({normalise_hostname(host) for host in self.allowed_hosts}))
        if self.network is NetworkPolicy.ALLOWLIST and not hosts:
            raise ValueError("ALLOWLIST needs at least one allowed host")
        if self.network is not NetworkPolicy.ALLOWLIST and hosts:
            raise ValueError("allowed_hosts is only meaningful with NetworkPolicy.ALLOWLIST")
        object.__setattr__(self, "allowed_hosts", hosts)


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


class StopSignal(Protocol):
    """A read-only request to stop a running command (ADR-0025 §2).

    ``threading.Event`` satisfies it. A runtime only reads it; whoever holds the
    event sets it.
    """

    def is_set(self) -> bool: ...


STOP_POLL_SECONDS = 0.25
"""How often a runtime looks at a stop signal while a command runs (at most 1 s)."""


@dataclass(frozen=True, slots=True)
class CommandResult:
    """How one command in a sandbox ended.

    ``timed_out``, ``cancelled`` (a stop signal, ADR-0025 §2), ``stalled`` (no
    progress for the idle timeout, ADR-0035 §3) and ``rejected`` (its output
    stream broke the consumer's rules, ADR-0035 §4) all mean the command was
    ended and its whole sandbox removed; at most one is set.
    """

    exit_code: int
    stdout: str
    stderr: str
    duration_seconds: float
    timed_out: bool = False
    cancelled: bool = False
    stalled: bool = False
    rejected: bool = False

    def __post_init__(self) -> None:
        if sum((self.timed_out, self.cancelled, self.stalled, self.rejected)) > 1:
            raise ValueError(
                "a command timed out, was cancelled, stalled or was rejected: at most one"
            )

    @property
    def ended_early(self) -> bool:
        """Killed before it finished: its sandbox is gone (port contract)."""
        return self.timed_out or self.cancelled or self.stalled or self.rejected

    @property
    def succeeded(self) -> bool:
        return self.exit_code == 0 and not self.ended_early


class StreamVerdict(enum.Enum):
    """What a stream consumer made of the output it was just given (ADR-0035 §8)."""

    NONE = "none"
    """Nothing new: no complete line, or only lines that are not progress."""
    PROGRESS = "progress"
    """At least one complete line that counts as progress: restart the idle timer."""
    VIOLATION = "violation"
    """The stream broke the consumer's rules: end the command now."""
