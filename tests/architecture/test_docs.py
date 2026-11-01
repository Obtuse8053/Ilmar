# SPDX-License-Identifier: Apache-2.0
"""Keep the research and decision knowledge base internally consistent (ADR-0008)."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
DOCS = REPO_ROOT / "docs"
SOURCES = DOCS / "research" / "sources.md"
FINDINGS = DOCS / "research" / "findings.md"
DECISIONS = DOCS / "architecture" / "decisions"

_FENCE = re.compile(r"```.*?```", re.DOTALL)
_LINK = re.compile(r"\[[^\]]*\]\(([^)\s]+)\)")
_SOURCE_REF = re.compile(r"\[(R\d+)\]")
_FINDING_REF = re.compile(r"\bF-\d{3}\b")
_EVIDENCE = re.compile(r"Evidence: \*\*(Established|Emerging|Experimental)\*\*")
_STATUS = re.compile(r"Status: (open|proposed|accepted|rejected|superseded)\b")


def _markdown_files() -> list[Path]:
    skip = {".git", ".venv", "node_modules"}
    return sorted(
        p for p in REPO_ROOT.rglob("*.md") if not skip.intersection(p.relative_to(REPO_ROOT).parts)
    )


def _prose(path: Path) -> str:
    return _FENCE.sub("", path.read_text())


def _adr_files() -> list[Path]:
    return sorted(DECISIONS.glob("[0-9][0-9][0-9][0-9]-*.md"))


def _registered_sources() -> set[str]:
    return set(re.findall(r"^\| (R\d+) \|", SOURCES.read_text(), re.MULTILINE))


def _finding_ids() -> set[str]:
    return set(re.findall(r"^### (F-\d{3}):", FINDINGS.read_text(), re.MULTILINE))


@pytest.mark.parametrize("path", _markdown_files(), ids=lambda p: str(p.relative_to(REPO_ROOT)))
def test_relative_links_resolve(path: Path) -> None:
    broken = []
    for target in _LINK.findall(_prose(path)):
        if re.match(r"^[a-z]+:", target) or target.startswith("#"):
            continue
        if not (path.parent / target.split("#", 1)[0]).exists():
            broken.append(target)
    assert not broken, f"{path.relative_to(REPO_ROOT)} has broken links: {broken}"


def test_cited_sources_are_registered() -> None:
    registered = _registered_sources()
    assert {f"R{n}" for n in range(1, 9)} <= registered, "MANIFEST §9 sources must be registered"
    for path in [FINDINGS, *_adr_files()]:
        unknown = set(_SOURCE_REF.findall(_prose(path))) - registered
        assert not unknown, f"{path.name} cites unregistered sources: {sorted(unknown)}"


def test_cited_findings_exist() -> None:
    known = _finding_ids()
    for path in [*_adr_files(), DOCS / "roadmap.md"]:
        unknown = set(_FINDING_REF.findall(_prose(path))) - known
        assert not unknown, f"{path.name} cites unknown findings: {sorted(unknown)}"


def test_every_finding_has_evidence_level_and_status() -> None:
    sections = re.split(r"^### ", FINDINGS.read_text(), flags=re.MULTILINE)[1:]
    assert sections, "findings.md has no findings"
    for section in sections:
        title = section.splitlines()[0]
        assert _EVIDENCE.search(section), f"{title}: missing or invalid evidence level"
        assert _STATUS.search(section), f"{title}: missing or invalid status"


def test_adr_index_lists_every_adr() -> None:
    index = (DECISIONS / "README.md").read_text()
    for adr in _adr_files():
        assert f"({adr.name})" in index, f"{adr.name} is missing from the ADR index"


@pytest.mark.parametrize("path", _adr_files(), ids=lambda p: p.name)
def test_adrs_follow_the_template(path: Path) -> None:
    text = path.read_text()
    number = path.name[:4]
    assert text.startswith(f"# ADR-{number}: "), "title must be '# ADR-NNNN: ...'"
    for heading in (
        "## Context and problem",
        "## Considered options",
        "## Decision",
        "## Consequences",
        "## Revisit when",
    ):
        assert heading in text, f"missing section {heading!r}"
    assert re.search(r"^- Status: \w", text, re.MULTILINE), "missing status"


STATUS_SECTIONS = (
    "## Current milestone",
    "## In progress",
    "## Next up",
    "## Decisions needed from the maintainer",
    "## Change log",
)


def test_status_file_has_resume_sections() -> None:
    """docs/STATUS.md is the resume point for new sessions (ADR-0009)."""
    text = (DOCS / "STATUS.md").read_text()
    missing = [s for s in STATUS_SECTIONS if s not in text]
    assert not missing, f"docs/STATUS.md is missing sections: {missing}"
    assert re.search(r"^- \d{4}-\d{2}-\d{2}: ", text, re.MULTILINE), (
        "change log needs dated entries"
    )


def test_agents_md_points_to_resume_files() -> None:
    text = (REPO_ROOT / "AGENTS.md").read_text()
    assert "docs/STATUS.md" in text
    assert "docs/workflow.md" in text
