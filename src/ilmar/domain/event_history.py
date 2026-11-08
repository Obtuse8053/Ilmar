# SPDX-License-Identifier: Apache-2.0
"""Invariant I11 (ADR-0024 §2 and §8): the event history rebuilds the stored state.

:func:`verify` folds every entity from its first event (its creation event,
or its migration baseline) and compares the result with the stored rows. A
finding is either *historical* (data outside event coverage by design,
bounded by the baseline's explicit entities and counts) or an *integrity
failure*. The two are never mixed, and timestamps are never used to decide
which is which (ADR-0022).
"""

from __future__ import annotations

import enum
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field

from ilmar.domain.events import Event, EventType, resource_ref
from ilmar.domain.execution import Execution
from ilmar.domain.ledger import Operation, Resource


class FindingClass(enum.Enum):
    HISTORICAL = "historical"
    INTEGRITY = "integrity"


@dataclass(frozen=True, slots=True)
class Finding:
    finding_class: FindingClass
    check: str
    """A stable name for what was checked, e.g. ``fold-mismatch``."""
    subject: str
    """The entity: ``execution:<id>``, ``operation:<key>``, ``resource:<ref>`` or ``store``."""
    detail: str


@dataclass(frozen=True, slots=True)
class StoredState:
    """The rows I11 is checked against, read in one consistent snapshot."""

    executions: Sequence[Execution]
    operations: Sequence[Operation]
    resources: Sequence[Resource]
    transitions: Mapping[str, int]
    """Execution id -> number of ``transitions`` rows."""
    evidence: Mapping[str, int]
    """Execution id -> number of ``evidence`` rows."""


@dataclass(frozen=True, slots=True)
class Report:
    covered: bool
    """``False`` when the store has no events at all (exit 2 of ``ilmar events verify``)."""
    findings: tuple[Finding, ...] = ()

    @property
    def failures(self) -> tuple[Finding, ...]:
        return tuple(f for f in self.findings if f.finding_class is FindingClass.INTEGRITY)

    @property
    def historical(self) -> tuple[Finding, ...]:
        return tuple(f for f in self.findings if f.finding_class is FindingClass.HISTORICAL)

    @property
    def exit_code(self) -> int:
        if not self.covered:
            return 2
        return 1 if self.failures else 0


@dataclass
class _Execution:
    state: str
    version: int
    attempt: int | None
    baselined: bool
    transitions_before: int = 0
    evidence_before: int = 0
    transitions_seen: int = 0
    evidence_seen: int = 0


@dataclass
class _Row:
    status: str
    version: int
    baselined: bool


@dataclass
class _Fold:
    executions: dict[str, _Execution] = field(default_factory=dict)
    operations: dict[str, _Row] = field(default_factory=dict)
    resources: dict[str, _Row] = field(default_factory=dict)
    findings: list[Finding] = field(default_factory=list)

    def fail(self, check: str, subject: str, detail: str) -> None:
        self.findings.append(Finding(FindingClass.INTEGRITY, check, subject, detail))


_CREATES = {
    EventType.EXECUTION_CREATED,
    EventType.OPERATION_INTENDED,
    EventType.RESOURCE_RECORDED,
}
_BASELINES = {
    EventType.EXECUTION_BASELINE,
    EventType.OPERATION_BASELINE,
    EventType.RESOURCE_BASELINE,
}
_OPERATION_CHANGES = {
    EventType.OPERATION_INTENDED,
    EventType.OPERATION_SUCCEEDED,
    EventType.OPERATION_FAILED,
    EventType.OPERATION_AMBIGUOUS,
    EventType.OPERATION_RECONCILED,
}
_RESOURCE_CHANGES = {
    EventType.RESOURCE_RECORDED,
    EventType.RESOURCE_CHANGED,
    EventType.RESOURCE_QUARANTINED,
}


def _fold(events: Iterable[Event]) -> tuple[_Fold, bool]:
    fold = _Fold()
    expected_seq = 1
    marker_seen = False
    for event in events:
        if event.seq != expected_seq:
            fold.fail("seq-gap", "store", f"expected seq {expected_seq}, found {event.seq}")
            expected_seq = (event.seq or expected_seq) + 1
        else:
            expected_seq += 1
        if event.type is EventType.COVERAGE_STARTED:
            if marker_seen or event.seq != 1:
                fold.fail("duplicate-marker", "store", f"coverage marker at seq {event.seq}")
            marker_seen = True
            continue
        if event.type in _BASELINES and not marker_seen:
            fold.fail("baseline-without-marker", "store", f"baseline at seq {event.seq}")
        _apply(fold, event)
    return fold, marker_seen


