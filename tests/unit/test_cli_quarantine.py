# SPDX-License-Identifier: Apache-2.0
"""``ilmar quarantine list|resolve`` (ADR-0042, #59): checks and refusals.

The full flow (quarantine, refusal while parts remain, removal by a person, a
recorded resolution and the next pass finishing the execution) is in
``test_recovery.py::test_a_person_s_verified_resolution_lets_the_next_pass_finish``.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from ilmar.cli_quarantine import _verify
from ilmar.domain.ledger import Resource, ResourceKind
from ilmar.domain.workspace import ResourceOwner, SandboxPart

NOW = datetime(2026, 3, 1, 12, tzinfo=UTC)
OURS = ResourceOwner("inst", "e1", 1, "tok")


class Parts:
    def __init__(self, *parts: SandboxPart) -> None:
        self.parts = parts

    def sandbox_parts(self, name: str) -> tuple[SandboxPart, ...]:
        return self.parts


def sandbox() -> Resource:
    return Resource.intended(ResourceKind.CONTAINER, "e1", 1, "ilmar-inst-e1-1",
                             owner_token="tok", now=NOW)  # fmt: skip


def test_a_sandbox_with_nothing_left_is_absent() -> None:
    assert _verify(sandbox(), None, Parts()) == "absent"


def test_any_remaining_part_refuses_and_a_foreign_one_is_flagged() -> None:
    other = ResourceOwner("inst", "e9", 1, "x")
    found = _verify(sandbox(), None, Parts(
        SandboxPart("network", "ilmar-inst-e1-1-net", OURS),
        SandboxPart("sidecar", "ilmar-inst-e1-1-egress", other),
        SandboxPart("container", "ilmar-inst-e1-1", None),
    ))  # fmt: skip
    assert isinstance(found, list)
    assert len(found) == 3
    assert sum("ANOTHER OWNER" in line for line in found) == 2


def test_a_directory_is_absent_or_unchanged_and_otherwise_refused(tmp_path: Path) -> None:
    path = tmp_path / "w"
    row = Resource.intended(ResourceKind.WORKSPACE, "e1", 1, str(path), owner_token="t",
                            now=NOW)  # fmt: skip
    assert _verify(row, None, None) == "absent"
    path.mkdir()
    stat = path.stat()
    same = row.moved(row.status.ACTIVE, now=NOW, identity=f"{stat.st_dev}:{stat.st_ino}")
    assert _verify(same, None, None) == "identity-matches"
    other = row.moved(row.status.ACTIVE, now=NOW, identity="1:2")
    assert isinstance(_verify(other, None, None), list)


def test_a_kind_that_cannot_be_checked_is_refused() -> None:
    row = Resource.intended(ResourceKind.BRANCH, "e1", 1, "ilmar/x", owner_token="t", now=NOW)
    assert isinstance(_verify(row, None, None), list)


# -- the whole command: every check under the instance lock (#136 review) ---------------

import argparse  # noqa: E402
import io  # noqa: E402
from typing import cast  # noqa: E402

import pytest  # noqa: E402

from ilmar import cli_quarantine  # noqa: E402
from ilmar.adapters.registry import store_writer  # noqa: E402
from ilmar.cli import _build_parser  # noqa: E402
from ilmar.config import load_config  # noqa: E402
from ilmar.core.layout import WorkspaceLayout  # noqa: E402
from ilmar.core.ownership import Ownership  # noqa: E402
from ilmar.domain.errors import FencingError  # noqa: E402
from ilmar.domain.events import EventType  # noqa: E402
from ilmar.domain.execution import Execution, ExecutionState, Transition  # noqa: E402
from ilmar.domain.ledger import Commit, ResourceStatus  # noqa: E402
from ilmar.domain.tasks import TaskRef  # noqa: E402
from ilmar.ports.execution_store import ExecutionStore  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[2]
KEY_NAME = "ilmar-default-e1-1"


def opened(path: Path) -> tuple[WorkspaceLayout, ExecutionStore, str]:
    config = load_config(path)
    layout, open_store = store_writer(config)
    return layout, cast(ExecutionStore, open_store()), config.instance_id


def shut(store: ExecutionStore) -> None:
    getattr(store, "close")()  # noqa: B009 - not part of the port


@pytest.fixture
def deployment(tmp_path: Path) -> tuple[Path, str]:
    """A configuration whose store holds one execution with a quarantined sandbox."""
    path = tmp_path.resolve() / "ilmar.toml"
    path.write_text((REPO_ROOT / "ilmar.example.toml").read_text())
    load_config(path).state_dir.mkdir(mode=0o700, parents=True)
    layout, store, instance = opened(path)
    try:
        with Ownership.take(layout=layout, store=store, instance_id=instance, now=NOW) as owner:
            queued = Execution("e1", TaskRef("github", "o/r#1"), ExecutionState.QUEUED, 1, 1,
                               NOW, NOW)  # fmt: skip
            first = Transition("e1", None, ExecutionState.QUEUED, NOW, "queued")
            store.commit(owner.fence, Commit(created=((queued, first),)))
            planned = Resource.intended(ResourceKind.CONTAINER, "e1", 1, KEY_NAME,
                                        owner_token="tok", now=NOW)  # fmt: skip
            store.commit(owner.fence, Commit(resources=(planned,)))
            active = planned.moved(ResourceStatus.ACTIVE, now=NOW, identity="c0ffee")
            store.commit(owner.fence, Commit(resources=(active,)))
            quarantined = active.moved(ResourceStatus.QUARANTINED, now=NOW, note="x")
            store.commit(owner.fence, Commit(resources=(quarantined,)))
    finally:
        shut(store)
    return path, quarantined.key


def resolve(path: Path, key: str, runtime: object) -> tuple[int, str]:
    args: argparse.Namespace = _build_parser().parse_args(
        ["quarantine", "resolve", "--config", str(path), "--execution", "e1", "--resource", key,
         "--as", "alice", "--action", "removed-by-hand"])  # fmt: skip
    err = io.StringIO()
    code = cli_quarantine.run(args, out=io.StringIO(), err=err, now=lambda: NOW, runtime=runtime)
    return code, err.getvalue()


def resolutions(path: Path) -> int:
    _, store, _ = opened(path)
    try:
        return sum(e.type is EventType.RESOURCE_RESOLVED for e in store.events())
    finally:
        shut(store)


class LockProbe(Parts):
    """Proves the check runs under the instance lock: taking it again fails."""

    def __init__(self, path: Path, *parts: SandboxPart) -> None:
        super().__init__(*parts)
        self.path, self.held = path, False

    def sandbox_parts(self, name: str) -> tuple[SandboxPart, ...]:
        layout, store, instance = opened(self.path)
        try:
            Ownership.take(layout=layout, store=store, instance_id=instance, now=NOW)
        except (FencingError, OSError):
            self.held = True
        finally:
            shut(store)
        return self.parts


def test_the_check_runs_under_the_instance_lock(deployment: tuple[Path, str]) -> None:
    path, key = deployment
    probe = LockProbe(path)
    assert resolve(path, key, probe)[0] == 0
    assert probe.held
    assert resolutions(path) == 1


def test_a_part_that_reappears_after_listing_refuses(deployment: tuple[Path, str]) -> None:
    """The state changed since the person last looked: the check under the lock,
    not an earlier one, decides."""
    path, key = deployment
    back = SandboxPart("sidecar", KEY_NAME + "-egress", OURS)
    code, err = resolve(path, key, LockProbe(path, back))
    assert (code, "remains: sidecar" in err) == (1, True)
    assert resolutions(path) == 0


def test_a_running_coordinator_blocks_the_resolution(deployment: tuple[Path, str]) -> None:
    path, key = deployment
    layout, store, instance = opened(path)
    calls: list[str] = []

    class Counting(Parts):
        def sandbox_parts(self, name: str) -> tuple[SandboxPart, ...]:
            calls.append(name)
            return ()

    try:
        with Ownership.take(layout=layout, store=store, instance_id=instance, now=NOW):
            code, err = resolve(path, key, Counting())
    finally:
        shut(store)
    assert (code, "is a coordinator running" in err) == (3, True)
    assert calls == []  # nothing was checked outside the lock
    assert resolutions(path) == 0


def test_a_second_resolution_is_not_recorded(deployment: tuple[Path, str]) -> None:
    path, key = deployment
    assert resolve(path, key, Parts())[0] == 0
    assert resolve(path, key, Parts())[0] == 0  # "already resolved", read under the lock
    assert resolutions(path) == 1
