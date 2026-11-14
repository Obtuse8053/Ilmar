# SPDX-License-Identifier: Apache-2.0
"""Structured events (ADR-0024): the schema, its validation and their derivation.

Durable events are part of the store's state: they are committed in the same
fenced transaction as the change they record. This module is pure; it builds
and checks events, and never stores them.

* :class:`Event` is one record in the ``ilmar.event/1`` envelope. It is
  validated when built, in this order (ADR-0024 §7):

  1. the envelope's identifiers against their patterns (an invalid one is a
     broken invariant: :class:`EventEnvelopeError`);
  2. ``data`` against the event type's allowlist: typed values only, no
     free-form strings (an invalid entry is dropped and the event flagged
     ``invalid``);
  3. a credential-shape scan of every string as defence in depth (a match is
     replaced and the event flagged ``redacted``).

* :func:`derive_events` builds the events a :class:`~ilmar.domain.ledger.Commit`
  must carry, one per changed row, so a caller cannot omit one (ADR-0024 §2).
* :func:`baseline_events` builds the migration baseline (ADR-0024 §8).
"""

from __future__ import annotations

import base64
import enum
import hashlib
import json
import re
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from types import MappingProxyType
from typing import Any, NoReturn

from ilmar.domain.budget import SuspensionReason
from ilmar.domain.errors import (
    CandidateRejectedError,
    ContainmentError,
    FencingError,
    LeaseConflictError,
    LedgerIntegrityError,
    NonFastForwardError,
    OwnershipConflictError,
    PolicyViolationError,
)
from ilmar.domain.execution import Execution, ExecutionState
from ilmar.domain.gates import (
    FIXTURE_PATH,
    GATE1_URL,
    GATED_MODES,
    PULL_URL,
    RUN_URL,
    SHA256,
    GateRecorded,
)
from ilmar.domain.ledger import (
    Commit,
    Operation,
    OperationKind,
    OperationStatus,
    Resource,
    ResourceKind,
    ResourceStatus,
)
from ilmar.domain.tasks import ClaimOutcome
from ilmar.domain.verification import Verdict

SCHEMA = "ilmar.event/1"
MAX_MESSAGE = 300
MAX_DATA_BYTES = 4096
REDACTED = "<redacted>"


class EventEnvelopeError(ValueError):
    """An envelope identifier is invalid: the event cannot be attributed (ADR-0024 §7)."""


class CorruptEventError(EventEnvelopeError):
    """A persisted event does not meet the schema it claims.

    Raised by :meth:`Event.from_json`. A stored event is reported, never
    repaired: reading it back drops nothing, redacts nothing and fills in
    nothing, so a corrupted record cannot pass as a clean one.
    """


# -- reason codes (ADR-0024 §4) --------------------------------------------------------

REASON_CODES: Mapping[str, str] = MappingProxyType(
    {
        "resource-ownership-conflict": "A resource's owner labels, ledger identity or "
        "directory did not match (ADR-0012 rule 3).",
        "containment-unconfirmed": "Removal of a sandbox part could not be confirmed.",
        "policy-violation": "A request exceeded what the deployment permits.",
        "candidate-rejected": "A candidate could not be frozen, or no longer matches its freeze.",
        "ledger-integrity": "The journal, the ledger or a candidate is inconsistent.",
        "fenced-out": "This process lost ownership of the store or the instance.",
        "branch-moved": "A remote head is neither the expected head nor the candidate.",
        "push-rejected": "Publishing was refused: not a fast-forward, or the lease failed.",
        "evidence-policy-mismatch": "Evidence was recorded under another verification policy.",
        "interrupted": "Recovery abandoned an interrupted step.",
        "agent-failed": "The agent reported a failure.",
        "agent-timed-out": "The agent did not finish in time.",
        "agent-stalled": "The agent showed no progress for the stall timeout.",
        "suspension-interrupted": "A suspension stopped the agent run; the attempt is "
        "retried, with a fresh reservation, only after a resume (ADR-0029 §3).",
        "suspension-check-failed": "The suspension check failed while an agent ran, so all "
        "new work is suspended until an operator resumes once the check succeeds again "
        "(ADR-0029 §4).",
        "model-spend-settled": "The gateway's record of an attempt's model requests "
        "was settled into the ledger; any cost beyond the reservation is an extra charge.",
        "spend-uncertain": "What an attempt's model requests cost is not known in full: "
        "all new work is suspended until an operator checks the provider's figures "
        "(ADR-0039).",
        "spend-reviewed": "An operator checked the model provider's own usage and billing "
        "for an attempt whose gateway spend was uncertain (ADR-0039).",
        "maintainer-released": "A maintainer released a blocked task: its claim is "
        "released and it may be picked again as a new execution (ADR-0031 §3).",
        "maintainer-abandoned": "A maintainer abandoned a blocked task: its claim is "
        "released and Ilmar never picks it again (ADR-0031 §3).",
        "protected-path": "The candidate touches a protected safety control; it is never "
        "pushed and its task stays claimed and blocked for a maintainer (ADR-0031).",
        "candidate-secret": "The candidate or its change request contains one of the "
        "deployment's own secrets; nothing is published (ADR-0038).",
        "provider-rate-limited": "The model provider rate-limited the agent; it is retried.",
        "provider-refused": "The model provider refused: spend limit, billing or authentication.",
        "verification-failed": "A verification run did not pass.",
        "attempts-exhausted": "No attempt is left.",
        "transient-provider-error": "A provider failed in a way that is retried.",
        "not-claimed-other": "Another factory instance holds the task.",
        "not-claimed-ineligible": "The task is not eligible (including a human claim).",
        "change-request-merged": "The change request was merged.",
        "change-request-closed": "The change request was closed without merging.",
        "unexpected-error": "An exception was caught at an execution's boundary.",
        # Added after the initial set (ADR-0024 §4: codes are added, never renamed).
        "no-changes": "The agent finished without changing anything; there is no candidate.",
        "ci-config-changed": "The candidate changes CI configuration, so a human must take over.",
        "checkout-failed": "The base branch could not be checked out; repeating it would fail "
        "the same way.",
        "cancel-requested": "A person asked to cancel the execution (ADR-0025).",
        # Tracker reconciliation (ADR-0026 §8).
        "task-closed": "The task was closed on the tracker.",
        "task-unlabelled": "The task lost its eligible label.",
        "task-excluded": "The task was given an excluded label.",
        "task-claimed-by-human": "A person claimed the task (an assignee or a human claim label).",
        "task-claimed-by-other-instance": "Another factory instance claimed the task.",
        "task-gone": "The task no longer exists in this repository.",
        "task-claim-lost": "This instance's claim on the task is gone, confirmed in two passes.",
        "orphaned-claim": "A claim of this instance that no execution record backs.",
        "stray-label": "Ilmar's claim label with no claim and no execution behind it.",
        "claim-changed": "A claim object changed: it is no longer this instance's authentic claim.",
        # Accounting reservations and suspension (ADR-0029 §2 and §3, #18).
        "budget-reserved": "An attempt's accounting reservation was charged as it started.",
        "budget-daily": "Starting another attempt would exceed the daily allowance.",
        "budget-monthly": "Starting another attempt would exceed the monthly allowance.",
        "provider-cap": "The model provider refused for its spend limit, billing or "
        "authentication.",
        "kill-switch": "The kill-switch file exists.",
        "operator-suspended": "An operator suspended new work.",
        "clock-anomaly": "The clock is behind the latest persisted time.",
        "operator-resumed": "An operator resumed new work after its cause was gone.",
        "gate-recorded": "A person recorded that a mode's gate passed, with its evidence "
        "(ADR-0040).",
        "gate-revoked": "A person revoked a mode's gate record; the mode is off at the "
        "next check (ADR-0040).",
        "restart-loop": "Loop mode started more often than its start limits allow (ADR-0033).",
        "unclassified": "No code was given. Never expected: tests assert it never occurs.",
    }
)

