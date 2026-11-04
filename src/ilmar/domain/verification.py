# SPDX-License-Identifier: Apache-2.0
"""Verification policy and evidence (ADR-0010 §3 to §5, ADR-0018).

The *policy* comes from Ilmar's configuration, never from the candidate. It
names the commands to run in the verification sandbox and three path classes:
protected paths, check-influencing paths and CI-configuration paths. The
*evidence record* is what one verification run produced. It is append-only
once stored, and it is the only basis for publishing.
"""

from __future__ import annotations

import enum
import hashlib
import json
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from functools import lru_cache
from typing import Any

from ilmar.domain.workspace import ResourceLimits

EVIDENCE_FORMAT = 1


class CheckClass(enum.Enum):
    """Who controls what a check runs (ADR-0010 §4)."""

    INDEPENDENT = "independent"
    """Run by Ilmar on the candidate's tree or diff as data; nothing is executed."""
    CANDIDATE_EXECUTED = "candidate-executed"
    """A policy command that runs code the candidate controls, in the sandbox."""


class Independence(enum.Enum):
    """How far the checks that passed were written by someone other than the agent."""

    UNCHANGED_CHECKS = "unchanged-checks"
    MODIFIED_CHECKS = "modified-checks"
    CI_CONFIG_MODIFIED = "ci-config-modified"


class Verdict(enum.Enum):
    """What the evidence allows (the ADR-0010 §5 table)."""

    PUBLISH = "publish"
    PUBLISH_FOR_REVIEW = "publish-for-review"
    """Publish with ``requires_human_review`` and the review label."""
    REJECTED_CHECKS_FAILED = "rejected-checks-failed"
    REJECTED_CI_CONFIG = "rejected-ci-config"

    @property
    def publishable(self) -> bool:
        return self in (Verdict.PUBLISH, Verdict.PUBLISH_FOR_REVIEW)


# -- paths -------------------------------------------------------------------------


def path_matches(path: str, pattern: str) -> bool:
    """Match a repository-relative POSIX path against a policy pattern.

    ``dir/`` matches everything below ``dir``. ``**`` matches across
    directories, ``*`` within one directory, ``?`` one character. Everything
    else is literal. Matching is case-sensitive, like git.
    """
    return _compile(pattern).fullmatch(path) is not None


@lru_cache(maxsize=512)
def _compile(pattern: str) -> re.Pattern[str]:
    if not pattern or pattern.startswith("/"):
        raise ValueError(f"invalid path pattern: {pattern!r}")
    if pattern.endswith("/"):
        pattern += "**"
    out: list[str] = []
    i = 0
    while i < len(pattern):
        if pattern.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
        elif pattern.startswith("**", i):
            out.append(".*")
            i += 2
        elif pattern[i] == "*":
            out.append("[^/]*")
            i += 1
        elif pattern[i] == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(pattern[i]))
            i += 1
    return re.compile("".join(out))


def matching(paths: Iterable[str], patterns: Iterable[str]) -> tuple[str, ...]:
    pattern_list = tuple(patterns)
    return tuple(sorted(p for p in set(paths) if any(path_matches(p, q) for q in pattern_list)))


DEFAULT_PROTECTED_PATHS: tuple[str, ...] = (
    "AGENTS.md",
    "CLAUDE.md",
    "MANIFEST.md",
    "LICENSE",
    "NOTICE",
    "THIRD_PARTY_NOTICES.md",
    "ilmar.toml",
    ".ilmar/",
)
DEFAULT_CHECK_INFLUENCING_PATHS: tuple[str, ...] = (
    "tests/",
    "test/",
    "**/test_*.py",
    "**/*_test.py",
    "**/conftest.py",
    "pyproject.toml",
    "setup.py",
    "setup.cfg",
    "tox.ini",
    "noxfile.py",
    "pytest.ini",
    "mypy.ini",
    "ruff.toml",
    ".ruff.toml",
    ".coveragerc",
    ".pre-commit-config.yaml",
    "Makefile",
    "**/requirements*.txt",
    "**/*.lock",
    "package.json",
    "package-lock.json",
    "pnpm-lock.yaml",
    "go.mod",
    "go.sum",
    "Cargo.toml",
    "scripts/",
)
DEFAULT_CI_CONFIG_PATHS: tuple[str, ...] = (
    ".github/",
    ".gitlab-ci.yml",
    ".gitlab/",
    ".circleci/",
    "azure-pipelines.yml",
    "Jenkinsfile",
    ".buildkite/",
)


# -- policy ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class VerificationCommand:
    """One candidate-executed check: ``argv`` run in the verification sandbox."""

    name: str
    argv: tuple[str, ...]
    timeout_seconds: float = 600.0
    required: bool = True

    def __post_init__(self) -> None:
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9 ._:-]{0,63}", self.name):
            raise ValueError(f"invalid check name: {self.name!r}")
        if not self.argv or not all(isinstance(a, str) and a for a in self.argv):
            raise ValueError(f"check {self.name}: argv must be non-empty strings")
        if self.timeout_seconds <= 0:
            raise ValueError(f"check {self.name}: timeout_seconds must be positive")


