# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

from ilmar.adapters.memory import InMemoryExecutionStore
from ilmar.adapters.sqlite import SQLiteExecutionStore
from ilmar.ports.execution_store import ExecutionStore

from contracts.execution_store import ExecutionStoreContract

# Static conformance checks: mypy fails if an adapter drifts from the port.
_memory: ExecutionStore = InMemoryExecutionStore()
_sqlite: type[ExecutionStore] = SQLiteExecutionStore


class TestInMemoryExecutionStore(ExecutionStoreContract):
    def make_store(self, tmp_path: Path) -> Iterator[ExecutionStore]:
        yield InMemoryExecutionStore()


class TestSQLiteExecutionStoreOnDisk(ExecutionStoreContract):
    def make_store(self, tmp_path: Path) -> Iterator[ExecutionStore]:
        with SQLiteExecutionStore(tmp_path / "executions.sqlite3") as store:
            yield store


class TestSQLiteExecutionStoreInMemory(ExecutionStoreContract):
    def make_store(self, tmp_path: Path) -> Iterator[ExecutionStore]:
        with SQLiteExecutionStore(":memory:") as store:
            yield store