def _apply(fold: _Fold, event: Event) -> None:
    data = event.data
    if event.type in (EventType.EXECUTION_CREATED, EventType.EXECUTION_BASELINE):
        subject = f"execution:{event.execution}"
        if event.execution in fold.executions:
            fold.fail("baseline-after-covered" if event.type in _BASELINES else "created-twice",
                      subject, f"seq {event.seq}")  # fmt: skip
            return
        baselined = event.type is EventType.EXECUTION_BASELINE
        fold.executions[str(event.execution)] = _Execution(
            state=str(data["state"]), version=int(data["version"]), attempt=event.attempt,
            baselined=baselined,
            transitions_before=int(data.get("transitions_before", 0)),
            evidence_before=int(data.get("evidence_before", 0)),
            transitions_seen=0 if baselined else 1,  # the creation row
        )  # fmt: skip
        return
    if event.type is EventType.EXECUTION_TRANSITIONED:
        state = fold.executions.get(str(event.execution))
        subject = f"execution:{event.execution}"
        if state is None:
            fold.fail("event-before-creation", subject, f"seq {event.seq}")
            return
        if data["from"] != state.state or data["version"] != state.version + 1:
            fold.fail("fold-mismatch", subject,
                      f"seq {event.seq}: {data['from']}@v{data['version']} after "
                      f"{state.state}@v{state.version}")  # fmt: skip
        state.state, state.version = str(data["to"]), int(data["version"])
        state.attempt = event.attempt
        state.transitions_seen += 1
        return
    if event.type is EventType.EVIDENCE_RECORDED:
        state = fold.executions.get(str(event.execution))
        if state is None:
            fold.fail("event-before-creation", f"execution:{event.execution}", f"seq {event.seq}")
            return
        state.evidence_seen += 1
        return
    if event.type in _OPERATION_CHANGES or event.type is EventType.OPERATION_BASELINE:
        _apply_row(fold, fold.operations, f"operation:{event.operation}", str(event.operation),
                   event)  # fmt: skip
        return
    if event.type in _RESOURCE_CHANGES or event.type is EventType.RESOURCE_BASELINE:
        ref = str(data["ref"])
        _apply_row(fold, fold.resources, f"resource:{ref}", ref, event)
        return
    # ownership and other execution-independent events change no row.


def _apply_row(fold: _Fold, rows: dict[str, _Row], subject: str, key: str, event: Event) -> None:
    data = event.data
    status, version = str(data["status"]), int(data["version"])
    known = rows.get(key)
    if event.type in _BASELINES:
        if known is not None:
            fold.fail("baseline-after-covered", subject, f"seq {event.seq}")
            return
        rows[key] = _Row(status, version, baselined=True)
        return
    if known is None:
        if event.type in _CREATES and version == 1:
            rows[key] = _Row(status, version, baselined=False)
        else:
            fold.fail("event-before-creation", subject, f"seq {event.seq}: v{version}")
        return
    if version != known.version + 1 or ("from" in data and data["from"] != known.status):
        fold.fail("fold-mismatch", subject,
                  f"seq {event.seq}: {data.get('from')}->{status}@v{version} after "
                  f"{known.status}@v{known.version}")  # fmt: skip
    known.status, known.version = status, version


def verify(events: Iterable[Event], stored: StoredState) -> Report:
    """Check I11 for every stored entity and every event; see the module docstring."""
    materialised = list(events)
    if not materialised:
        return Report(covered=False)
    fold, marker_seen = _fold(materialised)
    if not marker_seen:
        fold.fail("no-coverage-marker", "store", "events exist without events.coverage_started")
    findings = fold.findings

    stored_executions = {e.id: e for e in stored.executions}
    for execution in stored.executions:
        subject = f"execution:{execution.id}"
        folded = fold.executions.get(execution.id)
        if folded is None:
            fold.fail("no-coverage-event", subject, "neither created nor baselined")
            continue
        if (folded.state, folded.version) != (execution.state.value, execution.version):
            fold.fail("fold-mismatch", subject,
                      f"events give {folded.state}@v{folded.version}, stored "
                      f"{execution.state.value}@v{execution.version}")  # fmt: skip
        if folded.attempt is not None and folded.attempt != execution.attempt:
            fold.fail(
                "fold-mismatch",
                subject,
                f"events give attempt {folded.attempt}, stored {execution.attempt}",
            )
        expected_transitions = folded.transitions_before + folded.transitions_seen
        if stored.transitions.get(execution.id, 0) != expected_transitions:
            fold.fail("count-mismatch", subject,
                      f"{stored.transitions.get(execution.id, 0)} transitions stored, "
                      f"{expected_transitions} accounted for")  # fmt: skip
        expected_evidence = folded.evidence_before + folded.evidence_seen
        if stored.evidence.get(execution.id, 0) != expected_evidence:
            fold.fail("count-mismatch", subject,
                      f"{stored.evidence.get(execution.id, 0)} evidence records stored, "
                      f"{expected_evidence} accounted for")  # fmt: skip
        if folded.baselined and (folded.transitions_before or folded.evidence_before):
            findings.append(Finding(
                FindingClass.HISTORICAL, "before-coverage", subject,
                f"{folded.transitions_before} transitions and {folded.evidence_before} "
                "evidence records before coverage",
            ))  # fmt: skip
    for execution_id in fold.executions.keys() - stored_executions.keys():
        fold.fail("orphan-event", f"execution:{execution_id}", "no such execution is stored")

    for rows, items, name, key_of in (
        (fold.operations, stored.operations, "operation", lambda o: o.key),
        (fold.resources, stored.resources, "resource", lambda r: resource_ref(r.key)),
    ):
        seen = set()
        for item in items:
            key = key_of(item)
            seen.add(key)
            subject = f"{name}:{key}"
            folded_row = rows.get(key)
            if folded_row is None:
                fold.fail("no-coverage-event", subject, "neither created nor baselined")
                continue
            if (folded_row.status, folded_row.version) != (item.status.value, item.version):
                fold.fail("fold-mismatch", subject,
                          f"events give {folded_row.status}@v{folded_row.version}, stored "
                          f"{item.status.value}@v{item.version}")  # fmt: skip
            if folded_row.baselined:
                findings.append(
                    Finding(
                        FindingClass.HISTORICAL,
                        "before-coverage",
                        subject,
                        "its creation and earlier changes predate coverage",
                    )
                )
        for key in rows.keys() - seen:
            fold.fail("orphan-event", f"{name}:{key}", f"no such {name} is stored")
    return Report(covered=True, findings=tuple(findings))
