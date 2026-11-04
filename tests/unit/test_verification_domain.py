# SPDX-License-Identifier: Apache-2.0
"""Verification policy, path classes and evidence records (#9, ADR-0018)."""

from __future__ import annotations

import dataclasses
from datetime import UTC, datetime

import pytest

from ilmar.domain.verification import (
    DEFAULT_CHECK_INFLUENCING_PATHS,
    DEFAULT_CI_CONFIG_PATHS,
    DEFAULT_PROTECTED_PATHS,
    AgentReport,
    CheckClass,
    CheckEvidence,
    EvidenceRecord,
    Independence,
    Verdict,
    VerificationCommand,
    VerificationPolicy,
    matching,
    path_matches,
)


@pytest.mark.parametrize(
    ("path", "pattern", "expected"),
    [
        ("tests/test_a.py", "tests/", True),
        ("tests/deep/x/y.py", "tests/", True),
        ("tests", "tests/", False),
        ("mytests/x.py", "tests/", False),
        ("src/test_app.py", "**/test_*.py", True),
        ("test_app.py", "**/test_*.py", True),
        ("src/app_test.py", "**/*_test.py", True),
        ("src/conftest.py", "**/conftest.py", True),
        ("conftest.py", "**/conftest.py", True),
        ("src/my_conftest.py", "**/conftest.py", False),
        ("pyproject.toml", "pyproject.toml", True),
        ("sub/pyproject.toml", "pyproject.toml", False),
        (".github/workflows/ci.yml", ".github/", True),
        ("x/.github/workflows/ci.yml", ".github/", False),
        ("requirements-dev.txt", "**/requirements*.txt", True),
        ("a/b/poetry.lock", "**/*.lock", True),
        ("a.b", "a?b", True),
        ("axb", "a?b", True),
        ("a/b", "a?b", False),
        ("src/x.py", "src/*.py", True),
        ("src/a/x.py", "src/*.py", False),
        ("AGENTS.md", "agents.md", False),
    ],
)
def test_path_matching(path: str, pattern: str, expected: bool) -> None:
    assert path_matches(path, pattern) is expected


@pytest.mark.parametrize("pattern", ["", "/abs"])
def test_invalid_patterns_are_rejected(pattern: str) -> None:
    with pytest.raises(ValueError, match="pattern"):
        path_matches("x", pattern)


def test_defaults_classify_the_obvious_cases() -> None:
    changed = [
        "AGENTS.md",
        ".github/workflows/ci.yml",
        "tests/unit/test_x.py",
        "src/pkg/conftest.py",
        "pyproject.toml",
        "src/pkg/app.py",
        "docs/readme.md",
    ]
    assert matching(changed, DEFAULT_PROTECTED_PATHS) == ("AGENTS.md",)
    assert matching(changed, DEFAULT_CI_CONFIG_PATHS) == (".github/workflows/ci.yml",)
    assert matching(changed, DEFAULT_CHECK_INFLUENCING_PATHS) == (
        "pyproject.toml",
        "src/pkg/conftest.py",
        "tests/unit/test_x.py",
    )


def policy(**kwargs: object) -> VerificationPolicy:
    settings: dict[str, object] = {
        "image": "localhost/verify:1",
        "commands": (VerificationCommand("tests", ("pytest", "-q")),),
    } | kwargs
    return VerificationPolicy(**settings)  # type: ignore[arg-type]


def test_policy_digest_is_stable_and_sensitive_to_every_field() -> None:
    base = policy()
    assert base.digest() == policy().digest()
    assert base.digest().startswith("sha256:")
    variants = [
        policy(image="localhost/verify:2"),
        policy(commands=(VerificationCommand("tests", ("pytest",)),)),
        policy(commands=(VerificationCommand("tests", ("pytest", "-q"), timeout_seconds=5),)),
        policy(commands=(VerificationCommand("tests", ("pytest", "-q"), required=False),)),
        policy(protected_paths=("X",)),
        policy(check_influencing_paths=("X",)),
        policy(ci_config_paths=("X",)),
    ]
    assert len({v.digest() for v in variants} | {base.digest()}) == len(variants) + 1


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"image": ""}, "image"),
        ({"commands": ()}, "at least one"),
        (
            {"commands": (VerificationCommand("a", ("x",)), VerificationCommand("a", ("y",)))},
            "unique",
        ),
        ({"protected_paths": ("/abs",)}, "pattern"),
    ],
)
def test_policy_validation(kwargs: dict[str, object], message: str) -> None:
    with pytest.raises(ValueError, match=message):
        policy(**kwargs)


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"name": "bad/name", "argv": ("x",)}, "check name"),
        ({"name": "ok", "argv": ()}, "argv"),
        ({"name": "ok", "argv": ("",)}, "argv"),
        ({"name": "ok", "argv": ("x",), "timeout_seconds": 0}, "timeout"),
    ],
)
def test_command_validation(kwargs: dict[str, object], message: str) -> None:
    with pytest.raises(ValueError, match=message):
        VerificationCommand(**kwargs)  # type: ignore[arg-type]


def record(**kwargs: object) -> EvidenceRecord:
    settings: dict[str, object] = {
        "execution_id": "e1",
        "attempt": 1,
        "recorded_at": datetime(2026, 11, 4, 12, 0, tzinfo=UTC),
        "base_revision": "a" * 40,
        "candidate_revision": "b" * 40,
        "policy_digest": "sha256:" + "0" * 64,
        "checks": (
            CheckEvidence("ci-config-paths", CheckClass.INDEPENDENT, True),
            CheckEvidence(
                "tests",
                CheckClass.CANDIDATE_EXECUTED,
                True,
                command=("pytest", "-q"),
                exit_code=0,
                duration_seconds=1.5,
                output_digest="sha256:" + "1" * 64,
            ),
        ),
        "protected_paths": (),
        "check_influencing_paths": ("tests/test_x.py",),
        "ci_config_paths": (),
        "independence": Independence.MODIFIED_CHECKS,
        "verdict": Verdict.PUBLISH_FOR_REVIEW,
        "requires_human_review": True,
        "review_reasons": ("modified-checks: tests/test_x.py",),
        "agent_report": AgentReport("completed", "Fixed it."),
    } | kwargs
    return EvidenceRecord(**settings)  # type: ignore[arg-type]


def test_evidence_round_trips_through_json() -> None:
    original = record()
    assert EvidenceRecord.from_json(original.to_json()) == original
    assert original.digest() == EvidenceRecord.from_json(original.to_json()).digest()


def test_evidence_json_is_versioned() -> None:
    text = record().to_json().replace('"format":1', '"format":99')
    with pytest.raises(ValueError, match="format"):
        EvidenceRecord.from_json(text)


def test_review_and_plain_publish_cannot_be_combined() -> None:
    with pytest.raises(ValueError, match="review"):
        record(verdict=Verdict.PUBLISH, requires_human_review=True)


def test_publishable_verdicts() -> None:
    assert {v for v in Verdict if v.publishable} == {Verdict.PUBLISH, Verdict.PUBLISH_FOR_REVIEW}


def test_evidence_has_no_field_for_ci_results() -> None:
    """CI results on a change request are never verification evidence (ADR-0010 §5)."""
    fields = {f.name for f in dataclasses.fields(EvidenceRecord)}
    assert not any("ci_result" in f or "external" in f for f in fields)
