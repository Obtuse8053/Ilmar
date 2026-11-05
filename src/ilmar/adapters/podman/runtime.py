# SPDX-License-Identifier: Apache-2.0
"""Rootless Podman as an Ilmar ``ContainerRuntime``.

This adapter maps the provider-neutral sandbox profile of ADR-0011 onto
``podman`` command-line options. ADR-0014 records the mapping decisions. In
short, every sandbox:

* runs on **rootless** Podman (checked; rootful Podman is refused), as a
  non-root user (``--userns=keep-id:uid=…`` plus ``--user``), with all
  capabilities dropped, ``no-new-privileges`` and the default seccomp profile;
* has a read-only root filesystem (``--read-only --read-only-tmpfs=false``)
  with tmpfs only at ``/tmp`` and the home directory;
* has process, memory and CPU limits;
* mounts exactly one host directory, the workspace, and only after
  :func:`ilmar.core.mounts.authorize_mount` has confirmed it is the recorded
  directory. After the start, the identity of the mounted directory is checked
  again from the host through ``/proc/<pid>/root``. On a mismatch the
  container is removed;
* gets an environment built only from ``ContainerSpec.env`` and ``HOME``. Host
  and proxy variables are never inherited (``--env-host=false``,
  ``--http-proxy=false``), and values travel in a private ``--env-file``, not
  on the command line;
* has network ``none`` (only loopback, no routes, no Podman-managed
  ``/etc/hosts``). Under ``ALLOWLIST`` (ADR-0015) it sits alone on its own
  ``--internal`` network without DNS, IPv6 or hosts entries, whose only other
  member is an egress proxy sidecar (:mod:`egress_proxy`) that allows
  ``CONNECT <listed-host>:443`` only; ``HTTPS_PROXY`` points at it.
  ``UNRESTRICTED`` is refused unless the runtime was constructed with
  ``allow_unrestricted_network=True`` (a trusted or development deployment),
  and is then logged as a warning.
  ``ALLOWLIST`` arrives with the filtering proxy (ADR-0011 §5).

The ``podman`` process itself runs with a minimal environment
(:data:`PODMAN_ENVIRONMENT`), so no coordinator secret is visible to it.

Operations act on the full 64-hex container ID, never on a name. Replacing or
removing a container first checks its ownership labels (ADR-0012).

Removal is confirmed by inspecting the ID afterwards. If a container that had
to go (after a command timeout, or to be replaced) cannot be confirmed gone,
``ContainmentError`` is raised and its workspace is *quarantined*: no later
``start`` mounts that workspace until removal is confirmed (ADR-0014).
"""

from __future__ import annotations

import contextlib
import ipaddress
import json
import logging
import os
import re
import signal
import stat
import subprocess
import threading
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import IO, Any, Protocol

from ilmar.adapters.podman import egress_proxy
from ilmar.core.layout import WorkspaceLayout
from ilmar.core.mounts import authorize_mount
from ilmar.domain.errors import (
    ContainmentError,
    OwnershipConflictError,
    PolicyViolationError,
    ProviderError,
)
from ilmar.domain.workspace import (
    CommandResult,
    ContainerHandle,
    ContainerInfo,
    ContainerSpec,
    DirectoryIdentity,
    NetworkPolicy,
    ResourceLimits,
    ResourceOwner,
)

_LOG = logging.getLogger(__name__)

_CONTAINER_ID = re.compile(r"^[0-9a-f]{64}$")
_ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_IMAGE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/:@-]*$")
_UNSAFE_IN_MOUNT = re.compile(r"[,=\s]")

LABEL_MANAGED = "io.ilmar.managed"
LABEL_INSTANCE = "io.ilmar.owner.instance"
LABEL_EXECUTION = "io.ilmar.owner.execution"
LABEL_ATTEMPT = "io.ilmar.owner.attempt"
LABEL_TOKEN = "io.ilmar.owner.token"  # noqa: S105 - a label key, not a secret
LABEL_ROLE = "io.ilmar.role"
ROLE_SANDBOX = "sandbox"
ROLE_EGRESS_PROXY = "egress-proxy"
ROLE_EGRESS_NETWORK = "egress-network"
EGRESS_PROXY_SUFFIX = "-egress"
EGRESS_NETWORK_SUFFIX = "-net"
_PROXY_SCRIPT = "/ilmar-egress-proxy.py"
_MAX_ALLOWLIST_NAME = 120

PODMAN_ENVIRONMENT = (
    "PATH",
    "HOME",
    "USER",
    "LOGNAME",
    "LANG",
    "LC_ALL",
    "TMPDIR",
    "XDG_RUNTIME_DIR",
    "XDG_CONFIG_HOME",
    "XDG_DATA_HOME",
    "DBUS_SESSION_BUS_ADDRESS",
    "CONTAINERS_CONF",
    "CONTAINERS_STORAGE_CONF",
    "CONTAINERS_REGISTRIES_CONF",
)
"""The only variables of the coordinator's environment the ``podman`` process sees."""

