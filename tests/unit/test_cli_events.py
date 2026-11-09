# SPDX-License-Identifier: Apache-2.0
"""``ilmar events`` and ``ilmar events verify`` (#17 part 4, ADR-0024 §6 and §8).

* strictly read-only: no migration, no write, no new file, no lock, on
  pre-migration, older and covered stores alike;
* exit codes 0, 1, 2 and 3, with historical findings kept apart from integrity
  failures;
* filters and both output formats, and ``--follow``;
* corrupted streams, pre-migration databases and normal recovery history.
"""

from __future__ import annotations

import io
import json
import sqlite3
from collections.abc import Callable
from contextlib import closing
from pathlib import Path

import pytest

from event_store_support import MACHINE, digest, dump, populate
from ilmar import cli_events
from ilmar.adapters.sqlite import SCHEMA_V1, SQLiteExecutionStore
from ilmar.cli import _build_parser, main
from ilmar.core.layout import WorkspaceLayout
from ilmar.core.ownership import Ownership
from ilmar.domain.execution import ExecutionState
from ilmar.domain.ledger import Commit
from recovery_support import to_previous_schema

from helpers import at

REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def old_store(tmp_path: Path) -> Path:
    """A store as the previous schema left it, with crash leftovers (no events)."""
    path = tmp_path / "executions.sqlite3"
    with SQLiteExecutionStore(path) as store:
        populate(store)
    to_previous_schema(path)
    return path


@pytest.fixture
def covered(old_store: Path) -> Path:
    """The same store after the lock-held migration, with covered work after it."""
    with SQLiteExecutionStore(old_store) as store:
        fence = store.claim_instance("inst", now=at(10))
        execution = store.get("q")
        assert execution is not None
        updated, step = MACHINE.advance(execution, ExecutionState.CLAIMED, now=at(11), reason="r")
        store.commit(fence, Commit(applied=((updated, step),)))
    return old_store


def ilmar(*argv: str, sleep: Callable[[float], None] | None = None) -> tuple[int, str, str]:
    """Run ``ilmar events …``; return (exit code, stdout, stderr)."""
    words = list(argv)
    try:
        parsed = _build_parser().parse_args(words)
    except SystemExit:
        return main(words), "", ""  # a usage error: main maps it
    out, err = io.StringIO(), io.StringIO()
    code = cli_events.run(parsed, out=out, err=err, sleep=sleep or _no_sleep)
    return code, out.getvalue(), err.getvalue()


def _no_sleep(seconds: float) -> None:
    raise AssertionError("only --follow sleeps")


def stored_lines(path: Path) -> list[str]:
    with closing(sqlite3.connect(path)) as conn:
        return [line for (line,) in conn.execute("SELECT line FROM events ORDER BY seq")]


def edit(path: Path, *statements: str) -> None:
    with closing(sqlite3.connect(path)) as conn:
        for statement in statements:
            conn.execute(statement)
        conn.commit()


# -- strictly read-only -------------------------------------------------------------------


def v1_store(path: Path) -> Path:
    with closing(sqlite3.connect(path)) as conn:
        conn.executescript(SCHEMA_V1 + "PRAGMA user_version = 1;")
    return path


COMMANDS = [
    ("events",),
    ("events", "--format", "text"),
    ("events", "--type", "operation", "--limit", "3"),
    ("events", "verify"),
    ("events", "verify", "--format", "json"),
]


@pytest.mark.parametrize("command", COMMANDS, ids=" ".join)
@pytest.mark.parametrize("kind", ["previous-schema", "schema-1", "covered"])
def test_every_command_leaves_the_store_byte_for_byte_unchanged(
    tmp_path: Path, old_store: Path, kind: str, command: tuple[str, ...]
) -> None:
    if kind == "schema-1":
        path = v1_store(tmp_path / "v1.sqlite3")
    elif kind == "covered":
        with SQLiteExecutionStore(old_store) as store:
            store.claim_instance("inst", now=at(10))
        path = old_store
    else:
        path = old_store
    before, rows = digest(path), dump(path)
    files = {p.name for p in tmp_path.iterdir()}
    code, _, err = ilmar(*command, "--db", str(path))
    assert code == (0 if kind == "covered" else 2), err
    assert digest(path) == before
    assert dump(path) == rows
    # Nothing new beyond SQLite's own (empty) WAL side files.
    assert {p.name for p in tmp_path.iterdir()} - files <= {path.name + "-wal", path.name + "-shm"}
    if kind != "covered":
        with closing(sqlite3.connect(path)) as conn:
            tables = {n for (n,) in conn.execute("SELECT name FROM sqlite_master")}
        assert "events" not in tables  # never migrated