_ERROR_CODES: tuple[tuple[type[BaseException], str], ...] = (
    # Most specific first: ContainmentError is a ProviderError, and the
    # conflict classes share ConflictError.
    (ContainmentError, "containment-unconfirmed"),
    (OwnershipConflictError, "resource-ownership-conflict"),
    (FencingError, "fenced-out"),
    (LeaseConflictError, "push-rejected"),
    (NonFastForwardError, "push-rejected"),
    (PolicyViolationError, "policy-violation"),
    (CandidateRejectedError, "candidate-rejected"),
    (LedgerIntegrityError, "ledger-integrity"),
)

_CLAIM_CODES: Mapping[ClaimOutcome, str] = MappingProxyType(
    {
        ClaimOutcome.CLAIMED_BY_OTHER: "not-claimed-other",
        ClaimOutcome.NOT_ELIGIBLE: "not-claimed-ineligible",
    }
)


UNCLASSIFIED = "unclassified"
"""The fallback the event derivation writes when no code was given. It is never a
valid input: no caller may pass it as a code (ADR-0024 §2, layer 1)."""


def is_failure_code(code: object) -> bool:
    """Whether ``code`` is a registered reason code a caller may give."""
    return isinstance(code, str) and code in REASON_CODES and code != UNCLASSIFIED


def reason_for_error(error: type[BaseException] | BaseException) -> str:
    """The stable code of an error class, or ``unclassified``."""
    cls = error if isinstance(error, type) else type(error)
    for known, code in _ERROR_CODES:
        if issubclass(cls, known):
            return code
    return "unclassified"


def reason_for_claim(outcome: ClaimOutcome) -> str | None:
    """The code of a claim that did not succeed; ``None`` for ``CLAIMED``."""
    return _CLAIM_CODES.get(outcome)


# -- message templates (ADR-0024 §7) ---------------------------------------------------

_PARAM = re.compile(r"\{([a-z_]+)\}")

MESSAGES: Mapping[str, str] = MappingProxyType(
    {
        "failed-with-reason": "The execution failed: {reason}.",
        "blocked-for-human": "The execution needs a human: {reason}.",
        "quarantined": "A {kind} was quarantined: {reason}.",
        "coverage-started": "Event coverage starts here; earlier history is in the store's "
        "own tables.",
    }
)


@dataclass(frozen=True, slots=True)
class Message:
    """A registered template and its parameters. Never free text."""

    template: str
    params: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        text = MESSAGES.get(self.template)
        if text is None:
            raise ValueError(f"no message template {self.template!r}")
        if set(_PARAM.findall(text)) != set(self.params):
            raise ValueError(f"template {self.template!r} takes {sorted(_PARAM.findall(text))}")
        for name, value in self.params.items():
            if not _MESSAGE_PARAMS[name](value):
                raise ValueError(f"message parameter {name!r} is not allowed")
        object.__setattr__(self, "params", MappingProxyType(dict(sorted(self.params.items()))))

    def render(self) -> str:
        return MESSAGES[self.template].format(**self.params)[:MAX_MESSAGE]


# -- actors ----------------------------------------------------------------------------

_ACTOR_REF = re.compile(r"^[a-z][a-z0-9-]{0,31}$")


class ActorKind(enum.Enum):
    COORDINATOR = "coordinator"
    HUMAN = "human"
    MIGRATION = "migration"


@dataclass(frozen=True, slots=True)
class Actor:
    """Who acted. A human's ``ref`` is a short lower-case label, never a login,
    an address or a credential; an invalid one is refused, not redacted."""

    kind: ActorKind
    ref: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.kind, ActorKind):
            raise ValueError("an actor kind must be an ActorKind")
        if self.ref is not None and not isinstance(self.ref, str):
            raise ValueError("an actor ref must be a string")
        if self.kind is ActorKind.HUMAN:
            if self.ref is None or not _ACTOR_REF.match(self.ref):
                raise ValueError("a human actor needs a ref matching ^[a-z][a-z0-9-]{0,31}$")
        elif self.ref is not None:
            raise ValueError(f"a {self.kind.value} actor has no ref")

    def encode(self) -> dict[str, str]:
        return {"kind": self.kind.value} | ({"ref": self.ref} if self.ref else {})


COORDINATOR = Actor(ActorKind.COORDINATOR)
MIGRATION = Actor(ActorKind.MIGRATION)


# -- value validators ------------------------------------------------------------------

Check = Callable[[Any], bool]

_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
_PASS = re.compile(r"^p-[0-9a-f]{6,32}$")
_REQUEST_ID = re.compile(r"^q-[0-9a-f]{32}$")  # a cancellation request (ADR-0025)
CANCEL_DECISIONS = frozenset({"accepted", "too-late", "already-terminal", "duplicate"})
CLAIM_LOST_STATES = frozenset({"observed", "cleared"})  # ADR-0026 §3a
_OPAQUE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")  # a tracker's claim id
_DAY = re.compile(r"^\d{4}-\d{2}-\d{2}$")  # a UTC budget day (ADR-0029 §2)
_MONTH = re.compile(r"^\d{4}-\d{2}$")  # a UTC budget month
_OPERATION_KEY = re.compile(r"^[a-z-]+:[A-Za-z0-9][A-Za-z0-9_-]{0,63}:[1-9][0-9]{0,5}"
                            r"(:[A-Za-z0-9][A-Za-z0-9_.:-]{0,127})?$")  # fmt: skip
