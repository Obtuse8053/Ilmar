# SPDX-License-Identifier: Apache-2.0
"""Gate records (ADR-0030 §2, ADR-0040): a person's statement that a mode's gate passed.

A gate record binds a mode to the evidence it rests on:

* the approved target, by its **immutable** repository ID (a rename keeps it;
  a fork has another), with the name it had when recorded, for people;
* the one agent version the evidence is for (an exact ``x.y.z``);
* the merged pull request that holds the gate evidence;
* the passed pinned-version live test run;
* the reviewed, *recorded* stream fixture for that same version, by path and
  SHA-256 (ADR-0035: a synthetic fixture never counts);
* the Gate 1 record (#93).

Only a person writes one, with ``ilmar gate record``; nothing in the
coordinator does. A revocation is recorded, never a deletion.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable
from dataclasses import asdict, dataclass
from datetime import datetime

GATED_MODES = ("supervised",)
"""The modes a gate record may be written for. ``manual`` needs none."""

_VERSION = re.compile(r"^\d{1,4}\.\d{1,4}\.\d{1,6}$")
_REPOSITORY = re.compile(r"^[A-Za-z0-9-]{1,39}/[A-Za-z0-9._-]{1,100}$")
_GITHUB = r"^https://github\.com/[A-Za-z0-9-]{1,39}/[A-Za-z0-9._-]{1,100}/"
PULL_URL = re.compile(_GITHUB + r"pull/\d{1,9}$")
RUN_URL = re.compile(_GITHUB + r"actions/runs/\d{1,20}(/attempts/\d{1,4})?$")
GATE1_URL = re.compile(_GITHUB + r"(issues|pull)/\d{1,9}(#issuecomment-\d{1,20})?$")
FIXTURE_PATH = re.compile(r"^tests/fixtures/[A-Za-z0-9_./-]{1,200}\.json$")
SHA256 = re.compile(r"^[0-9a-f]{64}$")


@dataclass(frozen=True, slots=True)
class GateEvidence:
    """What a gate record states. Every field is checked; nothing is free text."""

    mode: str
    repository_id: int
    repository: str
    agent_version: str
    evidence: str
    live_test: str
    fixture: str
    fixture_sha256: str
    gate1: str

    def __post_init__(self) -> None:
        if self.mode not in GATED_MODES:
            raise ValueError(f"no gate is recorded for mode {self.mode!r}")
        if type(self.repository_id) is not int or not 0 < self.repository_id < 2**53:
            raise ValueError("repository_id must be a positive integer")
        checks = (
            ("repository", _REPOSITORY, "owner/name"),
            ("agent_version", _VERSION, "an exact x.y.z"),
            ("evidence", PULL_URL, "a GitHub pull request URL"),
            ("live_test", RUN_URL, "a GitHub Actions run URL"),
            ("fixture", FIXTURE_PATH, "a JSON file under tests/fixtures/"),
            ("fixture_sha256", SHA256, "64 lower-case hex digits"),
            ("gate1", GATE1_URL, "a GitHub issue, pull request or comment URL"),
        )
        for name, pattern, what in checks:
            value = getattr(self, name)
            if not isinstance(value, str) or not pattern.match(value):
                raise ValueError(f"{name} must be {what}")
        if ".." in self.fixture.split("/"):
            raise ValueError("fixture must not leave tests/fixtures/")

    def to_json(self) -> str:
        return json.dumps(asdict(self), sort_keys=True, separators=(",", ":"))

    @classmethod
    def from_json(cls, text: str) -> GateEvidence:
        data = json.loads(text)
        if not isinstance(data, dict) or set(data) != set(cls.__slots__):
            raise ValueError("a gate record's evidence is not understood")
        return cls(**data)


@dataclass(frozen=True, slots=True)
class GateRecord:
    """One stored gate record. ``seq`` orders them; a revoked one stays."""

    seq: int
    evidence: GateEvidence
    recorded_at: datetime
    recorded_by: str
    revoked_at: datetime | None = None
    revoked_by: str | None = None

    @property
    def active(self) -> bool:
        return self.revoked_at is None


@dataclass(frozen=True, slots=True)
class GateRecorded:
    """A commit's change: record ``evidence`` at ``at`` (the actor is the commit's)."""

    evidence: GateEvidence
    at: datetime


@dataclass(frozen=True, slots=True)
class GateRevoked:
    """A commit's change: revoke the active record of ``mode`` at ``at``."""

    mode: str
    at: datetime

    def __post_init__(self) -> None:
        if self.mode not in GATED_MODES:
            raise ValueError(f"no gate is recorded for mode {self.mode!r}")


GateChange = GateRecorded | GateRevoked


def active_gate(records: Iterable[GateRecord], mode: str) -> GateRecord | None:
    """The unrevoked record of ``mode``, if there is one (at most one exists)."""
    found = [r for r in records if r.evidence.mode == mode and r.active]
    if len(found) > 1:
        raise ValueError(f"more than one active gate record for {mode!r}")
    return found[0] if found else None