def test_no_coverage_is_exit_2_with_a_plain_message(old_store: Path) -> None:
    code, out, err = ilmar("events", "--db", str(old_store))
    assert (code, out) == (2, "")
    assert "no event coverage" in err
    assert "nothing was changed" in err
    code, out, _ = ilmar("events", "verify", "--db", str(old_store))
    assert code == 2
    assert "result: no event coverage" in out
    code, out, _ = ilmar("events", "verify", "--db", str(old_store), "--format", "json")
    assert json.loads(out)["result"] == "no-coverage"
    assert json.loads(out)["exit_code"] == 2


def test_it_reads_while_a_coordinator_holds_the_instance_lock(tmp_path: Path) -> None:
    state = tmp_path.resolve() / "state"
    state.mkdir()
    path = tmp_path / "executions.sqlite3"
    store = SQLiteExecutionStore(path)
    with Ownership.take(layout=WorkspaceLayout.under(state), store=store, instance_id="inst",
                        now=at(0)):  # fmt: skip
        code, out, _ = ilmar("events", "--db", str(path))
        assert code == 0
        assert len(out.splitlines()) == 2  # the coverage marker and ownership.acquired
        assert ilmar("events", "verify", "--db", str(path))[0] == 0
    store.close()


def test_a_missing_store_is_exit_3_and_creates_nothing(tmp_path: Path) -> None:
    missing = tmp_path / "nowhere" / "executions.sqlite3"
    for command in (("events",), ("events", "verify")):
        code, _, err = ilmar(*command, "--db", str(missing))
        assert code == 3
        assert "no execution store" in err
    assert list(tmp_path.iterdir()) == []


def config_file(tmp_path: Path) -> Path:
    """The example configuration, its store under ``tmp_path``. Nothing else (no
    container runtime, agent or secret) is needed to read events."""
    path = tmp_path / "ilmar.toml"
    path.write_text((REPO_ROOT / "ilmar.example.toml").read_text())
    return path