@dataclass(frozen=True, slots=True)
class VerificationPolicy:
    """What verification runs and how changed paths are classified.

    Defined in Ilmar's configuration; nothing in it comes from the candidate.
    """

    image: str
    commands: tuple[VerificationCommand, ...]
    protected_paths: tuple[str, ...] = DEFAULT_PROTECTED_PATHS
    check_influencing_paths: tuple[str, ...] = DEFAULT_CHECK_INFLUENCING_PATHS
    ci_config_paths: tuple[str, ...] = DEFAULT_CI_CONFIG_PATHS
    limits: ResourceLimits = field(default_factory=ResourceLimits)

    def __post_init__(self) -> None:
        if not self.image:
            raise ValueError("a verification image is required")
        if not self.commands:
            raise ValueError("at least one verification command is required")
        names = [c.name for c in self.commands]
        if len(names) != len(set(names)):
            raise ValueError("check names must be unique")
        for pattern in (
            *self.protected_paths,
            *self.check_influencing_paths,
            *self.ci_config_paths,
        ):
            _compile(pattern)

    def digest(self) -> str:
        """``sha256:`` of the canonical policy, recorded in every evidence record."""
        canonical = {
            "image": self.image,
            "commands": [
                [c.name, list(c.argv), c.timeout_seconds, c.required] for c in self.commands
            ],
            "protected_paths": list(self.protected_paths),
            "check_influencing_paths": list(self.check_influencing_paths),
            "ci_config_paths": list(self.ci_config_paths),
            "limits": [self.limits.cpus, self.limits.memory_mb, self.limits.max_processes],
        }
        text = json.dumps(canonical, sort_keys=True, separators=(",", ":"))
        return "sha256:" + hashlib.sha256(text.encode()).hexdigest()


# -- evidence ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CheckEvidence:
    name: str
    check_class: CheckClass
    passed: bool
    detail: str = ""
    command: tuple[str, ...] = ()
    exit_code: int | None = None
    timed_out: bool = False
    duration_seconds: float = 0.0
    output_digest: str | None = None
    """``sha256:`` of stdout and stderr; the output itself is not evidence."""
    required: bool = True


@dataclass(frozen=True, slots=True)
class AgentReport:
    """What the agent *said*: kept apart from evidence and never a verdict (ADR-0010 §7)."""

    outcome: str
    summary: str


@dataclass(frozen=True, slots=True)
class EvidenceRecord:
    execution_id: str
    attempt: int
    recorded_at: datetime
    base_revision: str
    candidate_revision: str
    policy_digest: str
    checks: tuple[CheckEvidence, ...]
    protected_paths: tuple[str, ...]
    check_influencing_paths: tuple[str, ...]
    ci_config_paths: tuple[str, ...]
    independence: Independence
    verdict: Verdict
    requires_human_review: bool
    review_reasons: tuple[str, ...]
    agent_report: AgentReport

    def __post_init__(self) -> None:
        if self.recorded_at.tzinfo is None:
            raise ValueError("recorded_at must be timezone-aware")
        if self.attempt < 1:
            raise ValueError("attempt starts at 1")
        if self.verdict is Verdict.PUBLISH and self.requires_human_review:
            raise ValueError("a record that requires review cannot have verdict PUBLISH")

    def to_json(self) -> str:
        return json.dumps(_encode(self), sort_keys=True, separators=(",", ":"))

    @classmethod
    def from_json(cls, text: str) -> EvidenceRecord:
        data = json.loads(text)
        if data.get("format") != EVIDENCE_FORMAT:
            raise ValueError(f"unsupported evidence format: {data.get('format')!r}")
        return cls(
            execution_id=data["execution_id"],
            attempt=data["attempt"],
            recorded_at=datetime.fromisoformat(data["recorded_at"]),
            base_revision=data["base_revision"],
            candidate_revision=data["candidate_revision"],
            policy_digest=data["policy_digest"],
            checks=tuple(_decode_check(c) for c in data["checks"]),
            protected_paths=tuple(data["protected_paths"]),
            check_influencing_paths=tuple(data["check_influencing_paths"]),
            ci_config_paths=tuple(data["ci_config_paths"]),
            independence=Independence(data["independence"]),
            verdict=Verdict(data["verdict"]),
            requires_human_review=data["requires_human_review"],
            review_reasons=tuple(data["review_reasons"]),
            agent_report=AgentReport(**data["agent_report"]),
        )

    def digest(self) -> str:
        return "sha256:" + hashlib.sha256(self.to_json().encode()).hexdigest()


def _encode(record: EvidenceRecord) -> dict[str, Any]:
    return {
        "format": EVIDENCE_FORMAT,
        "execution_id": record.execution_id,
        "attempt": record.attempt,
        "recorded_at": record.recorded_at.isoformat(),
        "base_revision": record.base_revision,
        "candidate_revision": record.candidate_revision,
        "policy_digest": record.policy_digest,
        "checks": [
            {
                "name": c.name,
                "check_class": c.check_class.value,
                "passed": c.passed,
                "detail": c.detail,
                "command": list(c.command),
                "exit_code": c.exit_code,
                "timed_out": c.timed_out,
                "duration_seconds": c.duration_seconds,
                "output_digest": c.output_digest,
                "required": c.required,
            }
            for c in record.checks
        ],
        "protected_paths": list(record.protected_paths),
        "check_influencing_paths": list(record.check_influencing_paths),
        "ci_config_paths": list(record.ci_config_paths),
        "independence": record.independence.value,
        "verdict": record.verdict.value,
        "requires_human_review": record.requires_human_review,
        "review_reasons": list(record.review_reasons),
        "agent_report": {
            "outcome": record.agent_report.outcome,
            "summary": record.agent_report.summary,
        },
    }


def _decode_check(data: Mapping[str, Any]) -> CheckEvidence:
    return CheckEvidence(
        name=data["name"],
        check_class=CheckClass(data["check_class"]),
        passed=data["passed"],
        detail=data["detail"],
        command=tuple(data["command"]),
        exit_code=data["exit_code"],
        timed_out=data["timed_out"],
        duration_seconds=data["duration_seconds"],
        output_digest=data["output_digest"],
        required=data["required"],
    )
