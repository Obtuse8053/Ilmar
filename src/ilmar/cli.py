# SPDX-License-Identifier: Apache-2.0
"""Command-line entry point."""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Sequence
from pathlib import Path

from ilmar import __version__
from ilmar.adapters.registry import plan
from ilmar.config import load_config
from ilmar.domain.errors import ConfigError


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    if args.command == "config":
        return _config_validate(Path(args.path), check_environment=args.check_environment)
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


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