TIMED_OUT_EXIT_CODE = -9
_DEFAULT_HOME = PurePosixPath("/home/ilmar")
_CONTAINER_TMP = "/tmp"  # noqa: S108 - a path inside the sandbox, not on the host
_CONTROL_TIMEOUT = 120.0


@dataclass(frozen=True, slots=True)
class Completed:
    """The outcome of one ``podman`` invocation."""

    returncode: int
    stdout: str
    stderr: str
    timed_out: bool = False


class Executor(Protocol):
    """Runs ``argv`` with exactly ``env`` and a timeout in seconds.

    ``pass_fds`` are inherited by the child under the same numbers: how values
    reach ``--env-file /dev/fd/N`` without a file on disk.
    """

    def __call__(
        self,
        argv: Sequence[str],
        env: Mapping[str, str],
        timeout: float,
        *,
        pass_fds: Sequence[int] = (),
    ) -> Completed: ...


MountProbe = Callable[[int, PurePosixPath], DirectoryIdentity]
"""Identity of ``path`` as seen inside the mount namespace of process ``pid``."""


OUTPUT_LIMIT_BYTES = 8 * 1024 * 1024
"""Bytes kept per stream of one ``podman`` invocation; the rest is drained and dropped.

Sandboxed commands control their output. Without a bound, a command that
prints without end within its timeout would exhaust the coordinator's memory.
"""


class _Capture:
    """Reads one pipe to its end, keeping at most ``limit`` bytes."""

    def __init__(self, stream: IO[bytes], limit: int) -> None:
        self.kept = bytearray()
        self.dropped = 0
        self._stream = stream
        self._limit = limit
        self.thread = threading.Thread(target=self._drain, daemon=True)
        self.thread.start()

    def _drain(self) -> None:
        read = getattr(self._stream, "read1", self._stream.read)
        while chunk := read(65536):
            room = max(0, self._limit - len(self.kept))
            self.kept += chunk[:room]
            self.dropped += len(chunk) - min(room, len(chunk))

    def text(self) -> str:
        data = bytes(self.kept)
        note = (
            f"\n[ilmar: {self.dropped} further bytes of output discarded]\n" if self.dropped else ""
        )
        return _decode(data) + note


def subprocess_executor(
    argv: Sequence[str],
    env: Mapping[str, str],
    timeout: float,
    *,
    pass_fds: Sequence[int] = (),
    output_limit: int = OUTPUT_LIMIT_BYTES,
) -> Completed:
    """Run a command in its own process group; on timeout, kill the whole group.

    Each output stream keeps at most ``output_limit`` bytes. Output is still
    read to the end, so the command never blocks on a full pipe.
    """
    try:
        process = subprocess.Popen(  # noqa: S603 - argv is built by this adapter
            list(argv),
            env=dict(env),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
            pass_fds=tuple(pass_fds),
        )
    except OSError as error:
        raise ProviderError(f"cannot run {argv[0]}: {error.strerror}") from None
    if process.stdout is None or process.stderr is None:  # pragma: no cover - PIPE was requested
        raise ProviderError(f"cannot read the output of {argv[0]}")
    captures = (_Capture(process.stdout, output_limit), _Capture(process.stderr, output_limit))
    deadline = time.monotonic() + timeout
    timed_out = False
    try:
        process.wait(timeout=timeout)
        for capture in captures:  # a leftover process may still hold the pipes
            capture.thread.join(max(0.0, deadline - time.monotonic()))
        timed_out = any(capture.thread.is_alive() for capture in captures)
    except subprocess.TimeoutExpired:
        timed_out = True
    if timed_out:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGKILL)
        with contextlib.suppress(subprocess.TimeoutExpired):
            process.wait(timeout=10)
        for capture in captures:
            capture.thread.join(10)
    for stream in (process.stdout, process.stderr):
        with contextlib.suppress(OSError):
            stream.close()
    out, err = (capture.text() for capture in captures)
    if timed_out:
        return Completed(TIMED_OUT_EXIT_CODE, out, err, timed_out=True)
    return Completed(process.returncode, out, err)


def proc_root_identity(pid: int, path: PurePosixPath) -> DirectoryIdentity:
    """Stat ``path`` inside the container through ``/proc/<pid>/root`` without following it."""
    info = os.lstat(f"/proc/{pid}/root{path}")
    if not stat.S_ISDIR(info.st_mode):
        raise OwnershipConflictError(f"{path} in the container is not a directory")
    return DirectoryIdentity(info.st_dev, info.st_ino)


