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
  ``/etc/hosts``) unless the spec asks for ``UNRESTRICTED``, which is logged as
  a warning.
  ``ALLOWLIST`` arrives with the filtering proxy (ADR-0011 §5).

The ``podman`` process itself runs with a minimal environment
(:data:`PODMAN_ENVIRONMENT`), so no coordinator secret is visible to it.

Operations act on the full 64-hex container ID, never on a name. Replacing or
removing a container first checks its ownership labels (ADR-0012).
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import re
import signal
import stat
import subprocess
import tempfile
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any

from ilmar.core.layout import WorkspaceLayout
from ilmar.core.mounts import authorize_mount
from ilmar.domain.errors import OwnershipConflictError, ProviderError
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


Executor = Callable[[Sequence[str], Mapping[str, str], float], Completed]
"""Runs ``argv`` with exactly ``env`` and a timeout in seconds."""

MountProbe = Callable[[int, PurePosixPath], DirectoryIdentity]
"""Identity of ``path`` as seen inside the mount namespace of process ``pid``."""


def subprocess_executor(argv: Sequence[str], env: Mapping[str, str], timeout: float) -> Completed:
    """Run a command in its own process group; on timeout, kill the whole group."""
    try:
        process = subprocess.Popen(  # noqa: S603 - argv is built by this adapter
            list(argv),
            env=dict(env),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
    except OSError as error:
        raise ProviderError(f"cannot run {argv[0]}: {error.strerror}") from None
    try:
        out, err = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGKILL)
        try:
            out, err = process.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            out, err = b"", b""
        return Completed(TIMED_OUT_EXIT_CODE, _decode(out), _decode(err), timed_out=True)
    return Completed(process.returncode, _decode(out), _decode(err))


def proc_root_identity(pid: int, path: PurePosixPath) -> DirectoryIdentity:
    """Stat ``path`` inside the container through ``/proc/<pid>/root`` without following it."""
    info = os.lstat(f"/proc/{pid}/root{path}")
    if not stat.S_ISDIR(info.st_mode):
        raise OwnershipConflictError(f"{path} in the container is not a directory")
    return DirectoryIdentity(info.st_dev, info.st_ino)


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

    @property
    def name(self) -> str:
        return self._name

    # -- ContainerRuntime ------------------------------------------------

    def start(self, spec: ContainerSpec) -> ContainerHandle:
        self._require_rootless()
        self._check_network(spec.network)
        env = self._container_env(spec.env)
        if not _IMAGE.match(spec.image):
            raise ValueError(f"invalid image reference: {spec.image!r}")
        if _UNSAFE_IN_MOUNT.search(str(spec.host_workspace)):
            raise ValueError(f"workspace path not usable in a mount option: {spec.host_workspace}")

        existing = self.find(spec.name)
        if existing is not None:
            if existing.owner != spec.owner:
                raise OwnershipConflictError(f"container {spec.name} is owned by someone else")
            self._remove(existing.handle.id)

        # Immediately before the mount: the actual directory, not just the path.
        authorize_mount(self._layout, spec.host_workspace, spec.workspace_identity)
        with self._env_file(env) as env_file:
            started = self._podman_call(self._run_arguments(spec, env_file))
        self._expect_success(started, "run")
        container_id = started.stdout.strip().splitlines()[-1] if started.stdout.strip() else ""
        if not _CONTAINER_ID.match(container_id):
            raise ProviderError("podman run did not report a container ID")
        try:
            self._verify_mount(container_id, spec)
        except BaseException:
            self._remove(container_id)
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
        with self._env_file(command_env) as env_file:
            options = ["--env-file", env_file] if env_file else []
            done = self._executor(
                [self._podman, "exec", *options, container_id, *argv],
                self._environ,
                timeout_seconds,
            )
        duration = max(0.0, self._clock() - began)
        if done.timed_out:
            # Processes the command started may still run; nothing may keep
            # writing to the workspace, so the whole container goes (ADR-0014).
            self._remove(container_id)
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
        self._remove(container_id)

    # -- podman arguments --------------------------------------------------

    def _run_arguments(self, spec: ContainerSpec, env_file: str | None) -> list[str]:
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
        if spec.network is NetworkPolicy.NONE:
            args += ["--network=none", "--no-hosts"]
        if limits.max_processes is not None:
            args += ["--pids-limit", str(limits.max_processes)]
        if limits.memory_mb is not None:
            args += ["--memory", f"{limits.memory_mb}m"]
        if limits.cpus is not None:
            args += ["--cpus", f"{limits.cpus:g}"]
        if profile.init:
            args.append("--init")
        for key, value in _owner_labels(spec.owner).items():
            args += ["--label", f"{key}={value}"]
        return [*args, spec.image, *profile.keepalive]

    def _container_env(self, env: Mapping[str, str], *, with_home: bool = True) -> dict[str, str]:
        result = {"HOME": str(self._profile.home)} if with_home else {}
        for key, value in env.items():
            if not _ENV_NAME.match(key):
                raise ValueError(f"invalid environment variable name: {key!r}")
            if "\n" in value or "\r" in value or "\0" in value:
                raise ValueError(f"environment variable {key} must be a single line")
            result[key] = value
        return result

    def _check_network(self, network: NetworkPolicy) -> None:
        if network is NetworkPolicy.UNRESTRICTED:
            _LOG.warning("starting a sandbox with UNRESTRICTED network access (ADR-0011)")
        elif network is not NetworkPolicy.NONE:
            raise ProviderError(f"network policy {network.value} is not supported yet")

    # -- podman calls ------------------------------------------------------

    def _podman_call(self, args: Sequence[str]) -> Completed:
        done = self._executor([self._podman, *args], self._environ, _CONTROL_TIMEOUT)
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

    def _remove(self, container_id: str) -> None:
        done = self._podman_call(["rm", "--force", "--time", "0", container_id])
        if done.returncode != 0 and self._inspect(container_id) is not None:
            self._expect_success(done, "rm")

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
    def _env_file(self, env: Mapping[str, str]) -> Iterator[str | None]:
        """Pass values in a private file, not on a command line others can read."""
        if not env:
            yield None
            return
        fd, path = tempfile.mkstemp(prefix="ilmar-env-")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.writelines(f"{key}={value}\n" for key, value in env.items())
            yield path
        finally:
            with contextlib.suppress(FileNotFoundError):
                Path(path).unlink()


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
