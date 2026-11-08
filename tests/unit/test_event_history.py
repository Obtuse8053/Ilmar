# SPDX-License-Identifier: Apache-2.0
"""ADR-0024 invariant I11: the event history rebuilds the stored state.

``Replay`` plays the store's part (ADR-0024 §2, layer 2) the way the store
adapter will: it applies a commit's rows, derives the commit's events, fills
each change's ``from`` from the previous row and assigns ``seq``. ``verify``
then checks I11 from creation or from the migration baseline (§8).
"""

from __future__ import annotations

import dataclasses
import json
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from typing import Any

import pytest

from ilmar.domain.event_history import (
    FindingClass,
    Report,
    StoredState,
    load_events,
    verify,
    verify_lines,
)
from ilmar.domain.events import (
    BaselineExecution,
    CorruptEventError,
    Event,
    EventContext,
    EventType,
    baseline_events,
    derive_events,
    resource_ref,
)
from ilmar.domain.execution import Execution, ExecutionState, Transition
from ilmar.domain.ledger import (
    Commit,
    Fence,
    Operation,
    OperationKind,
    OperationStatus,
    Resource,
    ResourceKind,
    ResourceStatus,
)
from ilmar.domain.tasks import TaskRef

NOW = datetime(2026, 11, 8, 12, tzinfo=UTC)
FENCE = Fence("inst", 4)
S, ST, R = ExecutionState, OperationStatus, ResourceStatus
DIGEST = "sha256:" + "a" * 64


@dataclass
class Replay:
    executions: dict[str, Execution] = field(default_factory=dict)
    operations: dict[str, Operation] = field(default_factory=dict)
    resources: dict[str, Resource] = field(default_factory=dict)
    transitions: dict[str, int] = field(default_factory=dict)
    evidence: dict[str, int] = field(default_factory=dict)
    events: list[Event] = field(default_factory=list)

    def append(self, *events: Event) -> None:
        for e in events:
            self.events.append(e.with_seq(len(self.events) + 1))

    def migrate(self, *, from_schema_version: int = 3) -> None:
        """The baseline, from what is stored now (ADR-0024 §8)."""
        self.append(*baseline_events(
            at=NOW, instance="inst", epoch=FENCE.epoch, from_schema_version=from_schema_version,
            executions=[BaselineExecution(e, self.transitions.get(e.id, 0),
                                          self.evidence.get(e.id, 0))
                        for e in self.executions.values()],
            operations=list(self.operations.values()), resources=list(self.resources.values()),
        ))  # fmt: skip

    def commit(self, changes: Commit, context: EventContext | None = None,
               *, evidence_for: str | None = None, covered: bool = True) -> None:  # fmt: skip
        """Apply ``changes``; with ``covered`` (after the migration) also its events."""
        context = context or EventContext(at=NOW, instance="inst", epoch=FENCE.epoch,
                                          pass_id="p-0a0b0c")  # fmt: skip
        derived = list(derive_events(changes, context)) if covered else []
        position = len(changes.created) + len(changes.applied)
        for operation in changes.operations:
            before = self.operations.get(operation.key)
            if covered and before is not None:
                derived[position] = derived[position].with_previous(before.status.value)
            self.operations[operation.key] = operation
            position += 1
        for resource in changes.resources:
            prior = self.resources.get(resource.key)
            if covered and prior is not None:
                derived[position] = derived[position].with_previous(prior.status.value)
            self.resources[resource.key] = resource
            position += 1
        for execution, _ in (*changes.created, *changes.applied):
            self.executions[execution.id] = execution
            self.transitions[execution.id] = self.transitions.get(execution.id, 0) + 1
        if evidence_for is not None:
            self.evidence[evidence_for] = self.evidence.get(evidence_for, 0) + 1
            if covered:
                derived.append(Event.create(
                    EventType.EVIDENCE_RECORDED, at=NOW, instance="inst", epoch=FENCE.epoch,
                    actor=context.actor, execution=evidence_for, attempt=1, run=1,
                    data={"run": 1, "digest": DIGEST, "verdict": "publish",
                          "policy_digest": DIGEST},
                ))  # fmt: skip
        self.append(*derived)

    def stored(self) -> StoredState:
        return StoredState(list(self.executions.values()), list(self.operations.values()),
                           list(self.resources.values()), dict(self.transitions),
                           dict(self.evidence))  # fmt: skip

    def verify(self) -> Report:
        return verify(self.events, self.stored())


