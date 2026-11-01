# SPDX-License-Identifier: Apache-2.0
"""Loading and validating ``ilmar.toml``.

The file has three sections::

    [ilmar]                      # general settings
    [limits]                     # execution limits
    [adapters.<port>]            # one table per port: kind + options

Validation is structural: it does not check that an adapter ``kind`` is
installed. Option keys that look like secrets are rejected unless they end in
``_env``, which means "the name of an environment variable" (ADR-0007).
"""

from __future__ import annotations

import re
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Any

from ilmar.domain.errors import ConfigError

PORT_NAMES: tuple[str, ...] = (
    "task_provider",
    "source_control",
    "container_runtime",
    "agent_runner",
    "execution_store",
)

_SECRET_LIKE = re.compile(
    r"(token|secret|password|passwd|api[_-]?key|private[_-]?key|credential)", re.IGNORECASE
)
_KIND = re.compile(r"^[a-z][a-z0-9-]*$")


@dataclass(frozen=True, slots=True)
class AdapterConfig:
    kind: str
    options: Mapping[str, Any] = field(default_factory=lambda: MappingProxyType({}))


@dataclass(frozen=True, slots=True)
class Limits:
    max_attempts: int = 3
    max_concurrent_executions: int = 1
    agent_timeout_seconds: float = 3600.0


@dataclass(frozen=True, slots=True)
class IlmarConfig:
    state_dir: Path
    limits: Limits
    adapters: Mapping[str, AdapterConfig]


def load_config(path: Path) -> IlmarConfig:
    """Read and validate a TOML file. Relative paths resolve against its directory."""
    try:
        with path.open("rb") as handle:
            data = tomllib.load(handle)
    except FileNotFoundError:
        raise ConfigError(f"configuration file not found: {path}") from None
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{path}: invalid TOML: {exc}") from exc
    return parse_config(data, base_dir=path.resolve().parent)


def parse_config(data: Mapping[str, Any], *, base_dir: Path) -> IlmarConfig:
    """Validate already-parsed configuration data."""
    _reject_unknown(data, {"ilmar", "limits", "adapters"}, "top level")

    general = _table(data, "ilmar", "[ilmar]")
    _reject_unknown(general, {"state_dir"}, "[ilmar]")
    state_dir_raw = general.get("state_dir", ".ilmar")
    if not isinstance(state_dir_raw, str) or not state_dir_raw:
        raise ConfigError("[ilmar] state_dir must be a non-empty string")
    state_dir = Path(state_dir_raw)
    if not state_dir.is_absolute():
        state_dir = base_dir / state_dir

    return IlmarConfig(
        state_dir=state_dir,
        limits=_parse_limits(_table(data, "limits", "[limits]")),
        adapters=_parse_adapters(_table(data, "adapters", "[adapters]")),
    )


def _parse_limits(raw: Mapping[str, Any]) -> Limits:
    defaults = Limits()
    _reject_unknown(
        raw,
        {"max_attempts", "max_concurrent_executions", "agent_timeout_seconds"},
        "[limits]",
    )
    max_attempts = _positive_int(raw, "max_attempts", defaults.max_attempts)
    max_concurrent = _positive_int(
        raw, "max_concurrent_executions", defaults.max_concurrent_executions
    )
    timeout = raw.get("agent_timeout_seconds", defaults.agent_timeout_seconds)
    if isinstance(timeout, bool) or not isinstance(timeout, int | float) or timeout <= 0:
        raise ConfigError("[limits] agent_timeout_seconds must be a positive number")
    return Limits(
        max_attempts=max_attempts,
        max_concurrent_executions=max_concurrent,
        agent_timeout_seconds=float(timeout),
    )


def _parse_adapters(raw: Mapping[str, Any]) -> Mapping[str, AdapterConfig]:
    _reject_unknown(raw, set(PORT_NAMES), "[adapters]")
    missing = [port for port in PORT_NAMES if port not in raw]
    if missing:
        raise ConfigError(f"[adapters] is missing: {', '.join(missing)}")

    adapters: dict[str, AdapterConfig] = {}
    for port in PORT_NAMES:
        section = f"[adapters.{port}]"
        table = _table(raw, port, section)
        _reject_unknown(table, {"kind", "options"}, section)
        kind = table.get("kind")
        if not isinstance(kind, str) or not _KIND.match(kind):
            raise ConfigError(f"{section} kind must be a lowercase identifier like 'github'")
        options = _table(table, "options", f"{section} options")
        _reject_inline_secrets(options, f"{section} options")
        adapters[port] = AdapterConfig(kind=kind, options=MappingProxyType(dict(options)))
    return MappingProxyType(adapters)


def _reject_inline_secrets(options: Mapping[str, Any], where: str) -> None:
    for key, value in options.items():
        if isinstance(value, Mapping):
            _reject_inline_secrets(value, f"{where}.{key}")
        elif _SECRET_LIKE.search(key) and not key.endswith("_env"):
            raise ConfigError(
                f"{where}: '{key}' looks like a secret. Put the secret in an environment "
                f"variable and reference it with '{key}_env' instead."
            )


def _table(data: Mapping[str, Any], key: str, where: str) -> Mapping[str, Any]:
    value = data.get(key, {})
    if not isinstance(value, Mapping):
        raise ConfigError(f"{where} must be a table")
    return value


def _reject_unknown(data: Mapping[str, Any], allowed: set[str], where: str) -> None:
    unknown = sorted(set(data) - allowed)
    if unknown:
        raise ConfigError(f"{where}: unknown key(s): {', '.join(unknown)}")


def _positive_int(data: Mapping[str, Any], key: str, default: int) -> int:
    value = data.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ConfigError(f"[limits] {key} must be a positive integer")
    return value