_TASK = re.compile(r"^[a-z][a-z0-9-]{0,31}:[A-Za-z0-9._/#-]{1,200}$")
_SAFE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,199}$")
_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
_GATE_REPOSITORY = re.compile(r"^[A-Za-z0-9-]{1,39}/[A-Za-z0-9._-]{1,100}$")
_GATE_VERSION = re.compile(r"^\d{1,4}\.\d{1,4}\.\d{1,6}$")
_REF = re.compile(r"^r-[0-9a-f]{24}$")
_ERROR_CLASS = re.compile(r"^[A-Z][A-Za-z]{0,63}Error$")


def _integer(low: int, high: int = 2**53) -> Check:
    return lambda v: type(v) is int and low <= v <= high


def _one_of(values: Iterable[str]) -> Check:
    allowed = frozenset(values)
    return lambda v: isinstance(v, str) and v in allowed


def _pattern(regex: re.Pattern[str]) -> Check:
    return lambda v: isinstance(v, str) and regex.match(v) is not None


def _code(v: Any) -> bool:
    return isinstance(v, str) and v in REASON_CODES


_STATES = _one_of(s.value for s in ExecutionState)
_OP_KINDS = _one_of(k.value for k in OperationKind)
_OP_STATUSES = _one_of(s.value for s in OperationStatus)
_RES_KINDS = _one_of(k.value for k in ResourceKind)
_RES_STATUSES = _one_of(s.value for s in ResourceStatus)
_VERDICTS = _one_of(v.value for v in Verdict)
_COUNT = _integer(0)

OBSERVATIONS: Mapping[str, str] = MappingProxyType(
    {  # what reconciliation saw when it settled an operation (ADR-0024 §1 and §5)
        "outcome-unknown": "Nothing external shows whether the effect happened.",
        "sandbox-absent": "No sandbox exists under the recorded name.",
        "sandbox-removed": "The sandbox was removed before the operation was settled.",
        "resource-quarantined": "The resource was quarantined instead of removed.",
        "evidence-present": "The run's evidence record exists.",
        "evidence-absent": "The run left no evidence record.",
        "branch-at-candidate": "The branch head is the verified candidate.",
        "branch-unmoved": "The branch head is still the expected head.",
        "directory-absent": "The directory does not exist.",
        "directory-unchanged": "The recorded directory is still there.",
        "directory-replaced": "Another directory is at the recorded path.",
        "change-request-merged": "The change request was merged: the task is complete.",
        "change-request-closed": "The change request was closed without merging.",
        # Tracker reconciliation (ADR-0026 §8): what a fresh standing read saw.
        "task-closed": "The task is closed.",
        "task-unlabelled": "The task has lost its eligible label.",
        "task-excluded": "The task has an excluded label.",
        "task-claimed-by-human": "The task has an assignee or a human claim label.",
        "task-claimed-by-other-instance": "Another instance's claim is on the task.",
        "task-gone": "The task is confirmed absent from this repository.",
        "task-claim-lost": "This instance's claim is not on the task.",
        "claim-label-missing": "The claim is there but its label is not.",
        "claim-absent": "The recorded claim object no longer exists.",
        "claim-changed": "The recorded claim object is no longer this instance's authentic claim.",
        "claim-present": "This instance's claim is on the task.",
    }
)
_POSITIVE = _integer(1)
SUSPENSION_REASONS = tuple(r.value for r in SuspensionReason)
BLOCK_DECISIONS = ("release", "abandon")
"""A maintainer's resolution of a blocked task (ADR-0031 §3): ``release`` makes
it eligible again as a new execution; ``abandon`` leaves it out of the backlog.
Both release Ilmar's claim."""

_MESSAGE_PARAM_VALUES: Mapping[str, frozenset[str]] = MappingProxyType(
    {"reason": frozenset(REASON_CODES), "kind": frozenset(k.value for k in ResourceKind)}
)
_MESSAGE_PARAMS: Mapping[str, Check] = MappingProxyType(
    {name: _one_of(values) for name, values in _MESSAGE_PARAM_VALUES.items()}
)


def _template_pattern(text: str) -> re.Pattern[str]:
    """A rendering of ``text`` with every allowed parameter value, and nothing else."""
    parts = _PARAM.split(text)  # literal, name, literal, name, ...
    pattern = "".join(
        re.escape(part) if index % 2 == 0
        else "(?:" + "|".join(re.escape(v) for v in sorted(_MESSAGE_PARAM_VALUES[part])) + ")"
        for index, part in enumerate(parts)
    )  # fmt: skip
    return re.compile(pattern)


_RENDERED = tuple(_template_pattern(text) for text in MESSAGES.values())


def is_rendered_message(text: str) -> bool:
    """``True`` if ``text`` is exactly a registered template's rendering."""
    return any(p.fullmatch(text) for p in _RENDERED)


# Resource kinds whose names are Ilmar's own identifiers (containers, networks,
# branches). Directory names are host paths and stay out of events (§7).
_NAMED_RESOURCE_KINDS = frozenset(
    {ResourceKind.CONTAINER, ResourceKind.NETWORK, ResourceKind.BRANCH}
)


# -- event types and their data allowlists (ADR-0024 §5) --------------------------------


class EventType(enum.Enum):
    COVERAGE_STARTED = "events.coverage_started"
    EXECUTION_BASELINE = "execution.baseline"
    OPERATION_BASELINE = "operation.baseline"
    RESOURCE_BASELINE = "resource.baseline"
    EXECUTION_CREATED = "execution.created"
    EXECUTION_TRANSITIONED = "execution.transitioned"
    OPERATION_INTENDED = "operation.intended"
    OPERATION_SUCCEEDED = "operation.succeeded"
    OPERATION_FAILED = "operation.failed"
    OPERATION_AMBIGUOUS = "operation.ambiguous"
    OPERATION_RECONCILED = "operation.reconciled"
    RESOURCE_RECORDED = "resource.recorded"
    RESOURCE_CHANGED = "resource.changed"
    RESOURCE_QUARANTINED = "resource.quarantined"
    EVIDENCE_RECORDED = "evidence.recorded"
    OWNERSHIP_ACQUIRED = "ownership.acquired"
    EXECUTION_CANCEL_REQUESTED = "execution.cancel_requested"
    EXECUTION_CLAIM_LOST = "execution.claim_lost"
    BUDGET_RESERVED = "budget.reserved"
    BUDGET_CHARGED = "budget.charged"
    INSTANCE_SUSPENDED = "instance.suspended"
    INSTANCE_RESUMED = "instance.resumed"
    INSTANCE_GATE_RECORDED = "instance.gate_recorded"
    INSTANCE_GATE_REVOKED = "instance.gate_revoked"
    EXECUTION_BLOCK_RESOLVED = "execution.block_resolved"
    EXECUTION_MODEL_SPEND = "execution.model_spend"
    EXECUTION_SPEND_REVIEWED = "execution.spend_reviewed"