@dataclass(frozen=True, slots=True)
class _Cleanup:
    """One removal still owed before a workspace may be mounted again."""

    kind: str  # "container" (ref is its ID) or "egress" (ref is the sandbox name)
    ref: str
    name: str
    owner: ResourceOwner | None = None

    @classmethod
    def egress(cls, spec: ContainerSpec) -> _Cleanup:
        return cls("egress", spec.name, spec.name, spec.owner)


@dataclass(frozen=True, slots=True)
class EgressSettings:
    """How ``ALLOWLIST`` sandboxes reach their listed hosts (ADR-0015).

    ``proxy_image`` must provide ``python3`` (3.11 or later); the proxy code is
    mounted read-only from this package. ``egress_network`` is the Podman
    network the proxy uses to reach the outside. ``extra_hosts`` pins hostnames
    to addresses in the proxy's ``/etc/hosts`` (for example a private LLM
    gateway); it never widens the allow-list.
    """

    proxy_image: str
    egress_network: str = "podman"
    proxy_port: int = 3128
    ready_timeout: float = 20.0
    extra_hosts: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not _IMAGE.match(self.proxy_image):
            raise ValueError(f"invalid proxy image reference: {self.proxy_image!r}")
        if not re.match(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$", self.egress_network):
            raise ValueError(f"invalid network name: {self.egress_network!r}")
        if not 1024 <= self.proxy_port <= 65535:
            raise ValueError("proxy_port must be an unprivileged port")
        pinned = {}
        for host, address in self.extra_hosts.items():
            name = egress_proxy.hostname(host)
            if name is None:
                raise ValueError(f"extra_hosts: not a hostname: {host!r}")
            pinned[name] = str(ipaddress.ip_address(address))
        object.__setattr__(self, "extra_hosts", pinned)


@dataclass(frozen=True, slots=True)
class SandboxProfile:
    """Deployment settings of the ADR-0011 profile that ``ContainerSpec`` leaves open."""

    uid: int = 1000
    gid: int = 1000
    home: PurePosixPath = _DEFAULT_HOME
    tmpfs_mb: int = 1024
    default_limits: ResourceLimits = field(
        default_factory=lambda: ResourceLimits(cpus=2.0, memory_mb=4096, max_processes=1024)
    )
    init: bool = True
    keepalive: tuple[str, ...] = ("sleep", "1000000000")

    def __post_init__(self) -> None:
        if self.uid < 1 or self.gid < 1:
            raise ValueError("the sandbox user must not be root (uid and gid >= 1)")
        if not self.home.is_absolute() or str(self.home) in {"/", _CONTAINER_TMP}:
            raise ValueError("home must be an absolute directory other than / and /tmp")
        if self.tmpfs_mb < 1:
            raise ValueError("tmpfs_mb must be positive")
        if not self.keepalive:
            raise ValueError("keepalive must name a long-running command")


class PodmanRuntime:
    """``ContainerRuntime`` backed by the rootless ``podman`` command line."""

    def __init__(
        self,
        *,
        layout: WorkspaceLayout,
        podman: str = "podman",
        name: str = "podman",
        profile: SandboxProfile | None = None,
        environ: Mapping[str, str] | None = None,
        executor: Executor = subprocess_executor,
        mount_probe: MountProbe = proc_root_identity,
        clock: Callable[[], float] = time.monotonic,
        allow_unrestricted_network: bool = False,
        egress: EgressSettings | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._layout = layout
        self._podman = podman
        self._name = name
        self._profile = profile or SandboxProfile()
        source = os.environ if environ is None else environ
        self._environ = {key: source[key] for key in PODMAN_ENVIRONMENT if key in source}
        self._executor = executor
        self._mount_probe = mount_probe
        self._clock = clock
        self._rootless_checked = False
        self._allow_unrestricted = allow_unrestricted_network
        self._egress = egress
        self._sleep = sleep
        # Workspace identity -> resources that must be confirmed gone before the
        # workspace may be mounted again (ADR-0014, ADR-0015).
        self._obligations: dict[DirectoryIdentity, set[_Cleanup]] = {}
        self._mounted: dict[str, DirectoryIdentity] = {}

    @property
    def name(self) -> str:
        return self._name

    # -- ContainerRuntime ------------------------------------------------

    def start(self, spec: ContainerSpec) -> ContainerHandle:
        self._require_rootless()
        self._check_network(spec)
        env = self._container_env(spec.env)
        if not _IMAGE.match(spec.image):
            raise ValueError(f"invalid image reference: {spec.image!r}")
        if _UNSAFE_IN_MOUNT.search(str(spec.host_workspace)):
            raise ValueError(f"workspace path not usable in a mount option: {spec.host_workspace}")

        self._release_quarantine(spec.workspace_identity, spec.name)
        existing = self.find(spec.name)
        if existing is not None:
            if existing.owner != spec.owner:
                raise OwnershipConflictError(f"container {spec.name} is owned by someone else")
            self._remove_sandbox(existing.handle.id, spec.name)
        else:  # leftovers of a crashed start
            self._discharge(spec.workspace_identity, [_Cleanup.egress(spec)], spec.name)

        network_args = self._network_arguments(spec.network, spec.name)
        if spec.network is NetworkPolicy.ALLOWLIST:
            try:
                proxy_url = self._start_egress(spec)
            except BaseException:
                self._discharge(spec.workspace_identity, [_Cleanup.egress(spec)], spec.name)
                raise
            env |= {"HTTPS_PROXY": proxy_url, "https_proxy": proxy_url}

        try:
            # Immediately before the mount: the actual directory, not just the path.
            authorize_mount(self._layout, spec.host_workspace, spec.workspace_identity)
            with self._env_file(env) as (env_file, fds):
                started = self._podman_call(
                    self._run_arguments(spec, env_file, network_args), pass_fds=fds
                )
            self._expect_success(started, "run")
        except BaseException:
            self._discharge(spec.workspace_identity, [_Cleanup.egress(spec)], spec.name)
            raise
        container_id = started.stdout.strip().splitlines()[-1] if started.stdout.strip() else ""
        if not _CONTAINER_ID.match(container_id):
            self._discharge(spec.workspace_identity, [_Cleanup.egress(spec)], spec.name)
            raise ProviderError("podman run did not report a container ID")
        self._mounted[container_id] = spec.workspace_identity
        try:
            self._verify_mount(container_id, spec)
        except BaseException:
            self._remove_sandbox(container_id, spec.name)
            raise
        return ContainerHandle(runtime=self._name, id=container_id, name=spec.name)

    def find(self, name: str) -> ContainerInfo | None:
        listed = self._podman_call(
            [
                "ps",
                "--all",
                "--no-trunc",
                "--filter",
                f"name=^{re.escape(name)}$",
                "--format",
                "json",
            ]
        )
        self._expect_success(listed, "ps")
        matches = [
            entry for entry in _json_list(listed.stdout, "ps") if name in (entry.get("Names") or [])
        ]
        if not matches:
            return None
        if len(matches) > 1:
            raise ProviderError(f"more than one container is named {name}")
        container_id = str(matches[0].get("Id", ""))
        details = self._inspect(container_id) if _CONTAINER_ID.match(container_id) else None
        if details is None:
            return None  # removed in between
        handle = ContainerHandle(runtime=self._name, id=container_id, name=name)
        return ContainerInfo(
            handle=handle,
            owner=_owner(details),
            running=bool(details.get("State", {}).get("Running")),
        )

    def run(
        self,
        handle: ContainerHandle,
        argv: Sequence[str],
        *,
        timeout_seconds: float,
        env: Mapping[str, str] | None = None,
    ) -> CommandResult:
        container_id = self._container_id(handle)
        if not argv:
            raise ValueError("argv must not be empty")
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        command_env = self._container_env(env or {}, with_home=False)
        if not self._running(container_id):
            raise ProviderError(f"container is not running: {handle.name}")

        began = self._clock()
        with self._env_file(command_env) as (env_file, fds):
            options = ["--env-file", env_file] if env_file else []
            done = self._executor(
                [self._podman, "exec", *options, container_id, *argv],
                self._environ,
                timeout_seconds,
                pass_fds=fds,
            )
        duration = max(0.0, self._clock() - began)
        if done.timed_out:
            # Processes the command started may still run; nothing may keep
            # writing to the workspace, so the whole container goes (ADR-0014).
            # If that cannot be confirmed, ContainmentError propagates.
            self._remove_sandbox(container_id, handle.name)
            return CommandResult(
                TIMED_OUT_EXIT_CODE, done.stdout, done.stderr, duration, timed_out=True
            )
        if done.returncode == 125 and not self._running(container_id):
            raise ProviderError(f"container stopped while running a command: {handle.name}")
        return CommandResult(done.returncode, done.stdout, done.stderr, duration)

    def is_running(self, handle: ContainerHandle) -> bool:
        return self._running(self._container_id(handle))

    def stop(self, handle: ContainerHandle, *, owner: ResourceOwner) -> None:
        container_id = self._container_id(handle)
        details = self._inspect(container_id)
        if details is None:
            return
        if _owner(details) != owner:
            raise OwnershipConflictError(f"container {handle.name} is owned by someone else")
        self._remove_sandbox(container_id, handle.name)

    def remove_owned(self, name: str, *, owner: ResourceOwner) -> bool:
        """See the port. Needs no in-memory state: labels identify the owner."""
        try:
            container = self.find(name)
            proxy = self.find(name + EGRESS_PROXY_SUFFIX)
        except ProviderError as error:
            if "more than one" in str(error):
                raise OwnershipConflictError(f"{name} is ambiguous: {error}") from None
            raise
        network = name + EGRESS_NETWORK_SUFFIX
        network_exists = self._podman_call(["network", "exists", network]).returncode == 0
        network_owner, network_id = None, ""
        if network_exists:
            details = self._network(network)
            labels = details.get("labels") or {}
            network_owner = _owner({"Config": {"Labels": labels}})
            # Removed by the ID read together with its labels, never by name: a
            # network replaced in between is someone else's and is left alone.
            network_id = str(details.get("id") or "")
            if not _CONTAINER_ID.match(network_id):
                raise OwnershipConflictError(f"{network} has no verifiable ID")
        # Ownership is checked for everything before anything is removed.
        for found, what in ((container, name), (proxy, name + EGRESS_PROXY_SUFFIX)):
            if found is not None and found.owner != owner:
                raise OwnershipConflictError(f"{what} is not owned by the recorded execution")
        if network_exists and network_owner != owner:
            raise OwnershipConflictError(f"{network} is not owned by the recorded execution")
        if container is not None:
            self._remove(container.handle.id, name)
            self._mounted.pop(container.handle.id, None)
        if proxy is not None:
            self._remove(proxy.handle.id, proxy.handle.name)
        if network_exists:
            removed = self._podman_call(["network", "rm", network_id])
            if self._podman_call(["network", "exists", network_id]).returncode == 0:
                detail = removed.stderr.strip().splitlines()[-1:] or [f"exit {removed.returncode}"]
                raise ContainmentError(
                    f"network {network} could not be confirmed removed: {detail[0][:200]}",
                    container_id=container.handle.id if container else "",
                    name=name,
                )
        for workspace, owed in list(self._obligations.items()):  # nothing is owed any more
            owed.difference_update({c for c in owed if c.name == name})
            if not owed:
                self._obligations.pop(workspace, None)
        return container is not None or proxy is not None or network_exists

    # -- podman arguments --------------------------------------------------

    def _run_arguments(
        self, spec: ContainerSpec, env_file: str | None, network_args: Sequence[str]
    ) -> list[str]:
        profile = self._profile
        limits = _merge_limits(spec.limits, profile.default_limits)
        workdir = spec.container_workdir
        args = [
            "run",
            "--detach",
            "--name",
            spec.name,
            "--pull=never",
            f"--userns=keep-id:uid={profile.uid},gid={profile.gid}",
            "--user",
            f"{profile.uid}:{profile.gid}",
            "--cap-drop=all",
            "--security-opt=no-new-privileges",
            "--read-only",
            "--read-only-tmpfs=false",
            "--tmpfs",
            f"{_CONTAINER_TMP}:rw,nosuid,nodev,mode=1777,size={profile.tmpfs_mb}m",
            "--tmpfs",
            f"{profile.home}:rw,nosuid,nodev,mode=1777,size={profile.tmpfs_mb}m",
            "--env-host=false",
            "--http-proxy=false",
            "--workdir",
            str(workdir),
            "--mount",
            f"type=bind,source={spec.host_workspace},destination={workdir}",
            "--log-driver=none",
            "--hostname=sandbox",
        ]
        if env_file:
            args += ["--env-file", env_file]
        args += network_args
        if limits.max_processes is not None:
            args += ["--pids-limit", str(limits.max_processes)]
        if limits.memory_mb is not None:
            args += ["--memory", f"{limits.memory_mb}m"]
        if limits.cpus is not None:
            args += ["--cpus", f"{limits.cpus:g}"]
        if profile.init:
            args.append("--init")
        for key, value in (_owner_labels(spec.owner) | {LABEL_ROLE: ROLE_SANDBOX}).items():
            args += ["--label", f"{key}={value}"]
        return [*args, spec.image, *profile.keepalive]

    @staticmethod
    def _network_arguments(network: NetworkPolicy, name: str) -> list[str]:
        # Podman's default containers.conf allows unprivileged ICMP sockets for
        # group 0 (ping_group_range "0 0"); disable them entirely.
        no_icmp = ["--sysctl", "net.ipv4.ping_group_range=1 0"]
        if network is NetworkPolicy.NONE:
            return ["--network=none", "--no-hosts", *no_icmp]
        if network is NetworkPolicy.ALLOWLIST:
            return [
                "--network",
                name + EGRESS_NETWORK_SUFFIX,
                "--dns=none",
                "--no-hosts",
                "--sysctl",
                "net.ipv6.conf.all.disable_ipv6=1",
                "--sysctl",
                "net.ipv6.conf.default.disable_ipv6=1",
                *no_icmp,
            ]
        return []  # UNRESTRICTED: Podman's default network (opted in)

    def _container_env(self, env: Mapping[str, str], *, with_home: bool = True) -> dict[str, str]:
        result = {"HOME": str(self._profile.home)} if with_home else {}
        for key, value in env.items():
            if not _ENV_NAME.match(key):
                raise ValueError(f"invalid environment variable name: {key!r}")
            if "\n" in value or "\r" in value or "\0" in value:
                raise ValueError(f"environment variable {key} must be a single line")
            result[key] = value
        return result

    def _check_network(self, spec: ContainerSpec) -> None:
        network = spec.network
        if network is NetworkPolicy.ALLOWLIST:
            if self._egress is None:
                raise ProviderError("ALLOWLIST needs EgressSettings for this runtime")
            if len(spec.name) > _MAX_ALLOWLIST_NAME:
                raise ValueError(f"container name too long for ALLOWLIST: {spec.name}")
            return
        if network is NetworkPolicy.UNRESTRICTED:
            if not self._allow_unrestricted:
                raise PolicyViolationError(
                    "unrestricted network access is not permitted by this deployment "
                    "(PodmanRuntime(allow_unrestricted_network=True) is a trusted opt-in)"
                )
            _LOG.warning("starting a sandbox with UNRESTRICTED network access (ADR-0011)")
        elif network is not NetworkPolicy.NONE:
            raise ProviderError(f"network policy {network.value} is not supported yet")

    # -- podman calls ------------------------------------------------------

    def _podman_call(self, args: Sequence[str], *, pass_fds: Sequence[int] = ()) -> Completed:
        done = self._executor(
            [self._podman, *args], self._environ, _CONTROL_TIMEOUT, pass_fds=pass_fds
        )
        if done.timed_out:
            raise ProviderError(f"podman {args[0]} timed out")
        return done

    def _require_rootless(self) -> None:
        if self._rootless_checked:
            return
        info = self._podman_call(["info", "--format", "{{.Host.Security.Rootless}}"])
        self._expect_success(info, "info")
        if info.stdout.strip() != "true":
            raise ProviderError("Ilmar requires rootless Podman (ADR-0011)")
        self._rootless_checked = True

    def _inspect(self, container_id: str) -> dict[str, Any] | None:
        done = self._podman_call(["container", "inspect", container_id])
        if done.returncode != 0:
            if "no such" in done.stderr.lower():
                return None
            self._expect_success(done, "container inspect")
        entries = _json_list(done.stdout, "container inspect")
        if not entries or entries[0].get("Id") != container_id:
            return None
        return entries[0]

    def _running(self, container_id: str) -> bool:
        details = self._inspect(container_id)
        return details is not None and bool(details.get("State", {}).get("Running"))

    def _remove(self, container_id: str, name: str) -> None:
        """Remove ``container_id`` and confirm it is gone; ``ContainmentError`` otherwise."""
        try:
            done = self._executor(
                [self._podman, "rm", "--force", "--time", "0", container_id],
                self._environ,
                _CONTROL_TIMEOUT,
            )
            gone = self._inspect(container_id) is None
        except ProviderError:
            done, gone = None, False
        if gone:
            return
        reason = "timed out" if done is None or done.timed_out else f"exit {done.returncode}"
        _LOG.error("could not confirm removal of %s (%s): %s", name, container_id, reason)
        raise ContainmentError(
            f"container {name} could not be confirmed removed ({reason}); its workspace "
            "must not be reused until removal is confirmed",
            container_id=container_id,
            name=name,
        )

    def _remove_sandbox(self, container_id: str, name: str) -> None:
        """Remove a sandbox and its egress sidecar; quarantine the workspace until all are gone."""
        details = self._inspect(container_id)
        owner = _owner(details) if details is not None else None
        cleanups = [_Cleanup("container", container_id, name)]
        if owner is not None:
            cleanups.append(_Cleanup("egress", name, name, owner))
        self._discharge(self._mounted.get(container_id), cleanups, name)

    def _discharge(
        self, workspace: DirectoryIdentity | None, cleanups: Sequence[_Cleanup], name: str
    ) -> None:
        """Carry out ``cleanups`` in order; any that fail keep ``workspace`` quarantined.

        Each obligation is recorded *before* it is attempted and dropped only once
        its resources are confirmed gone. The first failure raises
        ``ContainmentError``; the remaining obligations stay recorded.
        """
        pending = self._obligations.setdefault(workspace, set()) if workspace else set()
        pending.update(cleanups)
        try:
            for cleanup in sorted(cleanups, key=lambda c: c.kind != "container"):
                try:
                    if cleanup.kind == "container":
                        self._remove(cleanup.ref, cleanup.name)
                        self._mounted.pop(cleanup.ref, None)
                    elif cleanup.owner is not None:
                        self._remove_sidecars(cleanup.ref, cleanup.owner)
                except ContainmentError:
                    raise
                except ProviderError as error:
                    raise ContainmentError(
                        f"egress resources of {cleanup.ref} could not be confirmed removed "
                        f"({error}); its workspace must not be reused until they are",
                        container_id=cleanup.ref,
                        name=name,
                    ) from error
                pending.discard(cleanup)
        finally:
            if workspace is not None and not pending:
                self._obligations.pop(workspace, None)

    # -- ALLOWLIST egress sidecar (ADR-0015) ---------------------------------

    def _start_egress(self, spec: ContainerSpec) -> str:
        """Create the internal network and the proxy; return the proxy URL."""
        settings = self._egress
        if settings is None:
            raise ProviderError("ALLOWLIST needs EgressSettings for this runtime")
        network = spec.name + EGRESS_NETWORK_SUFFIX
        proxy_name = spec.name + EGRESS_PROXY_SUFFIX
        labels = _owner_labels(spec.owner)

        create = ["network", "create", "--internal", "--disable-dns"]
        for key, value in (labels | {LABEL_ROLE: ROLE_EGRESS_NETWORK}).items():
            create += ["--label", f"{key}={value}"]
        self._expect_success(self._podman_call([*create, network]), "network create")
        subnets = [
            str(entry.get("subnet", ""))
            for entry in self._network(network).get("subnets") or []
            if ":" not in str(entry.get("subnet", ""))
        ]
        if not subnets or not all(subnets):
            raise ProviderError(f"network {network} has no IPv4 subnet")

        script = Path(egress_proxy.__file__).resolve()
        if _UNSAFE_IN_MOUNT.search(str(script)):
            raise ProviderError(f"proxy script path not usable in a mount option: {script}")
        args = [
            "run",
            "--detach",
            "--name",
            proxy_name,
            "--pull=never",
            "--user",
            "65534:65534",
            "--cap-drop=all",
            "--security-opt=no-new-privileges",
            "--read-only",
            "--read-only-tmpfs=false",
            "--env-host=false",
            "--http-proxy=false",
            "--network",
            network,
            "--network",
            settings.egress_network,
            "--mount",
            f"type=bind,source={script},destination={_PROXY_SCRIPT},ro=true",
            "--pids-limit",
            "256",
            "--memory",
            "256m",
            "--log-driver=k8s-file",
            "--init",
        ]
        for host, address in sorted(settings.extra_hosts.items()):
            args += ["--add-host", f"{host}:{address}"]
        for key, value in (labels | {LABEL_ROLE: ROLE_EGRESS_PROXY}).items():
            args += ["--label", f"{key}={value}"]
        args += [
            settings.proxy_image,
            "python3",
            "-I",
            _PROXY_SCRIPT,
            "--listen",
            f"0.0.0.0:{settings.proxy_port}",
            "--allow",
            ",".join(spec.allowed_hosts),
            "--clients",
            ",".join(subnets),
        ]
        started = self._podman_call(args)
        self._expect_success(started, "run (egress proxy)")
        proxy_id = started.stdout.strip().splitlines()[-1] if started.stdout.strip() else ""
        if not _CONTAINER_ID.match(proxy_id):
            raise ProviderError("podman run did not report the egress proxy's ID")
        address = self._wait_for_proxy(proxy_id, network, settings.ready_timeout)
        return f"http://{address}:{settings.proxy_port}"

    def _wait_for_proxy(self, proxy_id: str, network: str, timeout: float) -> str:
        deadline = self._clock() + timeout
        while True:
            logs = self._podman_call(["logs", proxy_id])
            ready = "ilmar-egress READY" in logs.stdout + logs.stderr
            details = self._inspect(proxy_id)
            if details is None or not details.get("State", {}).get("Running"):
                raise ProviderError("the egress proxy exited during start")
            if ready:
                networks = (details.get("NetworkSettings") or {}).get("Networks") or {}
                address = str((networks.get(network) or {}).get("IPAddress") or "")
                if not re.match(r"^\d{1,3}(\.\d{1,3}){3}$", address):
                    raise ProviderError("the egress proxy has no IPv4 address on its network")
                return address
            if self._clock() >= deadline:
                raise ProviderError("the egress proxy did not become ready")
            self._sleep(0.2)

    def _network(self, name: str) -> dict[str, Any]:
        done = self._podman_call(["network", "inspect", name])
        self._expect_success(done, "network inspect")
        entries = _json_list(done.stdout, "network inspect")
        if len(entries) != 1:
            raise ProviderError(f"network {name} not found")
        return entries[0]

    def _remove_sidecars(self, name: str, owner: ResourceOwner) -> None:
        """Remove the egress proxy and network of sandbox ``name`` if ``owner`` owns them."""
        proxy = self.find(name + EGRESS_PROXY_SUFFIX)
        if proxy is not None:
            if proxy.owner != owner:
                _LOG.warning("not removing %s: owned by someone else", proxy.handle.name)
            else:
                self._remove(proxy.handle.id, proxy.handle.name)
        network = name + EGRESS_NETWORK_SUFFIX
        if self._podman_call(["network", "exists", network]).returncode != 0:
            return
        labels = self._network(network).get("labels") or {}
        if _owner({"Config": {"Labels": labels}}) != owner:
            _LOG.warning("not removing network %s: owned by someone else", network)
            return
        removed = self._podman_call(["network", "rm", network])
        if self._podman_call(["network", "exists", network]).returncode == 0:
            detail = removed.stderr.strip().splitlines()[-1:] or [f"exit {removed.returncode}"]
            raise ProviderError(f"network {network} could not be removed: {detail[0][:200]}")

    def _release_quarantine(self, workspace: DirectoryIdentity, name: str) -> None:
        """Retry every cleanup still owed for ``workspace``; refuse it if any remain."""
        owed = list(self._obligations.get(workspace, ()))
        if owed:
            self._discharge(workspace, owed, name)

    def _verify_mount(self, container_id: str, spec: ContainerSpec) -> None:
        details = self._inspect(container_id)
        state = (details or {}).get("State", {})
        pid = state.get("Pid")
        if not state.get("Running") or not isinstance(pid, int) or pid <= 0:
            raise ProviderError(f"container {spec.name} did not stay running")
        try:
            mounted = self._mount_probe(pid, spec.container_workdir)
        except OSError as error:
            raise ProviderError(f"cannot check the mounted workspace: {error.strerror}") from None
        if mounted != spec.workspace_identity:
            raise OwnershipConflictError(
                f"the directory mounted in {spec.name} is not the recorded workspace"
            )

    def _container_id(self, handle: ContainerHandle) -> str:
        if handle.runtime != self._name or not _CONTAINER_ID.match(handle.id):
            raise ProviderError(f"not a {self._name} container handle: {handle.id!r}")
        return handle.id

    @staticmethod
    def _expect_success(done: Completed, what: str) -> None:
        if done.returncode != 0:
            detail = done.stderr.strip().splitlines()[-1:] or ["no error output"]
            raise ProviderError(f"podman {what} failed: {detail[0][:300]}")

    @contextlib.contextmanager
    def _env_file(self, env: Mapping[str, str]) -> Iterator[tuple[str | None, tuple[int, ...]]]:
        """Pass values as ``--env-file /dev/fd/N`` backed by memory, never by a file.

        Not on a command line others can read, and never on disk: a memfd has
        no path, and the kernel frees it when the last descriptor closes, also
        when the coordinator is killed mid-command (review for #11d; before,
        a crash left the model key in ``$TMPDIR``). Yields the path and the
        descriptor podman must inherit.
        """
        if not env:
            yield None, ()
            return
        fd = os.memfd_create("ilmar-env", os.MFD_CLOEXEC)  # CLOEXEC: only podman gets it
        try:
            os.write(fd, "".join(f"{key}={value}\n" for key, value in env.items()).encode())
            os.lseek(fd, 0, os.SEEK_SET)
            yield f"/dev/fd/{fd}", (fd,)
        finally:
            os.close(fd)


def _merge_limits(requested: ResourceLimits, defaults: ResourceLimits) -> ResourceLimits:
    return ResourceLimits(
        cpus=requested.cpus if requested.cpus is not None else defaults.cpus,
        memory_mb=requested.memory_mb if requested.memory_mb is not None else defaults.memory_mb,
        max_processes=(
            requested.max_processes
            if requested.max_processes is not None
            else defaults.max_processes
        ),
    )


def _owner_labels(owner: ResourceOwner) -> dict[str, str]:
    return {
        LABEL_MANAGED: "true",
        LABEL_INSTANCE: owner.instance_id,
        LABEL_EXECUTION: owner.execution_id,
        LABEL_ATTEMPT: str(owner.attempt),
        LABEL_TOKEN: owner.token,
    }


def _owner(details: Mapping[str, Any]) -> ResourceOwner | None:
    labels = (details.get("Config") or {}).get("Labels") or {}
    try:
        return ResourceOwner(
            instance_id=str(labels[LABEL_INSTANCE]),
            execution_id=str(labels[LABEL_EXECUTION]),
            attempt=int(labels[LABEL_ATTEMPT]),
            token=str(labels[LABEL_TOKEN]),
        )
    except (KeyError, ValueError, TypeError):
        return None


def _json_list(text: str, what: str) -> list[dict[str, Any]]:
    try:
        data = json.loads(text or "[]")
    except json.JSONDecodeError:
        raise ProviderError(f"podman {what} returned invalid JSON") from None
    if not isinstance(data, list) or not all(isinstance(item, dict) for item in data):
        raise ProviderError(f"podman {what} returned unexpected JSON")
    return data


def _decode(data: bytes | None) -> str:
    return (data or b"").decode("utf-8", errors="replace")