TASK = TaskRef("github", "Obtuse8053/ilmar-disposable#6")


def created(execution_id: str = "e1") -> Commit:
    e = Execution(execution_id, TASK, S.QUEUED, 1, 1, NOW, NOW)
    return Commit(created=((e, Transition(execution_id, None, S.QUEUED, NOW, "queued")),))


def move(replay: Replay, target: ExecutionState, execution_id: str = "e1") -> Commit:
    current = replay.executions[execution_id]
    after = replace(current, state=target, version=current.version + 1)
    return Commit(applied=((after, Transition(execution_id, current.state, target, NOW, "")),))


def intend(kind: OperationKind, step: str = "", execution_id: str = "e1") -> Operation:
    return Operation.intended(kind, execution_id, 1, fence=FENCE, now=NOW, step=step)


def container(name: str = "ilmar-inst-e1-1") -> Resource:
    return Resource.intended(ResourceKind.CONTAINER, "e1", 1, name, owner_token="t", now=NOW)


def failures(report: Report) -> list[tuple[str, str]]:
    return [(f.check, f.subject) for f in report.failures]


# -- fully covered -----------------------------------------------------------------------


def lifecycle(replay: Replay) -> None:
    replay.commit(created())
    claim = intend(OperationKind.CLAIM_TASK)
    replay.commit(Commit(operations=(claim,)))
    replay.commit(Commit(operations=(claim.moved(ST.SUCCEEDED, fence=FENCE, now=NOW),),
                         applied=move(replay, S.CLAIMED).applied))  # fmt: skip
    box = container()
    replay.commit(Commit(resources=(box,)))
    active = box.moved(R.ACTIVE, now=NOW, identity="c0ffee")
    replay.commit(Commit(resources=(active,)))
    replay.commit(Commit(resources=(active.moved(R.RELEASING, now=NOW),)))
    replay.commit(Commit(resources=(replay.resources[box.key].moved(R.RELEASED, now=NOW),)),
                  evidence_for="e1")  # fmt: skip


def test_a_store_covered_from_the_start_folds_exactly_to_its_rows() -> None:
    replay = Replay()
    replay.migrate()  # an empty store: the marker only
    lifecycle(replay)
    report = replay.verify()
    assert report.findings == ()
    assert report.exit_code == 0
    assert replay.events[0].type is EventType.COVERAGE_STARTED
    assert replay.events[0].data == {"from_schema_version": 3, "executions": 0,
                                     "operations": 0, "resources": 0}  # fmt: skip
    assert [e.seq for e in replay.events] == list(range(1, len(replay.events) + 1))


def test_a_store_without_events_has_no_coverage() -> None:
    replay = Replay()
    replay.commit(created(), covered=False)
    assert replay.verify().covered is False
    assert replay.verify().exit_code == 2


# -- the migration baseline (pre-existing data) -------------------------------------------


def pre_migration_store() -> Replay:
    """What today's schema can hold when the events table is added: a
    non-terminal execution mid-agent, an intended and an ambiguous
    operation left by a crash, active, releasing and quarantined resources,
    and a succeeded and a failed execution."""
    replay = Replay()
    replay.commit(created(), covered=False)
    for state in (S.CLAIMED, S.PROVISIONING, S.RUNNING_AGENT):
        replay.commit(move(replay, state), covered=False)
    replay.commit(Commit(), covered=False, evidence_for="e1")
    run = intend(OperationKind.RUN_AGENT)
    start = intend(OperationKind.START_CONTAINER, "ilmar-inst-e1-1")
    replay.commit(Commit(operations=(run, start.moved(ST.AMBIGUOUS, fence=FENCE, now=NOW))),
                  covered=False)  # fmt: skip
    active = container("ilmar-inst-e1-1").moved(R.ACTIVE, now=NOW, identity="a1")
    releasing = container("ilmar-inst-e1-0").moved(R.ACTIVE, now=NOW, identity="a0").moved(
        R.RELEASING, now=NOW)  # fmt: skip
    quarantined = container("ilmar-inst-e1-q").moved(R.QUARANTINED, now=NOW)
    replay.commit(Commit(resources=(active, releasing, quarantined)), covered=False)
    for done, final in (("e2", S.SUCCEEDED), ("e3", S.FAILED)):
        replay.commit(created(done), covered=False)
        replay.commit(move(replay, final, done), covered=False)
    return replay


