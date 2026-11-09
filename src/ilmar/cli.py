# SPDX-License-Identifier: Apache-2.0
"""Command-line entry point."""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

from ilmar import __version__, cli_cancel, cli_events
from ilmar.adapters.registry import build, plan
from ilmar.config import load_config
from ilmar.core.coordinator import Coordinator, ExitCode
from ilmar.core.ownership import Ownership
from ilmar.core.safe_fs import ensure_directory, require_canonical_root
from ilmar.domain.errors import ConfigError, FencingError, OwnershipConflictError

UNATTENDED_DISABLED = (
    "unattended operation is disabled: only `ilmar run --once` is available, run by a "
    "person who watches it. It stays disabled until the real Claude Code smoke test "
    "(issue 44) has passed and unattended operation (issue 20) is designed and approved."
)


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    words = list(sys.argv[1:] if argv is None else argv)
    try:
        args = parser.parse_args(words)
    except SystemExit as stop:
        # `ilmar events` uses exit 2 for "no event coverage": a usage error there is 3.
        if words[:1] in (["events"], ["cancel"]) and stop.code == 2:
            return cli_events.CONFIGURATION
        raise
    if args.command == "events":
        return cli_events.run(args)
    if args.command == "cancel":
        return cli_cancel.run(args)
    if args.command == "config":
        return _config_validate(Path(args.path), check_environment=args.check_environment)
    if args.command == "run":
        if not args.once:
            print(f"error: {UNATTENDED_DISABLED}", file=sys.stderr)
            return int(ExitCode.CONFIGURATION)
        return _run_once(Path(args.config))
    parser.print_help()
    return 2


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="ilmar", description="Autonomous software factory.")
    parser.add_argument("--version", action="version", version=f"ilmar {__version__}")
    commands = parser.add_subparsers(dest="command")

    config = commands.add_parser("config", help="work with configuration files")
    config_commands = config.add_subparsers(dest="config_command", required=True)
    validate = config_commands.add_parser("validate", help="check a configuration file")
    validate.add_argument("path", nargs="?", default="ilmar.toml")

    run = commands.add_parser("run", help="process work (needs the instance lock)")
    run.add_argument(
        "--once",
        action="store_true",
        help="one pass, then exit (required: unattended operation is disabled)",
    )
    run.add_argument(
        "--config",
        required=True,
        help="the configuration file; required, never taken from the working directory",
    )
    cli_events.add_parser(commands)
    cli_cancel.add_parser(commands)
    validate.add_argument(
        "--check-environment",
        action="store_true",
        help="also check that every referenced secret variable is set (values are never shown)",
    )
    return parser


def _config_validate(path: Path, *, check_environment: bool = False) -> int:
    """Validate structure, every adapter's options and the rules between ports.

    Nothing is created and no secret is read; ``--check-environment`` only
    checks that the named variables are set.
    """
    try:
        blueprint = plan(load_config(path))
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(f"{path}: OK")
    for port, kind in blueprint.kinds.items():
        print(f"  {port}: {kind}")
    target = blueprint.target
    print(f"  target: {target.repository.provider}:{target.repository.slug} ({target.base_branch})")
    sandbox = blueprint.agent_sandbox
    hosts = ", ".join(sandbox.allowed_hosts) or "none"
    print(f"  agent sandbox: {sandbox.image}, network {sandbox.network.value} ({hosts})")
    policy = blueprint.context.verification
    names = ", ".join(c.name for c in policy.commands)
    print(f"  verification: {names} ({policy.digest()})")
    missing = [name for name in blueprint.environment if not os.environ.get(name)]
    if check_environment:
        for name in blueprint.environment:
            print(f"  secret {name}: {'missing' if name in missing else 'set'}")
        if missing:
            print(f"error: not set: {', '.join(missing)}", file=sys.stderr)
            return 1
    else:
        print(f"  secrets read from: {', '.join(blueprint.environment) or 'none'}")
    return 0


def _run_once(path: Path) -> int:
    """One coordinator pass. Exit codes: 0 success, 1 failure, 2 blocked, 3 configuration."""
    try:
        config = load_config(path)
        config.state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        components = build(config)
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return int(ExitCode.CONFIGURATION)
    with components:
        try:
            ownership = Ownership.take(
                layout=components.layout,
                store=components.execution_store,
                instance_id=components.instance_id,
                now=_now(),
            )
        except (FencingError, OwnershipConflictError, OSError) as exc:
            print(f"error: cannot own this instance: {exc}", file=sys.stderr)
            return int(ExitCode.CONFIGURATION)
        with ownership:
            try:
                ensure_directory(
                    components.layout.workspaces_root,
                    root=components.layout.coordinator_root.parent,
                )
                require_canonical_root(components.layout.workspaces_root)
                coordinator = Coordinator.create(
                    components,
                    ownership,
                    max_attempts=config.limits.max_attempts,
                    agent_timeout_seconds=config.limits.agent_timeout_seconds,
                    clock=_now,
                )
                report = coordinator.run_once()
            except FencingError as exc:
                print(f"error: ownership lost: {exc}", file=sys.stderr)
                return int(ExitCode.CONFIGURATION)
            except OwnershipConflictError as exc:
                print(f"error: {exc}", file=sys.stderr)
                return int(ExitCode.CONFIGURATION)
            except Exception as exc:  # the process boundary: never report a false outcome
                print(
                    f"error: unexpected {type(exc).__name__}; the journal shows where work "
                    "stopped and a human must look",
                    file=sys.stderr,
                )
                return int(ExitCode.BLOCKED)
    print(report.summary)
    for execution_id in report.completed_tasks:
        print(f"task completed (change request merged): {execution_id}")
    return int(report.code)


def _now() -> datetime:
    return datetime.now(UTC)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
