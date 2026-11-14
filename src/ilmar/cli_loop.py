# SPDX-License-Identifier: Apache-2.0
"""``ilmar run --loop`` (#91; ADR-0030 §1, §3, ADR-0033, ADR-0040).

Order, each step fail closed:

1. Read the configuration. Without ``[autonomy] mode = "supervised"``, exit 3
   before anything is written.
2. **Record this start** in the durable start log, before the store is opened
   or anything is contacted (ADR-0033). An unusable log exits 78; a restart
   loop writes the ``restart-loop`` suspension (when the store opens) and
   exits 78.
3. Build the deployment and take the instance lock once, for the whole loop.
   A second loop on the same store is refused here (exit 3).
4. Before every pass, read the facts fresh: the configuration file's digest
   (any change, removal or unreadable file stops the loop: it is never
   reloaded), the active gate record, the live
   repository ID, the kill switch, the suspension and the clock
   (:func:`ilmar.core.loop.loop_blocker`). Any refusal ends the loop with its
   code.
5. Run one coordinator pass, then wait ``poll_interval_seconds``. SIGTERM or
   SIGINT stop the loop at the next wait (exit 0). The loop never restarts
   itself.
"""

from __future__ import annotations

import hashlib
import signal
import sys
import threading
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ilmar.adapters.registry import build
from ilmar.config import IlmarConfig, load_config
from ilmar.core import kill_switch
from ilmar.core.components import Components
from ilmar.core.coordinator import Coordinator, ExitCode
from ilmar.core.layout import WorkspaceLayout
from ilmar.core.loop import LoopFacts, loop_blocker, run_loop
from ilmar.core.ownership import Ownership
from ilmar.core.safe_fs import ensure_directory, require_canonical_root
from ilmar.core.starts import StartLimits, account_start
from ilmar.core.state_machine import RetryBackoff
from ilmar.domain.budget import Suspension, SuspensionReason
from ilmar.domain.errors import (
    ConfigError,
    FencingError,
    LedgerIntegrityError,
    OwnershipConflictError,
    ProviderError,
    StartLogError,
)
from ilmar.domain.ledger import Commit

BOOT_ID = Path("/proc/sys/kernel/random/boot_id")


def make_coordinator(
    config: IlmarConfig,
    components: Components,
    ownership: Ownership,
    clock: Callable[[], datetime],
) -> Coordinator:
    """The coordinator ``ilmar run`` drives, once or in a loop."""
    ensure_directory(
        components.layout.workspaces_root, root=components.layout.coordinator_root.parent
    )
    require_canonical_root(components.layout.workspaces_root)
    return Coordinator.create(
        components,
        ownership,
        max_attempts=config.limits.max_attempts,
        agent_timeout_seconds=config.limits.agent_timeout_seconds,
        retry_backoff=RetryBackoff(
            config.limits.retry_backoff_base_seconds, config.limits.retry_backoff_max_seconds
        ),
        stall_timeout_seconds=config.limits.stall_timeout_seconds,
        execution_wall_seconds=config.limits.execution_wall_seconds,
        budget=config.budget.limits if config.budget else None,
        kill_switch=kill_switch.checker(config.budget.kill_switch_file if config.budget else None),
        clock=clock,
    )


