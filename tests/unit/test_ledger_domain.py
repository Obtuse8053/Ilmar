# SPDX-License-Identifier: Apache-2.0
"""Domain rules of fencing, the journal and the ledger (#11a, ADR-0020)."""

from __future__ import annotations

import pytest

from ilmar.domain.ledger import (
    Commit,
    Fence,
    Operation,
    OperationKind,
    OperationStatus,
    Resource,
    ResourceKind,
    ResourceStatus,
    changed_paths_digest,
    operation_key,
    resource_key,
)

from helpers import at

K = OperationKind
ST = OperationStatus
R = ResourceStatus
FENCE = Fence("inst", 1)


def test_idempotency_keys_are_deterministic_and_scoped() -> None:
    assert operation_key(K.VERIFY, "e1", 2, "run-3") == "verify:e1:2:run-3"
    assert operation_key(K.CLAIM_TASK, "e1", 1) == operation_key(K.CLAIM_TASK, "e1", 1)
    assert operation_key(K.CLAIM_TASK, "e1", 1) != operation_key(K.CLAIM_TASK, "e1", 2)


@pytest.mark.parametrize(
    ("execution_id", "attempt", "step"),
    [("../e1", 1, ""), ("e1", 0, ""), ("e1", 1, "has space"), ("e1", 1, "x" * 200)],
)
def test_idempotency_keys_refuse_unsafe_parts(execution_id: str, attempt: int, step: str) -> None:
    with pytest.raises(ValueError, match=r"identifier|attempt"):
        operation_key(K.CLAIM_TASK, execution_id, attempt, step)


def test_only_effects_that_are_safe_to_repeat_are_repeatable() -> None:
    repeatable = {kind for kind in K if kind.repeatable}
    assert repeatable == {
        K.CLAIM_TASK,
        K.RELEASE_TASK,
        K.STOP_CONTAINER,
        K.FREEZE_CANDIDATE,
        K.UPSERT_CHANGE_REQUEST,
    }
    for risky in (K.PUBLISH_REVISION, K.START_CONTAINER, K.DELETE_WORKSPACE, K.POST_UPDATE):
        assert not risky.repeatable


@pytest.mark.parametrize(
    ("before", "after", "allowed"),
    [
        (ST.INTENDED, ST.SUCCEEDED, True),
        (ST.INTENDED, ST.FAILED, True),
        (ST.INTENDED, ST.AMBIGUOUS, True),
        (ST.INTENDED, ST.INTENDED, True),  # a repeatable effect retried
        (ST.AMBIGUOUS, ST.SUCCEEDED, True),
        (ST.AMBIGUOUS, ST.INTENDED, True),
        (ST.SUCCEEDED, ST.FAILED, False),
        (ST.SUCCEEDED, ST.INTENDED, False),
        (ST.FAILED, ST.SUCCEEDED, False),
        (ST.AMBIGUOUS, ST.AMBIGUOUS, False),
    ],
)
def test_operation_status_moves(before: ST, after: ST, allowed: bool) -> None:
    assert before.can_become(after) is allowed


def test_an_operation_key_must_belong_to_its_operation() -> None:
    with pytest.raises(ValueError, match="does not belong"):
        Operation(
            key="claim-task:e2:1",
            execution_id="e1",
            attempt=1,
            kind=K.CLAIM_TASK,
            status=ST.INTENDED,
            epoch=1,
            version=1,
            created_at=at(0),
            updated_at=at(0),
        )


def test_intent_fields_are_small_strings_and_frozen() -> None:
    with pytest.raises(ValueError, match="too long"):
        Operation.intended(K.CLAIM_TASK, "e1", 1, fence=FENCE, now=at(0), intent={"x": "y" * 3000})
    with pytest.raises(ValueError, match="strings"):
        Operation.intended(K.CLAIM_TASK, "e1", 1, fence=FENCE, now=at(0), intent={"x": 1})  # type: ignore[dict-item]
    operation = Operation.intended(K.CLAIM_TASK, "e1", 1, fence=FENCE, now=at(0), intent={"a": "b"})
    with pytest.raises(TypeError):
        operation.intent["a"] = "c"  # type: ignore[index]


def test_a_moved_operation_takes_the_movers_epoch() -> None:
    operation = Operation.intended(K.CLAIM_TASK, "e1", 1, fence=FENCE, now=at(0))
    moved = operation.moved(ST.INTENDED, fence=Fence("inst", 2), now=at(1))
    assert (moved.epoch, moved.version) == (2, 2)
    with pytest.raises(ValueError, match="cannot move"):
        moved.moved(ST.SUCCEEDED, fence=FENCE, now=at(2)).moved(ST.FAILED, fence=FENCE, now=at(3))


def test_resources_need_an_identity_once_active_and_keep_it() -> None:
    planned = Resource.intended(ResourceKind.CONTAINER, "e1", 1, "c", owner_token="t", now=at(0))
    with pytest.raises(ValueError, match="identity"):
        planned.moved(R.ACTIVE, now=at(1))
    active = planned.moved(R.ACTIVE, now=at(1), identity="abc")
    with pytest.raises(ValueError, match="never changes"):
        active.moved(R.RELEASING, now=at(2), identity="other")
    assert active.moved(R.RELEASING, now=at(2)).identity == "abc"
    with pytest.raises(ValueError, match="cannot move"):
        active.moved(R.QUARANTINED, now=at(2)).moved(R.RELEASED, now=at(3))
    with pytest.raises(ValueError, match="owner token"):
        Resource.intended(ResourceKind.WORKSPACE, "e1", 1, "/w", owner_token="", now=at(0))
    assert resource_key(ResourceKind.BRANCH, "e1", 1, "ilmar/x") == "branch:e1:1:ilmar/x"


def test_a_commit_changes_each_record_once() -> None:
    operation = Operation.intended(K.CLAIM_TASK, "e1", 1, fence=FENCE, now=at(0))
    with pytest.raises(ValueError, match="at most once"):
        Commit(operations=(operation, operation))
    assert Commit().empty


def test_the_changed_paths_digest_ignores_order_and_duplicates() -> None:
    assert changed_paths_digest(["b", "a"]) == changed_paths_digest(["a", "b", "a"])
    assert changed_paths_digest(["a"]) != changed_paths_digest(["a", "b"])
    assert changed_paths_digest([]).startswith("sha256:")


@pytest.mark.parametrize(("instance_id", "epoch"), [("../x", 1), ("inst", 0)])
def test_fences_are_validated(instance_id: str, epoch: int) -> None:
    with pytest.raises(ValueError, match=r"identifier|epoch"):
        Fence(instance_id, epoch)


def test_a_freeze_has_exactly_one_key_per_attempt() -> None:
    with pytest.raises(ValueError, match="a freeze has no step"):
        operation_key(K.FREEZE_CANDIDATE, "e1", 1, "retry-1")
