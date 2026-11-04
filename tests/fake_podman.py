# SPDX-License-Identifier: Apache-2.0
"""An in-memory stand-in for the ``podman`` command line, used as an executor in tests.

It understands only the invocations ``PodmanRuntime`` makes. Like Podman, it
reports container IDs as 64 hex digits, filters ``ps`` by an anchored name
regex, answers ``inspect`` for unknown IDs with exit code 125 and "no such
container", and reads ``--env-file``. Inside containers it interprets the same
small command language as ``tests/fakes.py``:

* ``["echo", text]``, ``["exit", code]``, ``["sleep", seconds]``;
* ``["write", relative_path, content]``, which writes below the bind-mount
  source and refuses any path that resolves outside it.

Real Podman behaviour is covered by ``tests/integration/test_podman_runtime.py``.
"""

from __future__ import annotations

import hashlib
import itertools
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any

from ilmar.adapters.podman.runtime import TIMED_OUT_EXIT_CODE, Completed
from ilmar.domain.workspace import DirectoryIdentity

PODMAN = "podman"


@dataclass
class FakeContainer:
    id: str
    name: str
    labels: dict[str, str]
    pid: int
    source: Path | None
    destination: PurePosixPath | None
    run_args: list[str]
    env: dict[str, str]
    running: bool = True
    networks: dict[str, str] = field(default_factory=dict)  # name -> IPv4 address
    logs: str = ""


@dataclass
class Call:
    argv: list[str]
    env: dict[str, str]
    timeout: float
    env_file: dict[str, str] | None = None


@dataclass
class FakeNetwork:
    name: str
    labels: dict[str, str]
    subnet: str
    internal: bool
    dns_enabled: bool
    hosts: int = 1