def run(
    path: Path,
    *,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
    stop: threading.Event | None = None,
    builder: Callable[[IlmarConfig], Any] = build,
    boot_id: Callable[[], str] | None = None,
    limits: StartLimits = StartLimits(),  # noqa: B008 - immutable defaults
    out: Any = None,
    err: Any = None,
) -> int:
    out = out or sys.stdout
    err = err or sys.stderr
    started_with = configuration_digest(path)
    try:
        config = load_config(path)
    except ConfigError as exc:
        print(f"error: {exc}", file=err)
        return int(ExitCode.CONFIGURATION)
    if configuration_digest(path) != started_with:  # changed while it was read
        print("error: the configuration changed while it was read", file=err)
        return int(ExitCode.CONFIGURATION)
    if config.autonomy.mode != "supervised":
        print(f'error: loop mode needs [autonomy] mode = "supervised" (it is '
              f'"{config.autonomy.mode}"); `ilmar run --once` is unchanged', file=err)  # fmt: skip
        return int(ExitCode.CONFIGURATION)
    layout = WorkspaceLayout.under(config.state_dir)
    try:
        layout.coordinator_root.mkdir(mode=0o700, parents=True, exist_ok=True)
        breach = account_start(
            layout.coordinator_root, now=now(), boot_id=(boot_id or _boot_id)(), limits=limits
        )
    except (StartLogError, OSError) as exc:
        print(f"error: the start log is unusable; nothing started: {exc}", file=err)
        return int(ExitCode.SUSPENDED)
    try:
        components = builder(config)
    except ConfigError as exc:
        print(f"error: {exc}", file=err)
        return int(ExitCode.CONFIGURATION)
    stop = stop or _signal_stop()
    with components:
        try:
            ownership = Ownership.take(layout=components.layout, store=components.execution_store,
                                       instance_id=components.instance_id, now=now())  # fmt: skip
        except (FencingError, OwnershipConflictError, OSError) as exc:
            print(f"error: cannot own this instance (is another loop running?): {exc}", file=err)
            return int(ExitCode.CONFIGURATION)
        with ownership:
            store = components.execution_store
            if breach is not None:
                _suspend_restart_loop(store, ownership, breach, now())
                print(f"suspended: restart loop ({breach})", file=err)
                return int(ExitCode.SUSPENDED)
            coordinator = make_coordinator(config, components, ownership, now)
            engaged = kill_switch.checker(config.budget.kill_switch_file if config.budget else None)

            def check() -> tuple[int, str] | None:
                try:
                    live: int | str = components.source_control.repository_id(
                        components.target.repository
                    )
                except ProviderError as exc:
                    live = str(exc)[:200]
                runner = config.adapters.get("agent_runner")
                version = runner.options.get("expected_version") if runner else None
                return loop_blocker(LoopFacts(
                    configuration=configuration_digest(path), started_with=started_with,
                    mode=config.autonomy.mode,
                    expected_version=version if isinstance(version, str) else None,
                    repository_id=config.autonomy.repository_id, live_repository_id=live,
                    gates=store.gate_records(), kill_switch_engaged=engaged(),
                    suspension=store.suspension(),
                    latest_update=store.latest_update(), now=now(),
                ))  # fmt: skip

            def run_pass() -> int:
                try:
                    report = coordinator.run_once()
                except (FencingError, OwnershipConflictError) as exc:
                    print(f"error: ownership lost: {exc}", file=err)
                    return int(ExitCode.CONFIGURATION)
                except Exception as exc:  # the process boundary: never report a false outcome
                    print(f"error: unexpected {type(exc).__name__}; the journal shows where "
                          "work stopped and a human must look", file=err)  # fmt: skip
                    return int(ExitCode.BLOCKED)
                print(report.summary, file=out)
                return int(report.code)

            return run_loop(
                check=check, run_pass=run_pass, poll_interval=config.autonomy.poll_interval_seconds,
                stop=stop, report=lambda line: print(line, file=err),
            )  # fmt: skip


def configuration_digest(path: Path) -> str:
    """``sha256:<hex>`` of the configuration file's bytes, or why it cannot be read.
    The loop never reloads its configuration: any difference stops it."""
    try:
        return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError as exc:
        return f"unreadable: {exc.strerror or type(exc).__name__}"


def _suspend_restart_loop(store: Any, ownership: Ownership, breach: str, at: datetime) -> None:
    if store.suspension() is not None:
        return
    try:
        store.commit(ownership.fence, Commit(suspend=Suspension(
            SuspensionReason.RESTART_LOOP, at, detail=breach[:200])))  # fmt: skip
    except LedgerIntegrityError:
        if store.suspension() is None:
            raise


def _boot_id() -> str:
    try:
        return BOOT_ID.read_text().strip()[:64] or "unknown"
    except OSError:
        return "unknown"


def _signal_stop() -> threading.Event:
    stop = threading.Event()
    for number in (signal.SIGTERM, signal.SIGINT):
        signal.signal(number, lambda *_: stop.set())
    return stop
