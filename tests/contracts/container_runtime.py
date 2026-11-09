# SPDX-License-Identifier: Apache-2.0
"""Contract every ``ContainerRuntime`` adapter must satisfy.

The command hooks let each runtime express the same behaviour in its own
terms (a real runtime will use a shell; the fake uses a tiny DSL). Ownership
cases implement ADR-0012: only an equal ``ResourceOwner`` may replace or
remove a container, and operations act on the immutable id.
"""

from __future__ import annotations

import dataclasses
import threading
import time
from collections.abc import Iterator, Sequence
from pathlib import Path

import pytest

from ilmar.domain.errors import (
    ContainmentError,
    OwnershipConflictError,
    PolicyViolationError,
    ProviderError,
)
from ilmar.domain.workspace import (
    ContainerHandle,
    ContainerSpec,
    DirectoryIdentity,
    NetworkPolicy,
    ResourceOwner,
)
from ilmar.ports.container_runtime import ContainerRuntime

OWNER = ResourceOwner(instance_id="inst-a", execution_id="exec-1", attempt=1, token="tok-1")
OTHER_ATTEMPT = ResourceOwner(instance_id="inst-a", execution_id="exec-1", attempt=1, token="tok-2")
OTHER_INSTANCE = ResourceOwner(
    instance_id="inst-b", execution_id="exec-1", attempt=1, token="tok-1"
)
NAME = "ilmar-inst-a-exec-1-1"


def identity_of(path: Path) -> DirectoryIdentity:
    info = path.lstat()
    return DirectoryIdentity(info.st_dev, info.st_ino)


class SetAfter:
    """A stop signal that becomes set on its ``checks + 1``-th look.

    Deterministic for every runtime: a real one looks every 0.25 s, a fake once
    per simulated second. It also counts how often it was looked at.
    """

    def __init__(self, checks: int) -> None:
        self._checks = checks
        self.looks = 0
        self._lock = threading.Lock()

    def is_set(self) -> bool:
        with self._lock:
            self.looks += 1
            return self.looks > self._checks


STOP_BOUND_SECONDS = 30.0
"""How long a real runtime may take to kill, remove and confirm after a stop."""