def test_the_baseline_records_one_snapshot_per_existing_row() -> None:
    replay = pre_migration_store()
    replay.migrate()
    marker, *snapshots = replay.events
    assert marker.type is EventType.COVERAGE_STARTED
    assert marker.data == {"from_schema_version": 3, "executions": 3, "operations": 2,
                           "resources": 3}  # fmt: skip
    kinds = [e.type for e in snapshots]
    assert kinds.count(EventType.EXECUTION_BASELINE) == 3
    assert kinds.count(EventType.OPERATION_BASELINE) == 2
    assert kinds.count(EventType.RESOURCE_BASELINE) == 3
    assert all(e.actor.kind.value == "migration" for e in replay.events)
    e1 = next(e for e in snapshots if e.type is EventType.EXECUTION_BASELINE
              and e.execution == "e1")  # fmt: skip
    assert e1.data == {"state": "running_agent", "version": 4, "transitions_before": 4,
                       "evidence_before": 1}  # fmt: skip
    assert {e.data["status"] for e in snapshots if e.type is EventType.OPERATION_BASELINE} == {
        "intended", "ambiguous"}  # fmt: skip
    assert {e.data["status"] for e in snapshots if e.type is EventType.RESOURCE_BASELINE} == {
        "active", "releasing", "quarantined"}  # fmt: skip


def test_pre_existing_data_is_historical_never_a_failure() -> None:
    replay = pre_migration_store()
    replay.migrate()
    report = replay.verify()
    assert report.failures == ()
    assert report.exit_code == 0
    historical = {f.subject for f in report.historical}
    assert "execution:e1" in historical  # 4 transitions and 1 evidence before coverage
    assert len([s for s in historical if s.startswith("operation:")]) == 2
    assert len([s for s in historical if s.startswith("resource:")]) == 3
    detail = next(f.detail for f in report.historical if f.subject == "execution:e1")
    assert detail == "4 transitions and 1 evidence records before coverage"


def test_recovery_after_the_migration_continues_from_the_baseline() -> None:
    """The baselined ambiguous start and intended agent run are settled the way
    recovery settles them (ADR-0022), the sandboxes removed, a new attempt
    queued; I11 holds after every commit."""
    replay = pre_migration_store()
    replay.migrate()
    start = next(o for o in replay.operations.values() if o.status is ST.AMBIGUOUS)
    run = next(o for o in replay.operations.values() if o.status is ST.INTENDED)
    ctx = EventContext(at=NOW, instance="inst", epoch=5, pass_id="p-0a0b0d",
                       reconciled=frozenset({start.key, run.key}),
                       reasons={run.key: "interrupted", "e1": "interrupted"})  # fmt: skip
    steps = [
        Commit(operations=(start.moved(ST.SUCCEEDED, fence=FENCE, now=NOW),)),
        Commit(operations=(run.moved(ST.FAILED, fence=FENCE, now=NOW, note="abandoned"),)),
        Commit(
            resources=(
                replay.resources[container("ilmar-inst-e1-1").key].moved(R.RELEASING, now=NOW),
            )
        ),
    ]
    for step in steps:
        replay.commit(step, ctx)
        assert replay.verify().failures == ()
    replay.commit(Commit(resources=(replay.resources[container("ilmar-inst-e1-1").key].moved(
        R.RELEASED, now=NOW),)), ctx)  # fmt: skip
    replay.commit(Commit(resources=(replay.resources[container("ilmar-inst-e1-0").key].moved(
        R.RELEASED, now=NOW),)), ctx)  # fmt: skip
    replay.commit(move(replay, S.RETRY_WAIT), ctx)
    report = replay.verify()
    assert report.failures == ()
    assert report.exit_code == 0
    reconciled = [e for e in replay.events if e.type is EventType.OPERATION_RECONCILED]
    assert {e.data["from"] for e in reconciled} == {"ambiguous", "intended"}
    retry = replay.events[-1]
    assert (retry.type, retry.reason) == (EventType.EXECUTION_TRANSITIONED, "interrupted")
    # The quarantined row is untouched (ADR-0020) and stays historical.
    quarantine = resource_ref(container("ilmar-inst-e1-q").key)
    assert f"resource:{quarantine}" in {f.subject for f in report.historical}


