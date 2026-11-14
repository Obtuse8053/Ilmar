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