RESERVED_TYPES: Mapping[str, int] = MappingProxyType(
    {  # name -> the issue that defines its data (ADR-0024 §5); not constructible yet
        "execution.cancelled": 16,  # ADR-0025: unused; the transition is the record
        "task.reconciled": 12,  # ADR-0026: unused; decisions are execution events
        "claim.released": 12,  # ADR-0026: unused; release-orphan-claim is an operation
        "claim.relabelled": 12,  # ADR-0026: unused; restore-claim-label is an operation
        "retry.scheduled": 13,
        "agent.stalled": 13,
        "budget.released": 18,  # ADR-0029 §2: unused; a reservation is never released
        "budget.exceeded": 18,
        "quarantine.resolved": 59,
    }
)

# key -> (check, required)
Schema = Mapping[str, tuple[Check, bool]]

_OPERATION: Schema = {
    "kind": (_OP_KINDS, True),
    "status": (_OP_STATUSES, True),
    "version": (_POSITIVE, True),
    "from": (_OP_STATUSES, False),
    "duration_ms": (_COUNT, False),
    "error_class": (_pattern(_ERROR_CLASS), False),
    "observed": (_one_of(OBSERVATIONS), False),
}
_RESOURCE: Schema = {
    "ref": (_pattern(_REF), True),
    "kind": (_RES_KINDS, True),
    "status": (_RES_STATUSES, True),
    "version": (_POSITIVE, True),
    "attempt": (_POSITIVE, True),
    "from": (_RES_STATUSES, False),
    "name": (_pattern(_SAFE_NAME), False),
}

DATA_SCHEMAS: Mapping[EventType, Schema] = MappingProxyType(
    {
        EventType.COVERAGE_STARTED: {
            "from_schema_version": (_COUNT, True),
            "executions": (_COUNT, True),
            "operations": (_COUNT, True),
            "resources": (_COUNT, True),
        },
        EventType.EXECUTION_BASELINE: {
            "state": (_STATES, True),
            "version": (_POSITIVE, True),
            "transitions_before": (_COUNT, True),
            "evidence_before": (_COUNT, True),
        },
        EventType.OPERATION_BASELINE: {
            "kind": (_OP_KINDS, True),
            "status": (_OP_STATUSES, True),
            "version": (_POSITIVE, True),
        },
        EventType.RESOURCE_BASELINE: {k: v for k, v in _RESOURCE.items() if k != "from"},
        EventType.EXECUTION_CREATED: {
            "state": (_STATES, True),
            "version": (_POSITIVE, True),
        },
        EventType.EXECUTION_TRANSITIONED: {
            "from": (_STATES, True),
            "to": (_STATES, True),
            "version": (_POSITIVE, True),
        },
        EventType.OPERATION_INTENDED: _OPERATION,
        EventType.OPERATION_SUCCEEDED: _OPERATION,
        EventType.OPERATION_FAILED: _OPERATION,
        EventType.OPERATION_AMBIGUOUS: _OPERATION,
        EventType.OPERATION_RECONCILED: _OPERATION,
        EventType.RESOURCE_RECORDED: _RESOURCE,
        EventType.RESOURCE_CHANGED: _RESOURCE,
        EventType.RESOURCE_QUARANTINED: _RESOURCE,
        EventType.EVIDENCE_RECORDED: {
            "run": (_POSITIVE, False),
            "digest": (_pattern(_DIGEST), True),
            "verdict": (_VERDICTS, True),
            "policy_digest": (_pattern(_DIGEST), True),
        },
        EventType.OWNERSHIP_ACQUIRED: {
            "previous_instance": (_pattern(_IDENTIFIER), False),
            "previous_epoch": (_POSITIVE, False),
        },
        EventType.EXECUTION_CANCEL_REQUESTED: {  # ADR-0025; ADR-0026 §3
            # ``request`` for a person's request, ``observed`` for a tracker
            # decision by the coordinator: exactly one, by actor (see create).
            "request": (_pattern(_REQUEST_ID), False),
            "observed": (_one_of(OBSERVATIONS), False),
            "decision": (_one_of(CANCEL_DECISIONS), True),
        },
        EventType.BUDGET_RESERVED: {  # ADR-0029 §2
            "amount_micros": (_POSITIVE, True),
            "day": (_pattern(_DAY), True),
            "month": (_pattern(_MONTH), True),
        },
        EventType.BUDGET_CHARGED: {  # ADR-0029 §2: a reported cost above the reservation
            "amount_micros": (_POSITIVE, True),
            "day": (_pattern(_DAY), True),
            "month": (_pattern(_MONTH), True),
        },
        EventType.INSTANCE_SUSPENDED: {},  # ADR-0029 §3: the reason code is the cause
        EventType.INSTANCE_RESUMED: {  # the reason is operator-resumed; this is what cleared
            "cleared": (_one_of(SUSPENSION_REASONS), True),
        },
        EventType.EXECUTION_MODEL_SPEND: {  # P5 part 2b: what the gateway settled
            "requests": (_integer(0), True),
            "charged_micros": (_integer(0), True),
            "unsettled_micros": (_integer(0), True),
            "overrun_micros": (_integer(0), True),
            "extra_micros": (_integer(0), True),
        },
        EventType.EXECUTION_SPEND_REVIEWED: {  # ADR-0039: which uncertain settlement
            "spend_seq": (_integer(1), True),
        },
        EventType.INSTANCE_GATE_RECORDED: {  # ADR-0040: what the gate record states
            "mode": (_one_of(GATED_MODES), True),
            "repository_id": (_integer(1), True),
            "repository": (_pattern(_GATE_REPOSITORY), True),
            "agent_version": (_pattern(_GATE_VERSION), True),
            "evidence": (_pattern(PULL_URL), True),
            "live_test": (_pattern(RUN_URL), True),
            "fixture": (_pattern(FIXTURE_PATH), True),
            "fixture_sha256": (_pattern(SHA256), True),
            "gate1": (_pattern(GATE1_URL), True),
        },
        EventType.INSTANCE_GATE_REVOKED: {  # ADR-0040: which record
            "mode": (_one_of(GATED_MODES), True),
            "record_seq": (_integer(1), True),
        },
        EventType.EXECUTION_BLOCK_RESOLVED: {  # ADR-0031 §3: a maintainer's decision
            "decision": (_one_of(BLOCK_DECISIONS), True),
        },
        EventType.EXECUTION_CLAIM_LOST: {  # ADR-0026 §3a
            "state": (_one_of(CLAIM_LOST_STATES), True),
            "claim": (_pattern(_OPAQUE_ID), True),
            "observed": (_one_of(OBSERVATIONS), False),
        },
    }
)