# -- integrity failures, each found and never mistaken for history ------------------------


def covered_store() -> Replay:
    replay = pre_migration_store()
    replay.migrate()
    replay.commit(created("e4"))
    replay.commit(move(replay, S.CLAIMED, "e4"))
    return replay


def test_a_row_changed_without_an_event_is_a_fold_mismatch() -> None:
    replay = covered_store()
    op = next(iter(replay.operations.values()))
    replay.operations[op.key] = op.moved(ST.FAILED, fence=FENCE, now=NOW)  # edited outside Ilmar
    assert ("fold-mismatch", f"operation:{op.key}") in failures(replay.verify())
    replay = covered_store()
    replay.executions["e4"] = replace(replay.executions["e4"], state=S.FAILED, version=3)
    assert ("fold-mismatch", "execution:e4") in failures(replay.verify())


def test_an_extra_or_missing_transition_or_evidence_row_is_a_count_mismatch() -> None:
    for table, delta in (("transitions", 1), ("transitions", -1), ("evidence", 1)):
        replay = covered_store()
        counts = getattr(replay, table)
        counts["e1"] = counts.get("e1", 0) + delta
        report = replay.verify()
        assert ("count-mismatch", "execution:e1") in failures(report), table
        assert report.exit_code == 1


def test_an_entity_with_neither_creation_nor_baseline_is_a_failure() -> None:
    replay = covered_store()
    replay.commit(created("e9"), covered=False)  # inserted outside Ilmar
    assert ("no-coverage-event", "execution:e9") in failures(replay.verify())
    replay = covered_store()
    stray = container("ilmar-inst-e1-x")  # inserted outside Ilmar
    replay.resources[stray.key] = stray
    assert any(c == "no-coverage-event" and s.startswith("resource:")
               for c, s in failures(replay.verify()))  # fmt: skip


