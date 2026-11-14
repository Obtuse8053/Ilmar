# SPDX-License-Identifier: Apache-2.0
"""Gate records (ADR-0030 §2, ADR-0040): a person's statement that a mode's gate passed.

A gate record binds a mode to the evidence it rests on:

* the approved target, by its **immutable** repository ID (a rename keeps it;
  a fork has another), with the name it had when recorded, for people;
* the one agent version the evidence is for (an exact ``x.y.z``);
* the merged pull request that holds the gate evidence, and the immutable
  commit SHA a person reviewed it at (a URL alone can change meaning);
* the pinned-version live test run a person verified as passed;
* the *recorded* stream fixture for that same version, by path and SHA-256
  (ADR-0035: a synthetic fixture never counts);
* the Gate 1 record (#93).

Only a person writes one, with ``ilmar gate record``; nothing in the
coordinator does. A revocation is recorded, never a deletion.

**What Ilmar checks and what a person must.** Ilmar checks the *form* of every
field and, on the host, the fixture's provenance, version and digest. It does
not check that the live run passed, that Gate 1 passed, that the evidence PR
says what it should, or that the commit is the reviewed one: the person who
records the gate verifies those, and the record says who did.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta

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
COMMIT = re.compile(r"^[0-9a-f]{40}$")


@dataclass(frozen=True, slots=True)
class GateEvidence:
    """What a gate record states. Every field is checked; nothing is free text."""

    mode: str
    repository_id: int
    repository: str
    agent_version: str
    evidence: str
    evidence_commit: str
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
            ("evidence_commit", COMMIT, "a full 40-character lower-case commit SHA"),
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


def active_gate(records: Iterable[GateRecord], mode: str) -> GateRecord | None:
    """The unrevoked record of ``mode``, if there is one (at most one exists)."""
    found = [r for r in records if r.evidence.mode == mode and r.active]
    if len(found) > 1:
        raise ValueError(f"more than one active gate record for {mode!r}")
    return found[0] if found else None


# -- trial authorization (ADR-0041) -------------------------------------------------------

TRIAL_MAX_EXECUTIONS = 20
TRIAL_MAX_DURATION = timedelta(hours=72)
TRIAL_MODE = "supervised-trial"
"""Not a mode of ``[autonomy]``: ``ilmar run --loop --trial`` under ``supervised``."""


@dataclass(frozen=True, slots=True)
class TrialTerms:
    """A person's time-boxed, bounded authorization to run the supervised loop on
    one dedicated disposable repository, for the Gate 1 live run (ADR-0041).

    It is never a gate record and never satisfies ``ilmar run --loop``; only
    ``--loop --trial`` accepts it, and only within these terms."""

    repository_id: int
    repository: str
    agent_version: str
    max_executions: int
    max_spend_micros: int
    starts_at: datetime
    expires_at: datetime
    purpose: str
    """The Gate 1 issue or comment this trial is for."""

    def __post_init__(self) -> None:
        if type(self.repository_id) is not int or not 0 < self.repository_id < 2**53:
            raise ValueError("repository_id must be a positive integer")
        if not isinstance(self.repository, str) or not _REPOSITORY.match(self.repository):
            raise ValueError("repository must be owner/name")
        if self.repository.lower() == "obtuse8053/ilmar":
            raise ValueError("a trial never runs on Ilmar's own repository")
        if not isinstance(self.agent_version, str) or not _VERSION.match(self.agent_version):
            raise ValueError("agent_version must be an exact x.y.z")
        if type(self.max_executions) is not int or not 0 < self.max_executions <= (
            TRIAL_MAX_EXECUTIONS
        ):
            raise ValueError(f"max_executions must be 1 to {TRIAL_MAX_EXECUTIONS}")
        if type(self.max_spend_micros) is not int or self.max_spend_micros < 1:
            raise ValueError("max_spend_micros must be positive")
        if self.starts_at.tzinfo is None or self.expires_at.tzinfo is None:
            raise ValueError("trial times must be timezone-aware")
        if not timedelta(0) < self.expires_at - self.starts_at <= TRIAL_MAX_DURATION:
            raise ValueError("a trial lasts more than nothing and at most 72 hours")
        if not isinstance(self.purpose, str) or not GATE1_URL.match(self.purpose):
            raise ValueError("purpose must be the Gate 1 issue, pull request or comment URL")

    def to_json(self) -> str:
        data = asdict(self)
        data["starts_at"] = self.starts_at.astimezone(UTC).isoformat()
        data["expires_at"] = self.expires_at.astimezone(UTC).isoformat()
        return json.dumps(data, sort_keys=True, separators=(",", ":"))

    @classmethod
    def from_json(cls, text: str) -> TrialTerms:
        data = json.loads(text)
        if not isinstance(data, dict) or set(data) != set(cls.__slots__):
            raise ValueError("a trial's terms are not understood")
        data["starts_at"] = datetime.fromisoformat(data["starts_at"])
        data["expires_at"] = datetime.fromisoformat(data["expires_at"])
        return cls(**data)


@dataclass(frozen=True, slots=True)
class TrialRecord:
    seq: int
    terms: TrialTerms
    authorized_by: str
    revoked_at: datetime | None = None
    revoked_by: str | None = None

    def in_force(self, now: datetime) -> bool:
        """Unrevoked, started and not yet expired: expiry needs no one to act."""
        return self.revoked_at is None and self.terms.starts_at <= now < self.terms.expires_at


@dataclass(frozen=True, slots=True)
class TrialAuthorized:
    terms: TrialTerms
    at: datetime


@dataclass(frozen=True, slots=True)
class TrialRevoked:
    at: datetime


def trial_in_force(records: Iterable[TrialRecord], now: datetime) -> TrialRecord | None:
    found = [r for r in records if r.in_force(now)]
    if len(found) > 1:
        raise ValueError("more than one trial authorization in force")
    return found[0] if found else None


def unrevoked_trial(records: Iterable[TrialRecord]) -> TrialRecord | None:
    """The newest unrevoked trial, expired or not (a revocation's target)."""
    found = [r for r in records if r.revoked_at is None]
    return found[-1] if found else None


GateChange = GateRecorded | GateRevoked | TrialAuthorized | TrialRevoked