class ContainerRuntimeContract:
    def make_runtime(self) -> ContainerRuntime:
        raise NotImplementedError

    def break_removal(self, runtime: ContainerRuntime) -> None:
        """Make the next sandbox removal unconfirmable (provider-specific)."""
        pytest.skip("this runtime cannot simulate an unconfirmed removal")

    def repair_removal(self, runtime: ContainerRuntime) -> None:
        raise NotImplementedError

    def make_workspace(self, tmp_path: Path) -> Path:
        """Create the directory the runtime may mount (override to fit a layout)."""
        path = tmp_path / "workspace"
        path.mkdir()
        return path

    def make_spec(
        self,
        workspace: Path,
        name: str,
        owner: ResourceOwner,
        identity: DirectoryIdentity,
    ) -> ContainerSpec:
        raise NotImplementedError

    def create_foreign(
        self, runtime: ContainerRuntime, spec: ContainerSpec, owner: ResourceOwner | None
    ) -> ContainerHandle:
        """Create a running container named ``spec.name`` by provider-specific means,
        labelled with ``owner`` (or with no ownership labels when ``None``)."""
        raise NotImplementedError

    def echo(self, text: str) -> Sequence[str]:
        raise NotImplementedError

    def exit_with(self, code: int) -> Sequence[str]:
        raise NotImplementedError

    def sleep(self, seconds: float) -> Sequence[str]:
        raise NotImplementedError

    def write_file(self, relative_path: str, content: str) -> Sequence[str]:
        raise NotImplementedError

    @pytest.fixture
    def runtime(self) -> ContainerRuntime:
        return self.make_runtime()

    @pytest.fixture
    def workspace(self, tmp_path: Path) -> Path:
        return self.make_workspace(tmp_path)

    @pytest.fixture
    def spec(self, workspace: Path) -> ContainerSpec:
        return self.make_spec(workspace, NAME, OWNER, identity_of(workspace))

    @pytest.fixture
    def container(
        self, runtime: ContainerRuntime, spec: ContainerSpec
    ) -> Iterator[ContainerHandle]:
        handle = runtime.start(spec)
        yield handle
        current = runtime.find(spec.name)
        if current is not None and current.owner == OWNER:
            runtime.stop(current.handle, owner=OWNER)

    # -- commands --------------------------------------------------------

    def test_started_container_is_running_and_owned(
        self, runtime: ContainerRuntime, container: ContainerHandle
    ) -> None:
        assert container.runtime == runtime.name
        assert container.name == NAME
        assert runtime.is_running(container)
        info = runtime.find(NAME)
        assert info is not None
        assert info.handle == container
        assert info.owner == OWNER
        assert info.running

    def test_find_unknown_name_returns_none(self, runtime: ContainerRuntime) -> None:
        assert runtime.find("ilmar-does-not-exist") is None

    def test_run_captures_output(
        self, runtime: ContainerRuntime, container: ContainerHandle
    ) -> None:
        result = runtime.run(container, self.echo("hello ilmar"), timeout_seconds=30)
        assert result.succeeded
        assert result.stdout.strip() == "hello ilmar"

    def test_non_zero_exit_is_a_result_not_an_error(
        self, runtime: ContainerRuntime, container: ContainerHandle
    ) -> None:
        result = runtime.run(container, self.exit_with(3), timeout_seconds=30)
        assert result.exit_code == 3
        assert not result.succeeded
        assert not result.timed_out

    def test_timeout_is_reported_and_removes_the_container(
        self, runtime: ContainerRuntime, container: ContainerHandle
    ) -> None:
        result = runtime.run(container, self.sleep(30), timeout_seconds=1)
        assert result.timed_out
        assert not result.succeeded
        assert not runtime.is_running(container)
        assert runtime.find(NAME) is None
        with pytest.raises(ProviderError):
            runtime.run(container, self.echo("after timeout"), timeout_seconds=30)

    def test_workspace_is_shared_with_host(
        self, runtime: ContainerRuntime, container: ContainerHandle, workspace: Path
    ) -> None:
        result = runtime.run(container, self.write_file("out.txt", "done"), timeout_seconds=30)
        assert result.succeeded
        assert (workspace / "out.txt").read_text().strip() == "done"

    # -- network policy (PR #39 review) -------------------------------------

    def test_unrestricted_network_needs_a_deployment_opt_in(
        self, runtime: ContainerRuntime, spec: ContainerSpec
    ) -> None:
        with pytest.raises(PolicyViolationError):
            runtime.start(dataclasses.replace(spec, network=NetworkPolicy.UNRESTRICTED))
        assert runtime.find(NAME) is None

    # -- workspace containment (#4) ----------------------------------------

    def test_a_replaced_workspace_is_not_mounted(
        self, runtime: ContainerRuntime, workspace: Path
    ) -> None:
        recorded = identity_of(workspace)
        workspace.rename(workspace.with_name("moved-away"))
        workspace.mkdir()  # same path, different directory
        with pytest.raises(OwnershipConflictError):
            runtime.start(self.make_spec(workspace, NAME, OWNER, recorded))
        assert runtime.find(NAME) is None

    def test_a_symlinked_workspace_is_not_mounted(
        self, runtime: ContainerRuntime, workspace: Path
    ) -> None:
        target = workspace.with_name("elsewhere")
        target.mkdir()
        workspace.rmdir()
        workspace.symlink_to(target)
        with pytest.raises(OwnershipConflictError):
            runtime.start(self.make_spec(workspace, NAME, OWNER, identity_of(target)))
        assert runtime.find(NAME) is None

    def test_writes_cannot_escape_the_workspace(
        self, runtime: ContainerRuntime, container: ContainerHandle, workspace: Path
    ) -> None:
        runtime.run(container, self.write_file("../escaped.txt", "x"), timeout_seconds=30)
        assert not (workspace.parent / "escaped.txt").exists()

    def test_symlinks_in_the_workspace_do_not_reach_the_host(
        self, runtime: ContainerRuntime, container: ContainerHandle, workspace: Path
    ) -> None:
        outside = workspace.parent / "host-data"
        outside.mkdir()
        (workspace / "to-host").symlink_to(outside)
        (workspace / "to-host-file").symlink_to(outside / "planted.txt")
        runtime.run(container, self.write_file("to-host/pwned.txt", "x"), timeout_seconds=30)
        runtime.run(container, self.write_file("to-host-file", "x"), timeout_seconds=30)
        assert list(outside.iterdir()) == []

    # -- stop ------------------------------------------------------------

    def test_stop_is_idempotent_and_final(
        self, runtime: ContainerRuntime, container: ContainerHandle
    ) -> None:
        runtime.stop(container, owner=OWNER)
        runtime.stop(container, owner=OWNER)
        assert not runtime.is_running(container)
        assert runtime.find(NAME) is None
        with pytest.raises(ProviderError):
            runtime.run(container, self.echo("too late"), timeout_seconds=30)

    def test_remove_owned_removes_the_recorded_sandbox_once(
        self, runtime: ContainerRuntime, spec: ContainerSpec
    ) -> None:
        runtime.start(spec)
        assert runtime.remove_owned(NAME, owner=OWNER) is True
        assert runtime.find(NAME) is None
        assert runtime.remove_owned(NAME, owner=OWNER) is False  # idempotent

    def test_remove_owned_of_nothing_is_false(self, runtime: ContainerRuntime) -> None:
        assert runtime.remove_owned(NAME, owner=OWNER) is False

    def test_remove_owned_never_touches_another_owners_sandbox(
        self, runtime: ContainerRuntime, spec: ContainerSpec
    ) -> None:
        handle = runtime.start(spec)
        with pytest.raises(OwnershipConflictError):
            runtime.remove_owned(NAME, owner=OTHER_ATTEMPT)
        assert runtime.is_running(handle)
        runtime.stop(handle, owner=OWNER)

    @pytest.mark.parametrize("wrong", [OTHER_ATTEMPT, OTHER_INSTANCE])
    def test_stop_with_wrong_owner_is_refused_and_leaves_container(
        self, runtime: ContainerRuntime, container: ContainerHandle, wrong: ResourceOwner
    ) -> None:
        with pytest.raises(OwnershipConflictError):
            runtime.stop(container, owner=wrong)
        assert runtime.is_running(container)

    # -- start / replace -------------------------------------------------

    def test_restart_by_owner_replaces_with_a_new_container(
        self, runtime: ContainerRuntime, spec: ContainerSpec, container: ContainerHandle
    ) -> None:
        replacement = runtime.start(spec)
        assert replacement.id != container.id
        assert not runtime.is_running(container)
        assert runtime.is_running(replacement)
        info = runtime.find(NAME)
        assert info is not None
        assert info.handle == replacement

    def test_stale_handle_never_affects_the_replacement(
        self, runtime: ContainerRuntime, spec: ContainerSpec, container: ContainerHandle
    ) -> None:
        replacement = runtime.start(spec)
        runtime.stop(container, owner=OWNER)  # old id: already gone, a no-op
        assert runtime.is_running(replacement)
        with pytest.raises(ProviderError):
            runtime.run(container, self.echo("stale"), timeout_seconds=30)

    @pytest.mark.parametrize("foreign_owner", [OTHER_ATTEMPT, OTHER_INSTANCE, None])
    def test_start_refuses_to_replace_a_container_it_does_not_own(
        self,
        runtime: ContainerRuntime,
        spec: ContainerSpec,
        foreign_owner: ResourceOwner | None,
    ) -> None:
        foreign = self.create_foreign(runtime, spec, foreign_owner)
        with pytest.raises(OwnershipConflictError):
            runtime.start(spec)
        info = runtime.find(NAME)
        assert info is not None
        assert info.handle == foreign
        assert info.owner == foreign_owner
        assert runtime.is_running(foreign)

    # -- the stop signal (ADR-0025 §2): exactly like a timeout ----------------

    def test_a_stop_signal_cancels_the_command_and_removes_the_sandbox(
        self, runtime: ContainerRuntime, container: ContainerHandle
    ) -> None:
        stop = SetAfter(2)
        began = time.monotonic()
        result = runtime.run(container, self.sleep(120), timeout_seconds=300, stop=stop)
        assert time.monotonic() - began < STOP_BOUND_SECONDS
        assert result.cancelled
        assert not result.timed_out
        assert not result.succeeded
        assert stop.looks >= 3
        assert not runtime.is_running(container)
        assert runtime.find(NAME) is None
        with pytest.raises(ProviderError):
            runtime.run(container, self.echo("stale"), timeout_seconds=30)

    def test_a_signal_set_before_the_command_runs_nothing(
        self,
        runtime: ContainerRuntime,
        container: ContainerHandle,
        workspace: Path,
    ) -> None:
        stop = threading.Event()
        stop.set()
        result = runtime.run(container, self.write_file("ran.txt", "x"), timeout_seconds=30,
                             stop=stop)  # fmt: skip
        assert result.cancelled
        assert not (workspace / "ran.txt").exists()
        assert runtime.find(NAME) is None

    def test_an_unset_stop_signal_changes_nothing(
        self, runtime: ContainerRuntime, container: ContainerHandle
    ) -> None:
        stop = threading.Event()
        result = runtime.run(container, self.echo("still here"), timeout_seconds=30, stop=stop)
        assert result.succeeded
        assert not result.cancelled
        assert result.stdout.strip() == "still here"
        assert runtime.is_running(container)
        assert not stop.is_set()  # a runtime only reads the signal

    def test_a_timeout_with_an_unset_signal_is_still_a_timeout(
        self, runtime: ContainerRuntime, container: ContainerHandle
    ) -> None:
        result = runtime.run(container, self.sleep(30), timeout_seconds=1,
                             stop=threading.Event())  # fmt: skip
        assert result.timed_out
        assert not result.cancelled
        assert runtime.find(NAME) is None

    def test_a_stop_leaves_other_sandboxes_alone(
        self, runtime: ContainerRuntime, container: ContainerHandle, tmp_path: Path
    ) -> None:
        other_workspace = self.make_workspace_named(tmp_path, "other")
        other_spec = self.make_spec(other_workspace, NAME + "-other", OTHER_ATTEMPT,
                                    identity_of(other_workspace))  # fmt: skip
        other = runtime.start(other_spec)
        try:
            result = runtime.run(container, self.sleep(120), timeout_seconds=300,
                                 stop=SetAfter(1))  # fmt: skip
            assert result.cancelled
            assert runtime.is_running(other)
            assert runtime.run(other, self.echo("alive"), timeout_seconds=30).succeeded
        finally:
            runtime.stop(other, owner=OTHER_ATTEMPT)

    def test_an_unconfirmed_removal_after_a_stop_fails_closed_and_quarantines(
        self,
        runtime: ContainerRuntime,
        container: ContainerHandle,
        spec: ContainerSpec,
    ) -> None:
        self.break_removal(runtime)
        with pytest.raises(ContainmentError):
            runtime.run(container, self.sleep(120), timeout_seconds=300, stop=SetAfter(1))
        with pytest.raises(ContainmentError):  # the workspace is quarantined
            runtime.start(spec)
        self.repair_removal(runtime)
        replacement = runtime.start(spec)  # removal now confirmed: released
        assert replacement.id != container.id
        runtime.stop(replacement, owner=OWNER)

    def make_workspace_named(self, tmp_path: Path, name: str) -> Path:
        """A second workspace the runtime may mount (override to fit a layout)."""
        path = tmp_path / name
        path.mkdir()
        return path
