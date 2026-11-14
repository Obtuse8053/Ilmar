# SPDX-License-Identifier: Apache-2.0
"""``ilmar gate record|revoke|show`` (ADR-0030 §2, ADR-0040, #91)."""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import sqlite3
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from ilmar import cli_gate
from ilmar.adapters.sqlite import SQLiteExecutionStore
from ilmar.cli import _build_parser
from ilmar.domain.events import ActorKind, EventType

REPO_ROOT = Path(__file__).resolve().parents[2]
NOW = datetime(2026, 3, 1, 12, tzinfo=UTC)
FIXTURE = "tests/fixtures/claude_code_streams/2.1.318-recorded.json"
RECORD = [
    "--mode", "supervised", "--repository-id", "987654321",
    "--repository", "someone/ilmar-disposable", "--agent-version", "2.1.318",
    "--evidence", "https://github.com/Obtuse8053/Ilmar/pull/130",
    "--live-test", "https://github.com/Obtuse8053/Ilmar/actions/runs/43567969214",
    "--fixture", FIXTURE, "--gate1", "https://github.com/Obtuse8053/Ilmar/issues/93",
]  # fmt: skip


@pytest.fixture
def checkout(tmp_path: Path) -> Path:
    root = tmp_path / "checkout"
    fixture = root / FIXTURE
    fixture.parent.mkdir(parents=True)
    fixture.write_text(json.dumps({"provenance": "recorded", "claude_code_version": "2.1.318",
                                   "events": []}))  # fmt: skip
    return root


@pytest.fixture
def config(tmp_path: Path) -> Path:
    path = tmp_path.resolve() / "ilmar.toml"
    path.write_text((REPO_ROOT / "ilmar.example.toml").read_text())
    return path


def state(config: Path) -> Path:
    return config.parent / ".ilmar"


def ilmar(*argv: str, checkout: Path, now: datetime = NOW) -> tuple[int, str, str]:
    args: argparse.Namespace = _build_parser().parse_args(list(argv))
    out, err = io.StringIO(), io.StringIO()
    code = cli_gate.run(args, out=out, err=err, now=lambda: now, checkout=checkout)
    return code, out.getvalue(), err.getvalue()


def test_a_person_records_shows_and_revokes_a_gate(config: Path, checkout: Path) -> None:
    code, out, err = ilmar("gate", "record", "--config", str(config), "--as", "alice", *RECORD,
                           checkout=checkout)  # fmt: skip
    assert (code, err) == (0, "")
    assert "recorded: the supervised gate, for repository 987654321" in out
    code, out, _ = ilmar("gate", "show", "--config", str(config), checkout=checkout)
    digest = hashlib.sha256((checkout / FIXTURE).read_bytes()).hexdigest()
    assert code == 0
    assert out.startswith("#1 supervised active: repository 987654321")
    assert f"sha256:{digest}" in out
    code, _, err = ilmar("gate", "record", "--config", str(config), "--as", "alice", *RECORD,
                         checkout=checkout)  # fmt: skip
    assert (code, "already active" in err) == (3, True)
    code, out, _ = ilmar("gate", "revoke", "--config", str(config), "--as", "bob",
                         "--mode", "supervised", checkout=checkout,
                         now=NOW + timedelta(minutes=1))  # fmt: skip
    assert code == 0
    assert "revoked" in out
    code, out, _ = ilmar("gate", "show", "--config", str(config), checkout=checkout)
    assert "#1 supervised revoked 2026-03-01T12:01:00+00:00 by bob" in out
    with SQLiteExecutionStore(state(config) / "executions.sqlite3") as store:
        gates = [e for e in store.events() if e.type.value.startswith("instance.gate")]
    assert [(e.type, e.actor.kind, e.actor.ref) for e in gates] == [
        (EventType.INSTANCE_GATE_RECORDED, ActorKind.HUMAN, "alice"),
        (EventType.INSTANCE_GATE_REVOKED, ActorKind.HUMAN, "bob"),
    ]
    assert gates[0].data["fixture_sha256"] == digest


def test_revoking_without_a_record_is_refused(config: Path, checkout: Path) -> None:
    code, _, err = ilmar("gate", "revoke", "--config", str(config), "--as", "bob",
                         "--mode", "supervised", checkout=checkout)  # fmt: skip
    assert (code, "no active supervised gate record" in err) == (3, True)


@pytest.mark.parametrize(
    ("fixture", "message"),
    [
        ({"provenance": "synthetic", "claude_code_version": "2.1.318"}, "recorded fixture"),
        ({"provenance": "recorded", "claude_code_version": "2.1.302"}, "another agent version"),
        ({"claude_code_version": "2.1.318"}, "recorded fixture"),
        (None, "not JSON"),
    ],
)
def test_the_fixture_must_be_recorded_for_the_same_version(
    config: Path, checkout: Path, fixture: dict[str, str] | None, message: str
) -> None:
    (checkout / FIXTURE).write_text("not json" if fixture is None else json.dumps(fixture))
    code, _, err = ilmar("gate", "record", "--config", str(config), "--as", "alice", *RECORD,
                         checkout=checkout)  # fmt: skip
    assert (code, message in err) == (3, True)
    assert not (state(config) / "executions.sqlite3").exists()


@pytest.mark.parametrize(
    ("flag", "value", "message"),
    [
        ("--fixture", "../outside.json", "under tests/fixtures/"),
        ("--fixture", "/etc/passwd", "inside this checkout"),
        ("--fixture", "tests/fixtures/missing.json", "cannot be read"),
        ("--agent-version", "2.1", "another agent version"),
        ("--evidence", "https://example.com/pull/1", "pull request URL"),
        ("--live-test", "https://github.com/o/r/pull/1", "Actions run URL"),
        ("--repository", "not a repository", "owner/name"),
        ("--repository-id", "0", "positive integer"),
    ],
)
def test_bad_evidence_is_refused(
    config: Path, checkout: Path, flag: str, value: str, message: str
) -> None:
    argv = list(RECORD)
    argv[argv.index(flag) + 1] = value
    code, _, err = ilmar("gate", "record", "--config", str(config), "--as", "alice", *argv,
                         checkout=checkout)  # fmt: skip
    assert code == 3
    assert message in err


def test_the_repository_id_must_be_the_configured_one(config: Path, checkout: Path) -> None:
    config.write_text(config.read_text() + "\n[autonomy]\nrepository_id = 111\n")
    code, _, err = ilmar("gate", "record", "--config", str(config), "--as", "alice", *RECORD,
                         checkout=checkout)  # fmt: skip
    assert (code, "not the configured [autonomy] repository_id 111" in err) == (3, True)


def test_a_stored_gate_record_cannot_be_removed_or_rewritten(config: Path, checkout: Path) -> None:
    ilmar("gate", "record", "--config", str(config), "--as", "alice", *RECORD, checkout=checkout)
    path = state(config) / "executions.sqlite3"
    with closing(sqlite3.connect(path)) as conn:
        for statement in ("DELETE FROM gate_record",
                          "UPDATE gate_record SET evidence = '{}'",
                          "UPDATE gate_record SET recorded_by = 'mallory'"):  # fmt: skip
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute(statement)
        conn.execute("UPDATE gate_record SET revoked_at = 'x', revoked_by = 'y'")  # once
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("UPDATE gate_record SET revoked_at = NULL, revoked_by = NULL")