def test_a_deleted_event_shows_as_a_seq_gap() -> None:
    replay = covered_store()
    del replay.events[len(replay.events) // 2]
    assert ("seq-gap", "store") in failures(replay.verify())


def test_a_second_marker_or_a_late_baseline_is_a_failure() -> None:
    replay = covered_store()
    replay.migrate()  # a second migration on top of covered history
    found = failures(replay.verify())
    assert ("duplicate-marker", "store") in found
    assert ("baseline-after-covered", "execution:e4") in found


def test_an_event_for_something_not_stored_is_an_orphan() -> None:
    replay = covered_store()
    del replay.executions["e4"]
    assert ("orphan-event", "execution:e4") in failures(replay.verify())


def test_a_change_before_creation_is_a_failure() -> None:
    replay = covered_store()
    stray = intend(OperationKind.POST_UPDATE, "note", "e4").moved(ST.SUCCEEDED, fence=FENCE,
                                                                 now=NOW)  # fmt: skip
    replay.commit(Commit(operations=(stray,)))  # its intent was never recorded
    assert ("event-before-creation", f"operation:{stray.key}") in failures(replay.verify())


def test_failures_never_hide_history_and_history_never_counts_as_failure() -> None:
    replay = covered_store()
    replay.transitions["e4"] += 1
    report = replay.verify()
    assert report.exit_code == 1
    assert all(f.finding_class is FindingClass.INTEGRITY for f in report.failures)
    assert all(f.finding_class is FindingClass.HISTORICAL for f in report.historical)
    assert {f.subject for f in report.historical} >= {"execution:e1"}
    assert {f.check for f in report.historical} == {"before-coverage"}


@pytest.mark.parametrize("execution_id", ["e2", "e3"])
def test_terminal_executions_are_baselined_too(execution_id: str) -> None:
    """A terminal execution can still change later (cleanup, a release, #59);
    its later changes fold from its baseline."""
    replay = pre_migration_store()
    replay.migrate()
    name = f"ilmar-inst-{execution_id}-1"
    box = Resource.intended(ResourceKind.CONTAINER, execution_id, 1, name, owner_token="t",
                            now=NOW)  # fmt: skip
    replay.commit(Commit(resources=(box,)))
    replay.commit(Commit(resources=(box.moved(R.RELEASED, now=NOW),)))
    report = replay.verify()
    assert report.failures == ()
    assert f"execution:{execution_id}" in {
        e.execution and f"execution:{e.execution}"
        for e in replay.events
        if e.type is EventType.EXECUTION_BASELINE
    }


def test_a_change_whose_from_disagrees_with_the_history_is_a_fold_mismatch() -> None:
    """Versions line up, but the event claims a previous status the history never had."""
    replay = covered_store()
    op = intend(OperationKind.POST_UPDATE, "note", "e4")
    replay.commit(Commit(operations=(op,)))
    done = op.moved(ST.SUCCEEDED, fence=FENCE, now=NOW)
    context = EventContext(at=NOW, instance="inst", epoch=FENCE.epoch)
    (event,) = derive_events(Commit(operations=(done,)), context)
    replay.operations[done.key] = done
    replay.append(event.with_previous("ambiguous"))  # it was intended
    assert failures(replay.verify()) == [("fold-mismatch", f"operation:{done.key}")]


# -- the baseline block is exact (review of PR #64) ------------------------------------------


def renumbered(events: list[Event]) -> list[Event]:
    return [dataclasses.replace(e, seq=n) for n, e in enumerate(events, 1)]


def migrated() -> Replay:
    replay = pre_migration_store()
    replay.migrate()
    return replay


@pytest.mark.parametrize(
    ("kind", "subject_prefix"),
    [
        (EventType.EXECUTION_BASELINE, "execution:"),
        (EventType.OPERATION_BASELINE, "operation:"),
        (EventType.RESOURCE_BASELINE, "resource:"),
    ],
)
def test_a_missing_baseline_is_detected(kind: EventType, subject_prefix: str) -> None:
    """The migration lost one snapshot: the marker's count catches it, and so does
    the row that now has no coverage event."""
    replay = migrated()
    victim = next(e for e in replay.events if e.type is kind)
    replay.events = renumbered([e for e in replay.events if e is not victim])
    found = failures(replay.verify())
    assert ("baseline-count-mismatch", "store") in found
    assert any(c == "no-coverage-event" and s.startswith(subject_prefix) for c, s in found)
    assert replay.verify().exit_code == 1


def test_an_extra_baseline_is_detected() -> None:
    replay = migrated()
    extra = next(e for e in replay.events if e.type is EventType.RESOURCE_BASELINE)
    position = replay.events.index(extra)
    replay.events = renumbered([*replay.events[: position + 1], extra,
                                *replay.events[position + 1 :]])  # fmt: skip
    found = failures(replay.verify())
    assert ("baseline-count-mismatch", "store") in found


def test_a_marker_whose_counts_were_changed_is_detected() -> None:
    replay = migrated()
    marker = replay.events[0]
    forged = dataclasses.replace(marker, data={**marker.data, "executions": 2})
    replay.events = [forged, *replay.events[1:]]
    assert ("baseline-count-mismatch", "store") in failures(replay.verify())


def test_baselines_out_of_order_are_detected() -> None:
    """Counts are right, but an operation snapshot comes before an execution's."""
    replay = migrated()
    first_execution = next(i for i, e in enumerate(replay.events)
                           if e.type is EventType.EXECUTION_BASELINE)  # fmt: skip
    first_operation = next(i for i, e in enumerate(replay.events)
                           if e.type is EventType.OPERATION_BASELINE)  # fmt: skip
    events = list(replay.events)
    events[first_execution], events[first_operation] = (events[first_operation],
                                                        events[first_execution])  # fmt: skip
    replay.events = renumbered(events)
    found = failures(replay.verify())
    assert ("baseline-out-of-order", "store") in found
    assert ("baseline-count-mismatch", "store") not in found  # the counts are right


def test_a_covered_event_inside_the_baseline_block_is_detected() -> None:
    replay = migrated()
    replay.commit(created("e4"))
    covered = replay.events[-1]
    events = [e for e in replay.events if e is not covered]
    events.insert(2, covered)  # between two snapshots
    replay.events = renumbered(events)
    assert ("baseline-out-of-order", "store") in failures(replay.verify())


def test_a_baseline_after_the_block_is_detected() -> None:
    replay = migrated()
    replay.commit(created("e4"))
    late = next(e for e in replay.events if e.type is EventType.EXECUTION_BASELINE)
    replay.events = renumbered([e for e in replay.events if e is not late] + [late])
    found = failures(replay.verify())
    assert ("baseline-outside-block", "store") in found


def test_an_exact_baseline_block_passes() -> None:
    replay = migrated()
    replay.commit(created("e4"))
    assert replay.verify().failures == ()


# -- persisted lines are read strictly -------------------------------------------------------


def lines(replay: Replay) -> list[str]:
    return [e.to_json() for e in replay.events]


def test_every_event_the_store_would_hold_reads_back_strictly() -> None:
    replay = migrated()
    replay.commit(created("e4"))
    replay.commit(move(replay, S.CLAIMED, "e4"))
    events, problems = load_events(lines(replay))
    assert problems == []
    assert [e.to_json() for e in events] == lines(replay)
    assert verify_lines(lines(replay), replay.stored()).exit_code == 0


@pytest.mark.parametrize(
    "corrupt",
    [
        lambda text: text.replace('"status":"intended"', '"status":"maybe"', 1),
        lambda text: text.replace('"version":', '"version_":', 1),
        lambda text: text.replace('{"actor":', '{"note":"x","actor":', 1),
        lambda text: text.replace(",", ", ", 1),  # not canonical
        lambda text: text[:-5],  # truncated on disk
        lambda text: "",
    ],
    ids=["enum", "renamed key", "extra field", "whitespace", "cut off", "empty"],
)
def test_a_corrupt_stored_line_is_an_integrity_failure_never_repaired(corrupt: Any) -> None:
    replay = migrated()
    stored_lines = lines(replay)
    target = next(i for i, text in enumerate(stored_lines) if '"status":"intended"' in text)
    stored_lines[target] = corrupt(stored_lines[target])
    report = verify_lines(stored_lines, replay.stored())
    assert report.exit_code == 1
    checks = [f.check for f in report.failures]
    assert "corrupt-event" in checks
    assert "seq-gap" in checks  # the corrupt line is left out, not mended
    detail = next(f.detail for f in report.failures if f.check == "corrupt-event")
    assert detail.startswith(f"line {target + 1}:")


def test_a_store_whose_every_line_is_corrupt_still_reports_its_rows() -> None:
    replay = migrated()
    report = verify_lines(["{}"] * len(replay.events), replay.stored())
    assert report.covered is True
    checks = {f.check for f in report.failures}
    assert {"corrupt-event", "no-coverage-event"} <= checks


def test_no_stored_lines_means_no_coverage() -> None:
    replay = pre_migration_store()
    assert verify_lines([], replay.stored()).exit_code == 2


# -- type confusion: every malformed line is an integrity failure, never a crash -----------

# One of every JSON type, plus strings that look like the enum values a field may hold.
WRONG_TYPES: tuple[Any, ...] = (
    None, True, False, 0, -1, 1.5, 10**40, "", "x", "<redacted>", "coordinator",
    "operation.succeeded", [], [1], ["human"], {}, {"kind": "human"}, {"value": "x"},
)  # fmt: skip


def one_line_per_type() -> list[str]:
    replay = migrated()
    lifecycle(replay)
    by_type: dict[EventType, str] = {}
    for stored_event in replay.events:
        by_type.setdefault(stored_event.type, stored_event.to_json())
    return list(by_type.values())


def substitutions(text: str) -> list[tuple[str, str]]:
    """``text`` with each top-level, actor and data field replaced by each wrong type,
    canonically encoded so only the content can be what is refused."""
    record: dict[str, Any] = json.loads(text)
    out: list[tuple[str, str]] = []

    def emit(path: str, changed: dict[str, Any]) -> None:
        out.append((path, json.dumps(changed, sort_keys=True, separators=(",", ":"))))

    for key in sorted(set(record) | {"type", "actor", "data", "seq", "reason"}):
        for value in WRONG_TYPES:
            emit(key, record | {key: value})
    for key in ("kind", "ref"):
        for value in WRONG_TYPES:
            emit(f"actor.{key}", record | {"actor": record["actor"] | {key: value}})
            emit(
                f"actor.human.{key}",
                record | {"actor": {"kind": "human", "ref": "ops"} | {key: value}},
            )
    for key in sorted(record.get("data", {})):
        for value in WRONG_TYPES:
            emit(f"data.{key}", record | {"data": record["data"] | {key: value}})
    return out


SWEEP = [
    (text, path, changed) for text in one_line_per_type() for path, changed in substitutions(text)
]


def test_the_sweep_covers_every_enum_valued_field() -> None:
    paths = {path for _, path, _ in SWEEP}
    assert {"type", "actor.kind", "actor.human.ref", "reason", "data.kind", "data.status",
            "data.state", "data.from", "data.to", "data.verdict"} <= paths  # fmt: skip
    assert len({json.loads(text)["type"] for text, _, _ in SWEEP}) >= 11


def test_every_wrong_json_type_is_corrupt_or_a_valid_event_never_a_crash() -> None:
    """Any exception other than ``CorruptEventError`` escapes and fails the test."""
    refused = 0
    for _, path, changed in SWEEP:
        try:
            decoded = Event.from_json(changed)
        except CorruptEventError:
            refused += 1
            continue
        # Accepted only if the substitution happened to be a valid value; then it is
        # exactly what is stored.
        assert decoded.to_json() == changed, path
    assert refused > 0.95 * len(SWEEP)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("type", 7), ("type", None), ("type", ["operation.succeeded"]), ("type", {}),
        ("actor", "coordinator"), ("actor", ["coordinator"]),
        ("actor.kind", 1), ("actor.kind", None), ("actor.kind", {"kind": "human"}),
        ("actor.human.ref", 1), ("actor.human.ref", True), ("actor.human.ref", ["ops"]),
        ("actor.human.ref", {}), ("actor.ref", 0), ("reason", 3),
        ("data.kind", 1), ("data.status", ["intended"]), ("data.version", "2"),
    ],
)  # fmt: skip
def test_verify_lines_reports_a_wrong_json_type_as_an_integrity_failure(
    field: str, value: Any
) -> None:
    replay = migrated()
    stored_lines = lines(replay)
    target = next(i for i, text in enumerate(stored_lines) if '"status":"intended"' in text)
    record: dict[str, Any] = json.loads(stored_lines[target])
    match field.split("."):
        case [key]:
            record[key] = value
        case ["actor", "human", "ref"]:
            record["actor"] = {"kind": "human", "ref": value}
        case [outer, key]:
            record[outer] = record[outer] | {key: value}
    stored_lines[target] = json.dumps(record, sort_keys=True, separators=(",", ":"))
    report = verify_lines(stored_lines, replay.stored())
    assert report.exit_code == 1
    detail = next(f.detail for f in report.failures if f.check == "corrupt-event")
    assert detail.startswith(f"line {target + 1}:")


@pytest.mark.parametrize(
    "text",
    [
        "[" * 100_000 + "]" * 100_000,
        '{"a":' * 100_000 + "1" + "}" * 100_000,
        '{"seq":NaN}',
        '{"seq":Infinity}',
        "\ud800",
        '"just a string"',
    ],
    ids=["deep array", "deep object", "NaN", "Infinity", "lone surrogate", "string"],
)
def test_verify_lines_survives_unparseable_lines(text: str) -> None:
    replay = migrated()
    stored_lines = lines(replay)
    stored_lines[1] = text
    report = verify_lines(stored_lines, replay.stored())
    assert report.exit_code == 1
    assert any(
        f.check == "corrupt-event" and f.detail.startswith("line 2:") for f in report.failures
    )