def test_the_config_names_the_store_and_nothing_else_is_planned(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PATH", "/nonexistent")  # no podman, no claude, no git: not needed
    config = config_file(tmp_path)
    code, _, err = ilmar("events", "--config", str(config))
    assert code == 3
    assert "no execution store" in err or "cannot read" in err
    assert not (tmp_path / ".ilmar").exists()  # not even the state directory is created
    store_path = tmp_path / ".ilmar" / "executions.sqlite3"
    store_path.parent.mkdir()
    with SQLiteExecutionStore(store_path) as store:
        store.claim_instance("inst", now=at(0))
    code, out, _ = ilmar("events", "--config", str(config))
    assert code == 0
    assert out.splitlines() == stored_lines(store_path)


def test_a_store_kind_without_an_event_log_is_a_configuration_error(tmp_path: Path) -> None:
    config = config_file(tmp_path)
    config.write_text(config.read_text().replace('kind = "sqlite"', 'kind = "memory"'))
    code, _, err = ilmar("events", "--config", str(config))
    assert code == 3
    assert "error:" in err


@pytest.mark.parametrize(
    "argv",
    [
        ("events",),  # neither --config nor --db
        ("events", "--db", "x", "--config", "y"),
        ("events", "--db", "x", "--after", "-1"),
        ("events", "--db", "x", "--limit", "0"),
        ("events", "--db", "x", "--interval", "0"),
        ("events", "check", "--db", "x"),
    ],
)
def test_usage_errors_are_exit_3_never_the_no_coverage_code(argv: tuple[str, ...]) -> None:
    assert main(list(argv)) == 3


@pytest.mark.parametrize(
    "extra",
    [
        ("--type", "operation.exploded"),
        ("--reason", "unheard-of"),
        ("--format", "json"),
        ("--failures-only",),
    ],
)
def test_invalid_listing_options_are_exit_3(covered: Path, extra: tuple[str, ...]) -> None:
    code, _, err = ilmar("events", "--db", str(covered), *extra)
    assert code == 3
    assert err.startswith("error:")


@pytest.mark.parametrize(
    "extra",
    [("--after", "3"), ("--execution", "q"), ("--type", "operation"), ("--follow",),
     ("--format", "jsonl"), ("--limit", "1")],
)  # fmt: skip
def test_verify_always_checks_everything(covered: Path, extra: tuple[str, ...]) -> None:
    code, _, err = ilmar("events", "verify", "--db", str(covered), *extra)
    assert code == 3
    assert "error:" in err


# -- output and filters -------------------------------------------------------------------


def test_jsonl_is_every_event_exactly_as_stored_in_seq_order(covered: Path) -> None:
    code, out, _ = ilmar("events", "--db", str(covered))
    assert code == 0
    assert out.splitlines() == stored_lines(covered)
    seqs = [json.loads(line)["seq"] for line in out.splitlines()]
    assert seqs == sorted(seqs) == list(range(1, len(seqs) + 1))


def test_after_is_a_cursor_and_limit_counts_matches(covered: Path) -> None:
    every = stored_lines(covered)
    code, out, _ = ilmar("events", "--db", str(covered), "--after", "5", "--limit", "3")
    assert code == 0
    assert out.splitlines() == every[5:8]
    assert ilmar("events", "--db", str(covered), "--after", str(len(every)))[1] == ""


def test_filters_by_execution_type_family_and_reason(covered: Path) -> None:
    def seen(*extra: str) -> list[dict[str, object]]:
        code, out, _ = ilmar("events", "--db", str(covered), *extra)
        assert code == 0
        return [json.loads(line) for line in out.splitlines()]

    assert {e["execution"] for e in seen("--execution", "q", "--execution", "c")} == {"q", "c"}
    assert {e["type"] for e in seen("--type", "events.coverage_started")} == {
        "events.coverage_started"}  # fmt: skip
    families = {str(e["type"]).split(".")[0] for e in seen("--type", "operation",
                                                             "--type", "resource")}  # fmt: skip
    assert families == {"operation", "resource"}
    assert [e["type"] for e in seen("--execution", "q", "--type", "execution.transitioned")] == [
        "execution.transitioned"]  # fmt: skip
    assert seen("--reason", "interrupted") == []  # a clean migrated history


def test_text_is_one_readable_line_per_event(covered: Path) -> None:
    code, out, _ = ilmar("events", "--db", str(covered), "--format", "text",
                         "--execution", "q")  # fmt: skip
    assert code == 0
    lines = out.splitlines()
    assert len(lines) == 2
    assert "execution.baseline" in lines[0]
    assert "state=queued" in lines[0]
    assert "execution.transitioned" in lines[1]
    assert "from=queued to=claimed" in lines[1]


def test_follow_prints_new_events_once_and_ends_cleanly(covered: Path) -> None:
    start = len(stored_lines(covered))
    polls: list[int] = []

    def sleep(seconds: float) -> None:
        polls.append(len(polls))
        assert seconds == 0.25
        if len(polls) == 1:
            with SQLiteExecutionStore(covered) as store:  # another process writes meanwhile
                store.claim_instance("inst", now=at(20))
        elif len(polls) == 3:
            raise KeyboardInterrupt

    code, out, _ = ilmar("events", "--db", str(covered), "--follow", "--interval", "0.25",
                         "--type", "ownership", sleep=sleep)  # fmt: skip
    assert code == 0
    printed = [json.loads(line) for line in out.splitlines()]
    assert [e["seq"] for e in printed] == sorted({e["seq"] for e in printed})  # no duplicates
    assert printed[-1]["seq"] == start + 1
    assert [e["type"] for e in printed] == ["ownership.acquired"] * len(printed)


# -- verify: historical versus integrity --------------------------------------------------


def test_a_migrated_store_verifies_with_its_history_listed(covered: Path) -> None:
    code, out, _ = ilmar("events", "verify", "--db", str(covered))
    assert code == 0
    assert "integrity failures: 0" in out
    assert out.rstrip().endswith("result: ok")
    assert "execution:v: 5 transitions and 1 evidence records before coverage" in out
    quiet = ilmar("events", "verify", "--db", str(covered), "--failures-only")[1]
    assert "historical findings (information): " in quiet
    assert "before coverage" not in quiet
    code, out, _ = ilmar("events", "verify", "--db", str(covered), "--format", "json")
    report = json.loads(out)
    assert (report["exit_code"], report["result"], report["covered"]) == (0, "ok", True)
    assert report["failures"] == []
    assert {"check", "subject", "detail"} == set(report["historical"][0])
    assert report["events"] == len(stored_lines(covered))


CORRUPTIONS: dict[str, tuple[tuple[str, ...], int, str]] = {
    "a row changed without an event": (
        ("UPDATE executions SET state = 'provisioning', version = 3 WHERE id = 'q'",),
        0, "fold-mismatch",
    ),
    "an execution inserted with no events": (
        ("INSERT INTO executions (id, task_provider, task_key, state, is_terminal, attempt, "
         "version, created_at, updated_at) VALUES ('ghost', 'fake', 'ghost', 'queued', 0, 1, 1, "
         "'2026-11-08T00:00:00.000000+00:00', '2026-11-08T00:00:00.000000+00:00')",),
        0, "no-coverage-event",
    ),
    "a deleted event": (
        ("DROP TRIGGER events_are_append_only_delete", "DELETE FROM events WHERE seq = 4"),
        0, "seq-gap",
    ),
    "a line that is not JSON": (
        ("DROP TRIGGER events_are_append_only_update",
         "UPDATE events SET line = substr(line, 2) WHERE seq = 4"),
        1, "corrupt-event",
    ),
    "a line that is not canonical": (
        ("DROP TRIGGER events_are_append_only_update",
         "UPDATE events SET line = replace(line, ',', ', ') WHERE seq = 4"),
        1, "corrupt-event",
    ),
    "a line whose seq is not its row's": (
        ("DROP TRIGGER events_are_append_only_update",
         "UPDATE events SET line = replace(line, '\"seq\":4', '\"seq\":40') WHERE seq = 4"),
        1, "seq-gap",
    ),
    "an unknown event type": (
        ("DROP TRIGGER events_are_append_only_update",
         "UPDATE events SET line = replace(line, 'execution.baseline', 'execution.exploded') "
         "WHERE seq = 4"),
        1, "corrupt-event",
    ),
}  # fmt: skip


@pytest.mark.parametrize("name", sorted(CORRUPTIONS))
def test_a_corrupted_stream_is_an_integrity_failure(covered: Path, name: str) -> None:
    statements, export_code, check = CORRUPTIONS[name]
    edit(covered, *statements)
    before = digest(covered)
    code, out, _ = ilmar("events", "verify", "--db", str(covered))
    assert code == 1
    assert f"FAIL {check} " in out
    assert out.rstrip().endswith("result: integrity-failure")
    report = json.loads(ilmar("events", "verify", "--db", str(covered), "--format", "json")[1])
    assert report["exit_code"] == 1
    assert check in {f["check"] for f in report["failures"]}
    # Never mixed: a failure is not also history, and history is still history.
    failing = {(f["check"], f["subject"]) for f in report["failures"]}
    assert not failing & {(f["check"], f["subject"]) for f in report["historical"]}
    assert any(f["subject"] == "execution:v" for f in report["historical"])
    # The exporter stops at a corrupt line and says where; rows and gaps are verify's job.
    code, out, err = ilmar("events", "--db", str(covered))
    assert code == export_code
    if export_code == 1:
        assert "the event at seq 4 is corrupt" in err
        assert out.splitlines() == stored_lines(covered)[:3]  # everything before it
    assert digest(covered) == before
