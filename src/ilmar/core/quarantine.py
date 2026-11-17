# SPDX-License-Identifier: Apache-2.0
"""Quarantined resources and their human resolutions (ADR-0020, ADR-0042).

A quarantined row never changes. A person's ``resource.resolved`` event says
it was verified gone or unchanged; such a resource no longer holds its
execution. Nothing here, or anywhere in the coordinator, writes a resolution.
"""

from __future__ import annotations

from collections.abc import Sequence

from ilmar.domain.events import EventType
from ilmar.domain.ledger import Resource, ResourceStatus
from ilmar.ports.execution_store import ExecutionStore


def resolved_keys(store: ExecutionStore, execution_id: str) -> frozenset[str]:
    return frozenset(
        str(e.data.get("resource"))
        for e in store.events(execution_id=execution_id)
        if e.type is EventType.RESOURCE_RESOLVED
    )


def unresolved(store: ExecutionStore, execution_id: str) -> Sequence[Resource]:
    """The execution's quarantined resources that no person has resolved yet."""
    done = resolved_keys(store, execution_id)
    return tuple(
        r
        for r in store.resources(execution_id=execution_id, statuses={ResourceStatus.QUARANTINED})
        if r.key not in done
    )
