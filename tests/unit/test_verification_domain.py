# SPDX-License-Identifier: Apache-2.0
"""Verification policy, path classes and evidence records (#9, ADR-0018)."""

from __future__ import annotations

import dataclasses
import json
from datetime import UTC, datetime

import pytest

from ilmar.domain.verification import (
    DEFAULT_CHECK_INFLUENCING_PATHS,
    DEFAULT_CI_CONFIG_PATHS,
    DEFAULT_PROTECTED_PATHS,
    EVIDENCE_FORMAT,
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
        policy(commands=(VerificationCommand("tests", ("pytest", "-q"), job="other"),)),
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


def test_records_without_a_run_keep_their_original_bytes_and_digest() -> None:
    """Format 1 is still written for run-less records, so stored digests still verify.

    The digest was computed by the code before #11c on the same record.
    """
    old = record()
    assert json.loads(old.to_json())["format"] == 1
    assert "run" not in json.loads(old.to_json())
    assert old.digest() == "sha256:f256a641ba67d5fd4511193d9bb520c407dd5192eb4be370a59f1099cfa20ace"


def test_evidence_names_the_verification_run_it_came_from() -> None:
    new = record(run=2)
    data = json.loads(new.to_json())
    assert data["format"] == EVIDENCE_FORMAT == 2
    assert data["run"] == 2
    assert EvidenceRecord.from_json(new.to_json()) == new
    assert new.digest() != record(run=3).digest() != record().digest()


@pytest.mark.parametrize("run", [0, -1])
def test_a_run_number_starts_at_one(run: int) -> None:
    with pytest.raises(ValueError, match="run"):
        record(run=run)


@pytest.mark.parametrize("bad", ["true", '"1"', "1.0", "0", "null"])
def test_format_two_accepts_only_a_whole_positive_run(bad: str) -> None:
    text = record(run=1).to_json().replace('"run":1,', f'"run":{bad},')
    with pytest.raises(ValueError, match="run"):
        EvidenceRecord.from_json(text)


def test_format_two_requires_a_run() -> None:
    text = record(run=1).to_json().replace('"run":1,', "")
    with pytest.raises(ValueError, match="run"):
        EvidenceRecord.from_json(text)


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


def test_jobs_group_commands_in_order_of_first_appearance() -> None:
    a = VerificationCommand("a", ("a",), job="static")
    b = VerificationCommand("b", ("b",))
    c = VerificationCommand("c", ("c",), job="static")
    assert policy(commands=(a, b, c)).jobs() == (("static", (a, c)), ("default", (b,)))


@pytest.mark.parametrize(
    ("path", "classes"),
    [
        ("src/ilmar/ruff.toml", "check"),  # nested ruff configuration disables lint below it
        ("src/pkg/pyproject.toml", "check"),
        (".mypy.ini", "check"),
        ("src/sitecustomize.py", "check"),
        ("src/evil.pth", "check"),
        ("src/ilmar/AGENTS.md", "protected"),
        ("docs/CLAUDE.md", "protected"),
        (".claude/settings.json", "protected"),  # hooks execute in later agent sessions
        (".mcp.json", "protected"),
        (".vscode/tasks.json", "protected"),
        (".travis.yml", "ci"),
        (".woodpecker/build.yml", "ci"),
    ],
)
def test_default_path_classes_cover_indirect_influence(path: str, classes: str) -> None:
    table = {
        "check": DEFAULT_CHECK_INFLUENCING_PATHS,
        "protected": DEFAULT_PROTECTED_PATHS,
        "ci": DEFAULT_CI_CONFIG_PATHS,
    }
    assert matching((path,), table[classes]) == (path,)