@dataclass
class FakePodman:
    rootless: bool = True
    rm_behaviour: str = "ok"  # "ok", "fail" (exit 125, container stays) or "hang"
    proxy_ready: bool = True
    networks: dict[str, FakeNetwork] = field(
        default_factory=lambda: {
            "podman": FakeNetwork("podman", {}, "10.88.0.0/16", internal=False, dns_enabled=False)
        }
    )
    containers: dict[str, FakeContainer] = field(default_factory=dict)
    calls: list[Call] = field(default_factory=list)
    _counter: itertools.count[int] = field(default_factory=lambda: itertools.count(1))

    # -- executor --------------------------------------------------------

    def __call__(self, argv: Sequence[str], env: Mapping[str, str], timeout: float) -> Completed:
        args = list(argv)
        assert args[0] == PODMAN, args
        call = Call(args, dict(env), timeout)
        self.calls.append(call)
        command, rest = args[1], args[2:]
        if command == "info":
            return Completed(0, "true\n" if self.rootless else "false\n", "")
        if command == "ps":
            return self._ps(rest)
        if command == "container" and rest[0] == "inspect":
            return self._inspect(rest[1])
        if command == "run":
            return self._run(rest, call)
        if command == "exec":
            return self._exec(rest, call, timeout)
        if command == "network":
            return self._network(rest)
        if command == "logs":
            container = self.containers.get(rest[-1])
            if container is None:
                return Completed(125, "", "Error: no such container\n")
            return Completed(0, "", container.logs)
        if command == "rm":
            if self.rm_behaviour == "fail":
                return Completed(125, "", "Error: cannot remove container: device busy\n")
            if self.rm_behaviour == "hang":
                return Completed(TIMED_OUT_EXIT_CODE, "", "", timed_out=True)
            self.containers.pop(rest[-1], None)
            return Completed(0, rest[-1] + "\n", "")
        return Completed(125, "", f"Error: unsupported in fake: {args}\n")

    # -- helpers for tests -------------------------------------------------

    def create(self, name: str, labels: Mapping[str, str]) -> str:
        """A container created by someone else (no mount)."""
        container = self._new(name, dict(labels), None, None, [], {})
        return container.id

    def by_name(self, name: str) -> FakeContainer | None:
        return next((c for c in self.containers.values() if c.name == name), None)

    def mount_probe(self, pid: int, path: PurePosixPath) -> DirectoryIdentity:
        container = next(c for c in self.containers.values() if c.pid == pid)
        assert container.source is not None
        assert container.destination == path
        info = container.source.lstat()
        return DirectoryIdentity(info.st_dev, info.st_ino)

    def commands(self) -> list[str]:
        return [call.argv[1] for call in self.calls]

    # -- podman subcommands ------------------------------------------------

    def _new(
        self,
        name: str,
        labels: dict[str, str],
        source: Path | None,
        destination: PurePosixPath | None,
        run_args: list[str],
        env: dict[str, str],
    ) -> FakeContainer:
        number = next(self._counter)
        container_id = hashlib.sha256(f"container-{number}".encode()).hexdigest()
        container = FakeContainer(
            container_id, name, labels, 1000 + number, source, destination, run_args, env
        )
        self.containers[container_id] = container
        return container

    def _ps(self, rest: list[str]) -> Completed:
        pattern = rest[rest.index("--filter") + 1].removeprefix("name=")
        entries = [
            {"Id": c.id, "Names": [c.name], "Labels": c.labels, "State": "running"}
            for c in self.containers.values()
            if re.search(pattern, c.name)
        ]
        return Completed(0, json.dumps(entries), "")

    def _inspect(self, container_id: str) -> Completed:
        container = self.containers.get(container_id)
        if container is None:
            return Completed(125, "[]\n", f"Error: no such container {container_id}\n")
        data: list[dict[str, Any]] = [
            {
                "Id": container.id,
                "Name": container.name,
                "State": {"Running": container.running, "Pid": container.pid},
                "Config": {"Labels": container.labels},
                "NetworkSettings": {
                    "Networks": {
                        name: {"IPAddress": address} for name, address in container.networks.items()
                    }
                },
            }
        ]
        return Completed(0, json.dumps(data), "")

    def _run(self, rest: list[str], call: Call) -> Completed:
        name = rest[rest.index("--name") + 1]
        if self.by_name(name) is not None:
            return Completed(125, "", f'Error: the container name "{name}" is already in use\n')
        labels = dict(rest[i + 1].split("=", 1) for i, arg in enumerate(rest) if arg == "--label")
        mount = dict(part.split("=", 1) for part in rest[rest.index("--mount") + 1].split(","))
        env = _read_env_file(rest, call)
        networks = [rest[i + 1] for i, arg in enumerate(rest) if arg == "--network"]
        for network in networks:
            if network not in self.networks:
                return Completed(125, "", f"Error: unable to find network {network}\n")
        container = self._new(
            name,
            labels,
            Path(mount["source"]),
            PurePosixPath(mount["destination"]),
            rest,
            env,
        )
        for network in networks:
            fake = self.networks[network]
            fake.hosts += 1
            base = fake.subnet.split("/")[0].rsplit(".", 1)[0]
            container.networks[network] = f"{base}.{fake.hosts}"
        if self.proxy_ready and "python3" in rest:
            container.logs = "ilmar-egress READY listen=0.0.0.0:3128\n"
        return Completed(0, container.id + "\n", "")

    def _network(self, rest: list[str]) -> Completed:
        action, name = rest[0], rest[-1]
        if action == "create":
            if name in self.networks:
                return Completed(125, "", f"Error: network name {name} already used\n")
            labels = dict(
                rest[i + 1].split("=", 1) for i, arg in enumerate(rest) if arg == "--label"
            )
            subnet = f"10.89.{len(self.networks)}.0/24"
            self.networks[name] = FakeNetwork(
                name, labels, subnet, "--internal" in rest, "--disable-dns" not in rest
            )
            return Completed(0, name + "\n", "")
        network = self.networks.get(name)
        if action == "exists":
            return Completed(0 if network else 1, "", "")
        if network is None:
            return Completed(125, "", f"Error: unable to find network {name}: network not found\n")
        if action == "inspect":
            data = [
                {
                    "name": network.name,
                    "labels": network.labels,
                    "subnets": [{"subnet": network.subnet}],
                    "internal": network.internal,
                    "dns_enabled": network.dns_enabled,
                }
            ]
            return Completed(0, json.dumps(data), "")
        if action == "rm":
            if any(name in c.networks for c in self.containers.values()):
                return Completed(2, "", f"Error: network {name} is being used\n")
            del self.networks[name]
            return Completed(0, name + "\n", "")
        return Completed(125, "", f"Error: unsupported network action {action}\n")

    def _exec(self, rest: list[str], call: Call, timeout: float) -> Completed:
        _read_env_file(rest, call)
        if rest[0] == "--env-file":
            rest = rest[2:]
        container = self.containers.get(rest[0])
        if container is None or not container.running:
            return Completed(125, "", "Error: no such container\n")
        match rest[1:]:
            case ["echo", text]:
                return Completed(0, f"{text}\n", "")
            case ["exit", code]:
                return Completed(int(code), "", "")
            case ["sleep", seconds]:
                if float(seconds) > timeout:
                    return Completed(TIMED_OUT_EXIT_CODE, "", "", timed_out=True)
                return Completed(0, "", "")
            case ["write", relative, content]:
                assert container.source is not None
                target = (container.source / relative).resolve()
                if not target.is_relative_to(container.source.resolve()):
                    return Completed(1, "", "sh: can't create: Read-only file system\n")
                target.write_text(content)
                return Completed(0, "", "")
            case _:
                return Completed(127, "", f"Error: executable file not found: {rest[1:]}\n")


def _read_env_file(rest: list[str], call: Call) -> dict[str, str]:
    if "--env-file" not in rest:
        return {}
    path = Path(rest[rest.index("--env-file") + 1])
    lines = path.read_text(encoding="utf-8").splitlines()
    call.env_file = dict(line.split("=", 1) for line in lines)
    return call.env_file
