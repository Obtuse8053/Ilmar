# SPDX-License-Identifier: Apache-2.0
"""Command-line entry point."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path

from ilmar import __version__
from ilmar.config import load_config
from ilmar.domain.errors import ConfigError


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    if args.command == "config":
        return _config_validate(Path(args.path))
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
    return parser


def _config_validate(path: Path) -> int:
    try:
        config = load_config(path)
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(f"{path}: OK")
    for port, adapter in config.adapters.items():
        print(f"  {port}: {adapter.kind}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