# Event types that need a reason code (a failure, a refusal, a quarantine).
_NEEDS_REASON = frozenset({EventType.RESOURCE_QUARANTINED})
# Event types no row derives (ADR-0024 §9, ``Commit.extra_events``): given by the
# core, with an explicit reason and an execution, never an ``unclassified`` fallback.
EXTRA_TYPES = frozenset(
    {
        EventType.EXECUTION_CANCEL_REQUESTED,
        EventType.EXECUTION_CLAIM_LOST,
        EventType.EXECUTION_BLOCK_RESOLVED,
        EventType.EXECUTION_MODEL_SPEND,
        EventType.EXECUTION_SPEND_REVIEWED,
    }
)


# -- credential shapes (ADR-0024 §7, layer 3) ------------------------------------------

_SECRET_PREFIXES = ("github_pat_", "ghp_", "gho_", "ghu_", "ghs_", "ghr_", "sk-ant-",
                    "x-access-token:")  # fmt: skip


def _base64_fragments(prefix: str) -> tuple[str, ...]:
    """Substrings that appear in the base64 of any text containing ``prefix``."""
    fragments = []
    raw = prefix.encode()
    for offset in range(3):
        encoded = base64.b64encode(b"\0" * offset + raw).decode()
        start = -(-offset * 4 // 3) + (1 if offset else 0)  # past chars mixed with padding bytes
        stable = encoded[start : (offset + len(raw)) * 4 // 3]
        if len(stable) >= 8:
            fragments.append(stable)
    return tuple(fragments)


_CREDENTIAL = re.compile(
    r"github_pat_[A-Za-z0-9_]{20,}|gh[pousr]_[A-Za-z0-9]{20,}|sk-ant-[A-Za-z0-9_-]{20,}"
    r"|x-access-token:|x-access-token%3[Aa]"
    + "".join("|" + re.escape(f) for p in _SECRET_PREFIXES for f in _base64_fragments(p))
)


def looks_like_a_credential(text: str) -> bool:
    return _CREDENTIAL.search(text) is not None


# -- the event -------------------------------------------------------------------------


def _at(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise EventEnvelopeError("at must be timezone-aware")
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


@dataclass(frozen=True, slots=True)
class Event:
    """One durable event. Build it with :meth:`create`; ``seq`` is set by the store."""

    type: EventType
    at: str
    instance: str
    epoch: int
    actor: Actor
    data: Mapping[str, Any]
    pass_id: str | None = None
    execution: str | None = None
    attempt: int | None = None
    task: str | None = None
    operation: str | None = None
    run: int | None = None
    reason: str | None = None
    message: str | None = None
    seq: int | None = None
    flags: frozenset[str] = frozenset()

    @classmethod
    def create(
        cls,
        kind: EventType,
        *,
        at: datetime,
        instance: str,
        epoch: int,
        actor: Actor,
        data: Mapping[str, Any] | None = None,
        pass_id: str | None = None,
        execution: str | None = None,
        attempt: int | None = None,
        task: str | None = None,
        operation: str | None = None,
        run: int | None = None,
        reason: str | None = None,
        message: Message | None = None,
    ) -> Event:
        flags: set[str] = set()
        # 1. The envelope: identifiers come from validated domain values, so a
        #    failure here is a broken invariant, never "dropped and flagged".
        _check_envelope(kind, instance=instance, epoch=epoch, pass_id=pass_id,
                        execution=execution, attempt=attempt, operation=operation, run=run,
                        reason=reason, error=EventEnvelopeError)  # fmt: skip
        if reason is None and kind in _NEEDS_REASON:
            reason = "unclassified"
        # The task key is opaque to the core (TaskRef): only a safe form is kept.
        if task is not None and not _TASK.match(task):
            task, flags = None, flags | {"invalid"}
        # 2. ``data`` against the type's allowlist.
        schema = DATA_SCHEMAS[kind]
        kept: dict[str, Any] = {}
        for key, value in (data or {}).items():
            check = schema.get(key)
            if check is None or not check[0](value):
                flags.add("invalid")
                continue
            kept[key] = value
        missing = [k for k, (_, required) in schema.items() if required and k not in kept]
        if missing:
            raise EventEnvelopeError(f"{kind.value} needs {missing}")
        if kind is EventType.EXECUTION_CANCEL_REQUESTED:
            _check_cancel_source(actor, kept)
        if len(json.dumps(kept, sort_keys=True, separators=(",", ":"))) > MAX_DATA_BYTES:
            kept, flags = {}, flags | {"truncated"}
        rendered = message.render() if message is not None else None
        # 3. Defence in depth: validation should make a match impossible.
        if task is not None and looks_like_a_credential(task):
            task, flags = REDACTED, flags | {"redacted"}
        if rendered is not None and looks_like_a_credential(rendered):
            rendered, flags = REDACTED, flags | {"redacted"}
        for key, value in list(kept.items()):
            if isinstance(value, str) and looks_like_a_credential(value):
                kept[key], flags = REDACTED, flags | {"redacted"}
        return cls(
            type=kind,
            at=_at(at),
            instance=instance,
            epoch=epoch,
            actor=actor,
            data=MappingProxyType(dict(sorted(kept.items()))),
            pass_id=pass_id,
            execution=execution,
            attempt=attempt,
            task=task,
            operation=operation,
            run=run,
            reason=reason,
            message=rendered,
            flags=frozenset(flags),
        )

    def with_seq(self, seq: int) -> Event:
        if seq < 1:
            raise EventEnvelopeError("seq starts at 1")
        return _replace(self, seq=seq)

    def with_previous(self, previous: str) -> Event:
        """This event with ``data.from``, the stored row's status before the change."""
        check = DATA_SCHEMAS[self.type].get("from")
        if check is None or not check[0](previous):
            raise EventEnvelopeError(f"{self.type.value} has no valid 'from' {previous!r}")
        merged = dict(sorted({**self.data, "from": previous}.items()))
        return _replace(self, data=MappingProxyType(merged))

    def encode(self) -> dict[str, Any]:
        body: dict[str, Any] = {
            "schema": SCHEMA,
            "at": self.at,
            "instance": self.instance,
            "epoch": self.epoch,
            "actor": self.actor.encode(),
            "type": self.type.value,
        }
        optional = {
            "seq": self.seq,
            "pass": self.pass_id,
            "execution": self.execution,
            "attempt": self.attempt,
            "task": self.task,
            "operation": self.operation,
            "run": self.run,
            "reason": self.reason,
            "message": self.message,
        }
        body |= {k: v for k, v in optional.items() if v is not None}
        if self.data:
            body["data"] = dict(self.data)
        for flag in sorted(self.flags):
            body[flag] = True
        return body

    def to_json(self) -> str:
        """One canonical JSON line (no newline): keys sorted, no extra whitespace."""
        return json.dumps(self.encode(), sort_keys=True, separators=(",", ":"))

    @classmethod
    def from_json(cls, text: str) -> Event:
        """Read a persisted event back, strictly.

        The same guarantees as :meth:`create`, checked rather than applied:
        every envelope identifier, every ``data`` key and value against the
        type's allowlist, required keys, reason codes, templates for the
        message, flags, and ``seq``. A credential shape that ``create``
        would have redacted is refused, and so is ``<redacted>`` without the
        ``redacted`` flag. Finally the line must be exactly the canonical
        encoding of the event it decodes to. Anything else raises
        :class:`CorruptEventError`: nothing is dropped, redacted or filled in.

        This is the reader for Ilmar's own store. External consumers of the
        exported lines may ignore unknown fields (ADR-0024 §8); this reader
        does not, because within one schema version Ilmar writes none.
        """

        def bad(reason: str) -> NoReturn:
            raise CorruptEventError(reason)

        try:
            raw = json.loads(text)
        except ValueError:
            bad("not JSON")
        except RecursionError:
            # json.loads recurses per nesting level; a hostile or damaged
            # line must be reported, not crash the reader.
            bad("JSON nested too deeply")
        if not isinstance(raw, dict):
            bad("not a JSON object")
        if unknown := sorted(raw.keys() - _TOP_LEVEL):
            bad(f"unknown fields {unknown}")
        if raw.get("schema") != SCHEMA:
            bad("not an ilmar.event/1 event")
        try:
            kind = EventType(raw.get("type"))
        except ValueError:
            bad(f"unknown event type {raw.get('type')!r}")
        flags = set()
        for flag in _FLAGS:
            if flag in raw:
                if raw[flag] is not True:
                    bad(f"flag {flag} must be true when present")
                flags.add(flag)
        actor_raw = raw.get("actor")
        if not isinstance(actor_raw, dict) or not actor_raw.keys() <= {"kind", "ref"}:
            bad("actor must be an object with kind and ref only")
        try:
            actor = Actor(ActorKind(actor_raw.get("kind")), actor_raw.get("ref"))
        except ValueError as error:
            bad(f"actor: {error}")
        seq, at, instance, epoch = (
            raw.get("seq"),
            raw.get("at"),
            raw.get("instance"),
            raw.get("epoch"),
        )
        if type(seq) is not int or seq < 1:
            bad("a persisted event has a seq of at least 1")
        if not isinstance(at, str) or not _AT_FORMAT.match(at):
            bad("at is not an RFC 3339 UTC time with microseconds")
        try:
            datetime.strptime(at, "%Y-%m-%dT%H:%M:%S.%fZ")
        except ValueError:
            bad("at is not a valid time")
        for name in ("instance", "pass", "execution", "operation", "reason", "task", "message"):
            if name in raw and not isinstance(raw[name], str):
                bad(f"{name} must be a string")
        if not isinstance(instance, str) or type(epoch) is not int:
            bad("instance and epoch are required")
        _check_envelope(kind, instance=instance, epoch=epoch, pass_id=raw.get("pass"),
                        execution=raw.get("execution"), attempt=raw.get("attempt"),
                        operation=raw.get("operation"), run=raw.get("run"),
                        reason=raw.get("reason"), error=CorruptEventError)  # fmt: skip
        if kind in _NEEDS_REASON and "reason" not in raw:
            bad(f"{kind.value} always records a reason")
        redactions = 0

        def text_value(name: str, value: str, valid: Callable[[str], bool]) -> None:
            nonlocal redactions
            if value == REDACTED:
                if "redacted" not in flags:
                    bad(f"{name} is redacted but the event is not flagged")
                redactions += 1
            elif not valid(value):
                bad(f"{name} is not valid")
            elif looks_like_a_credential(value):
                bad(f"{name} looks like a credential and was not redacted")

        if "task" in raw:
            text_value("task", raw["task"], lambda v: _TASK.match(v) is not None)
        if "message" in raw:
            text_value("message", raw["message"], is_rendered_message)
        data = raw.get("data", {})
        if not isinstance(data, dict) or ("data" in raw and not data):
            bad("data must be a non-empty object when present")
        schema = DATA_SCHEMAS[kind]
        for key, value in data.items():
            if key not in schema:
                bad(f"data.{key} is not allowed for {kind.value}")
            check = schema[key][0]
            if isinstance(value, str):
                text_value(f"data.{key}", value, check)
            elif not check(value):
                bad(f"data.{key} is not valid")
        missing = [k for k, (_, required) in schema.items() if required and k not in data]
        if "truncated" in flags:
            if data:
                bad("a truncated event has no data")
        elif missing:
            bad(f"{kind.value} needs {missing}")
        if "redacted" in flags and not redactions:
            bad("flagged redacted but nothing is redacted")
        event = cls(
            type=kind,
            at=at,
            instance=instance,
            epoch=epoch,
            actor=actor,
            data=MappingProxyType(dict(sorted(data.items()))),
            pass_id=raw.get("pass"),
            execution=raw.get("execution"),
            attempt=raw.get("attempt"),
            task=raw.get("task"),
            operation=raw.get("operation"),
            run=raw.get("run"),
            reason=raw.get("reason"),
            message=raw.get("message"),
            seq=seq,
            flags=frozenset(flags),
        )
        if event.to_json() != text:
            bad("not in canonical form")
        return event


_FLAGS = ("invalid", "redacted", "truncated")
_TOP_LEVEL = frozenset(
    {"schema", "seq", "at", "instance", "epoch", "actor", "type", "pass", "execution",
     "attempt", "task", "operation", "run", "reason", "message", "data", *_FLAGS}
)  # fmt: skip
_AT_FORMAT = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{6}Z$")


def _check_envelope(
    kind: EventType,
    *,
    instance: str,
    epoch: int,
    pass_id: str | None,
    execution: str | None,
    attempt: object,
    operation: str | None,
    run: object,
    reason: str | None,
    error: type[EventEnvelopeError],
) -> None:
    """The envelope rules, for building and for reading alike."""
    if not _IDENTIFIER.match(instance):
        raise error("instance is not an identifier")
    # Only the migration may write epoch 0: a store never owned (ADR-0024 §8).
    if type(epoch) is not int or epoch < (0 if kind in _MIGRATION_TYPES else 1):
        raise error("epoch starts at 1")
    if pass_id is not None and not _PASS.match(pass_id):
        raise error("pass is not a pass id")
    if execution is not None and not _IDENTIFIER.match(execution):
        raise error("execution is not an identifier")
    for name, value in (("attempt", attempt), ("run", run)):
        if value is not None and (type(value) is not int or value < 1):
            raise error(f"{name} starts at 1")
    if operation is not None and not _OPERATION_KEY.match(operation):
        raise error("operation is not a journal key")
    if reason is not None and reason not in REASON_CODES:
        raise error(f"unknown reason code {reason!r}")
    if kind in EXTRA_TYPES and (execution is None or reason is None):
        raise error(f"{kind.value} names its execution and its reason")


def _check_cancel_source(actor: Actor, data: Mapping[str, Any]) -> None:
    """A person's request names its request file; a tracker decision by the
    coordinator names what it observed (ADR-0026 §3). Exactly one, never both."""
    expected = {ActorKind.HUMAN: "request", ActorKind.COORDINATOR: "observed"}.get(actor.kind)
    if expected is None:
        raise EventEnvelopeError("a cancellation comes from a person or the coordinator")
    other = "observed" if expected == "request" else "request"
    if expected not in data or other in data:
        raise EventEnvelopeError(f"a cancellation by {actor.kind.value} carries {expected!r}")


def _replace(event: Event, **changes: Any) -> Event:
    values = {name: getattr(event, name) for name in Event.__slots__} | changes
    return Event(**values)


def resource_ref(key: str) -> str:
    """A resource's reference in events: a digest of its ledger key, so that host
    paths (workspace and checkout names) never appear in an event."""
    return "r-" + hashlib.sha256(key.encode("utf-8")).hexdigest()[:24]


_MIGRATION_TYPES = frozenset(
    {
        EventType.COVERAGE_STARTED,
        EventType.EXECUTION_BASELINE,
        EventType.OPERATION_BASELINE,
        EventType.RESOURCE_BASELINE,
    }
)


# -- derivation from a commit (ADR-0024 §2, layer 1) ------------------------------------


@dataclass(frozen=True, slots=True)
class EventContext:
    """What a commit's rows do not hold. Keyed by the row the value belongs to:
    an execution id, an operation key or a resource key."""

    at: datetime
    instance: str
    epoch: int
    actor: Actor = COORDINATOR
    pass_id: str | None = None
    reasons: Mapping[str, str] = field(default_factory=dict)
    durations_ms: Mapping[str, int] = field(default_factory=dict)
    error_classes: Mapping[str, str] = field(default_factory=dict)
    reconciled: frozenset[str] = frozenset()
    tasks: Mapping[str, str] = field(default_factory=dict)
    """Execution id -> ``provider:key``, when the caller knows it."""
    observations: Mapping[str, str] = field(default_factory=dict)
    """Operation key -> what reconciliation observed (:data:`OBSERVATIONS`)."""


# Transitions into these states record why.
_REASONED_STATES = frozenset(
    {ExecutionState.FAILED, ExecutionState.CANCELLED, ExecutionState.RETRY_WAIT}
)
_OPERATION_EVENTS: Mapping[OperationStatus, EventType] = MappingProxyType(
    {
        OperationStatus.INTENDED: EventType.OPERATION_INTENDED,
        OperationStatus.SUCCEEDED: EventType.OPERATION_SUCCEEDED,
        OperationStatus.FAILED: EventType.OPERATION_FAILED,
        OperationStatus.AMBIGUOUS: EventType.OPERATION_AMBIGUOUS,
    }
)


def rows_needing_a_reason(commit: Commit) -> tuple[str, ...]:
    """The keys of the rows whose events must carry a reason code: a transition into
    failed, cancelled or retry_wait, a failed or ambiguous operation, and a
    quarantined resource."""
    keys = [e.id for e, t in commit.applied if t.to_state in _REASONED_STATES]
    keys += [o.key for o in commit.operations
             if o.status in (OperationStatus.FAILED, OperationStatus.AMBIGUOUS)]  # fmt: skip
    keys += [r.key for r in commit.resources if r.status is ResourceStatus.QUARANTINED]
    return tuple(keys)


def _task(execution: Execution) -> str:
    return f"{execution.task.provider}:{execution.task.key}"


def derive_events(commit: Commit, context: EventContext) -> tuple[Event, ...]:
    """The mandatory events of ``commit``: exactly one per changed row, in the
    commit's own order. ``data.from`` of an operation or resource change is
    filled by the store from the stored row (:meth:`Event.with_previous`)."""

    def new(kind: EventType, **values: Any) -> Event:
        return Event.create(kind, at=context.at, instance=context.instance,
                            epoch=context.epoch, actor=context.actor,
                            pass_id=context.pass_id, **values)  # fmt: skip

    if commit.reasons:  # the commit's own reasons; the context's, if any, take precedence
        context = replace(context, reasons={**commit.reasons, **context.reasons})
    if given := sorted(k for k, code in context.reasons.items() if not is_failure_code(code)):
        # ``unclassified`` is only ever the fallback below, never an input.
        raise EventEnvelopeError(f"not a reason code a caller may give, for {given}")
    events: list[Event] = []
    for execution, _transition in commit.created:
        events.append(new(
            EventType.EXECUTION_CREATED, execution=execution.id, attempt=execution.attempt,
            task=_task(execution), reason=context.reasons.get(execution.id),
            data={"state": execution.state.value, "version": execution.version},
        ))  # fmt: skip
    for execution, transition in commit.applied:
        reason = context.reasons.get(execution.id)
        if reason is None and transition.to_state in _REASONED_STATES:
            reason = "unclassified"
        source = transition.from_state
        if source is None:
            raise EventEnvelopeError("an applied transition has a source state")
        events.append(new(
            EventType.EXECUTION_TRANSITIONED, execution=execution.id,
            attempt=execution.attempt, task=_task(execution), reason=reason,
            data={"from": source.value, "to": transition.to_state.value,
                  "version": execution.version},
        ))  # fmt: skip
    for operation in commit.operations:
        events.append(_operation_event(operation, context, new))
    for resource in commit.resources:
        events.append(_resource_event(resource, context, new))
    return tuple(events)


def accounting_events(commit: Commit, context: EventContext) -> tuple[Event, ...]:
    """The events of a commit's charges and suspension change (ADR-0029 §2 and §3):
    one per charge, then one for a suspension set or cleared. They pair with
    those rows by construction and come after the commit's row events."""

    def new(kind: EventType, **values: Any) -> Event:
        return Event.create(kind, at=context.at, instance=context.instance,
                            epoch=context.epoch, actor=context.actor,
                            pass_id=context.pass_id, **values)  # fmt: skip

    events: list[Event] = []
    for charge in commit.charges:
        kind = (
            EventType.BUDGET_RESERVED
            if charge.kind.value == "reservation"
            else EventType.BUDGET_CHARGED
        )
        events.append(new(
            kind, execution=charge.execution_id, attempt=charge.attempt,
            reason="budget-reserved" if kind is EventType.BUDGET_RESERVED else None,
            data={"amount_micros": charge.amount_micros, "day": charge.day,
                  "month": charge.month},
        ))  # fmt: skip
    if commit.suspend is not None:
        events.append(new(EventType.INSTANCE_SUSPENDED, reason=commit.suspend.reason.value))
    return tuple(events)


def gate_events(
    commit: Commit, context: EventContext, *, revoked_seq: int | None
) -> tuple[Event, ...]:
    """The event of a gate record written or revoked (ADR-0040). ``revoked_seq``
    is the stored record a revocation ends; the store supplies it."""
    gate = commit.gate
    if gate is None:
        return ()

    def new(kind: EventType, reason: str, data: Mapping[str, Any]) -> Event:
        return Event.create(kind, at=context.at, instance=context.instance,
                            epoch=context.epoch, actor=context.actor,
                            pass_id=context.pass_id, reason=reason, data=data)  # fmt: skip

    if isinstance(gate, GateRecorded):
        recorded = json.loads(gate.evidence.to_json())
        return (new(EventType.INSTANCE_GATE_RECORDED, "gate-recorded", recorded),)
    if revoked_seq is None:
        raise EventEnvelopeError("a revocation names the record it revokes")
    data = {"mode": gate.mode, "record_seq": revoked_seq}
    return (new(EventType.INSTANCE_GATE_REVOKED, "gate-revoked", data),)


def resumed_event(cleared: str, context: EventContext) -> Event:
    """``instance.resumed``: the store adds it, knowing which suspension it cleared."""
    return Event.create(EventType.INSTANCE_RESUMED, at=context.at, instance=context.instance,
                        epoch=context.epoch, actor=context.actor, pass_id=context.pass_id,
                        reason="operator-resumed", data={"cleared": cleared})  # fmt: skip


def _operation_event(
    operation: Operation, context: EventContext, new: Callable[..., Event]
) -> Event:
    kind = _OPERATION_EVENTS[operation.status]
    if operation.key in context.reconciled and operation.status.is_final:
        kind = EventType.OPERATION_RECONCILED
    reason = context.reasons.get(operation.key)
    if reason is None and operation.status in (OperationStatus.FAILED, OperationStatus.AMBIGUOUS):
        reason = "unclassified"
    data: dict[str, Any] = {
        "kind": operation.kind.value,
        "status": operation.status.value,
        "version": operation.version,
    }
    if (duration := context.durations_ms.get(operation.key)) is not None:
        data["duration_ms"] = duration
    if (error_class := context.error_classes.get(operation.key)) is not None:
        data["error_class"] = error_class
    if (observed := context.observations.get(operation.key)) is not None:
        data["observed"] = observed
    return new(kind, execution=operation.execution_id, attempt=operation.attempt,
               operation=operation.key, reason=reason, data=data)  # fmt: skip


def _resource_data(resource: Resource) -> dict[str, Any]:
    data: dict[str, Any] = {
        "ref": resource_ref(resource.key),
        "kind": resource.kind.value,
        "status": resource.status.value,
        "version": resource.version,
        "attempt": resource.attempt,
    }
    if resource.kind in _NAMED_RESOURCE_KINDS:
        data["name"] = resource.name
    return data


def _resource_event(resource: Resource, context: EventContext, new: Callable[..., Event]) -> Event:
    if resource.status is ResourceStatus.QUARANTINED:
        kind = EventType.RESOURCE_QUARANTINED
    elif resource.version == 1:
        kind = EventType.RESOURCE_RECORDED
    else:
        kind = EventType.RESOURCE_CHANGED
    return new(kind, execution=resource.execution_id, attempt=resource.attempt,
               reason=context.reasons.get(resource.key), data=_resource_data(resource))  # fmt: skip


# -- the migration baseline (ADR-0024 §8) ----------------------------------------------


@dataclass(frozen=True, slots=True)
class BaselineExecution:
    execution: Execution
    transitions_before: int
    evidence_before: int


def baseline_events(
    *,
    at: datetime,
    instance: str,
    epoch: int,
    from_schema_version: int,
    executions: Sequence[BaselineExecution],
    operations: Sequence[Operation],
    resources: Sequence[Resource],
) -> tuple[Event, ...]:
    """``events.coverage_started``, then one snapshot per existing row. The
    snapshot is what exists now, not a history (no backfill)."""

    def new(kind: EventType, **values: Any) -> Event:
        return Event.create(kind, at=at, instance=instance, epoch=epoch, actor=MIGRATION,
                            **values)  # fmt: skip

    events = [
        new(EventType.COVERAGE_STARTED, message=Message("coverage-started"), data={
            "from_schema_version": from_schema_version, "executions": len(executions),
            "operations": len(operations), "resources": len(resources),
        })
    ]  # fmt: skip
    for item in executions:
        e = item.execution
        events.append(new(
            EventType.EXECUTION_BASELINE, execution=e.id, attempt=e.attempt, task=_task(e),
            data={"state": e.state.value, "version": e.version,
                  "transitions_before": item.transitions_before,
                  "evidence_before": item.evidence_before},
        ))  # fmt: skip
    for o in operations:
        events.append(new(
            EventType.OPERATION_BASELINE, execution=o.execution_id, attempt=o.attempt,
            operation=o.key,
            data={"kind": o.kind.value, "status": o.status.value, "version": o.version},
        ))  # fmt: skip
    for r in resources:
        events.append(new(EventType.RESOURCE_BASELINE, execution=r.execution_id,
                          attempt=r.attempt, data=_resource_data(r)))  # fmt: skip
    return tuple(events)
