# SPDX-License-Identifier: Apache-2.0
"""Loading and validating ``ilmar.toml``.

The file has these sections::

    [ilmar]                      # general settings: state_dir, instance_id
    [limits]                     # execution limits
    [adapters.<port>]            # one table per port: kind + options
    [verification]               # the verification policy (ADR-0010, ADR-0018)
    [[verification.commands]]    # one per check: name, argv, timeout, required, job

The verification policy lives here, never in the repository being changed.
Its path classes can only be *extended* (``extra_*``); the built-in protected,
check-influencing and CI-configuration paths always apply.

This module validates structure. Adapter kinds and their options are
validated by the adapter registry (``ilmar.adapters.registry``, ADR-0019),
which ``ilmar config validate`` also runs. Option keys that look like
secrets are rejected unless they end in ``_env``, which means "the name of an
environment variable" (ADR-0007).
"""

from __future__ import annotations

import math
import re
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Any

from ilmar.domain.errors import ConfigError
from ilmar.domain.verification import (
    DEFAULT_CHECK_INFLUENCING_PATHS,
    DEFAULT_CI_CONFIG_PATHS,
    DEFAULT_JOB,
    DEFAULT_PROTECTED_PATHS,
    VerificationCommand,
    VerificationPolicy,
)
from ilmar.domain.workspace import ResourceLimits

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
DEFAULT_INSTANCE_ID = "default"


@dataclass(frozen=True, slots=True)
class AdapterConfig:
    kind: str
    options: Mapping[str, Any] = field(default_factory=lambda: MappingProxyType({}))


WALL_MARGIN_SECONDS = 600.0
"""Default ``execution_wall_seconds`` beyond ``agent_timeout_seconds``; equal to
``ilmar.core.lifetimes.DEFAULT_WALL_MARGIN_SECONDS`` (checked by a test)."""


@dataclass(frozen=True, slots=True)
class Limits:
    max_attempts: int = 3
    max_concurrent_executions: int = 1
    agent_timeout_seconds: float = 3600.0
    retry_backoff_base_seconds: float = 300.0
    """Wait after the first failed attempt; doubled per attempt (#13, ADR-0028)."""
    retry_backoff_max_seconds: float = 21600.0
    """The longest wait between attempts."""
    stall_timeout_seconds: float = 900.0
    """End an agent run with no progress for this long (ADR-0035 §3); at most
    ``agent_timeout_seconds``. A single long tool step may legitimately be silent."""
    execution_wall_seconds: float = 3600.0 + 600.0
    """The agent sandbox's hard lifetime, enforced by the container runtime even
    if Ilmar is killed (ADR-0029 §1, #90); at least ``agent_timeout_seconds``.
    Unset, it is ``agent_timeout_seconds`` plus 600."""


@dataclass(frozen=True, slots=True)
class IlmarConfig:
    state_dir: Path
    limits: Limits
    adapters: Mapping[str, AdapterConfig]
    verification: VerificationPolicy | None = None
    """``None`` when the file has no ``[verification]``; nothing can be verified then."""
    instance_id: str = DEFAULT_INSTANCE_ID
    base_dir: Path | None = None
    """The directory relative option paths resolve against (the file's directory)."""
    source: Path | None = None
    """The file this configuration was read from, if any."""


def load_config(path: Path) -> IlmarConfig:
    """Read and validate a TOML file. Relative paths resolve against its directory."""
    try:
        with path.open("rb") as handle:
            data = tomllib.load(handle)
    except FileNotFoundError:
        raise ConfigError(f"configuration file not found: {path}") from None
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{path}: invalid TOML: {exc}") from exc
    return parse_config(data, base_dir=path.resolve().parent, source=path.resolve())


def parse_config(
    data: Mapping[str, Any], *, base_dir: Path, source: Path | None = None
) -> IlmarConfig:
    """Validate already-parsed configuration data."""
    _reject_unknown(data, {"ilmar", "limits", "adapters", "verification"}, "top level")

    general = _table(data, "ilmar", "[ilmar]")
    _reject_unknown(general, {"state_dir", "instance_id"}, "[ilmar]")
    instance_id = general.get("instance_id", DEFAULT_INSTANCE_ID)
    if not isinstance(instance_id, str) or not instance_id:
        raise ConfigError("[ilmar] instance_id must be a non-empty string")
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
        verification=(
            _parse_verification(_table(data, "verification", "[verification]"))
            if "verification" in data
            else None
        ),
        instance_id=instance_id,
        base_dir=base_dir,
        source=source,
    )


def _parse_verification(raw: Mapping[str, Any]) -> VerificationPolicy:
    where = "[verification]"
    _reject_unknown(
        raw,
        {
            "image",
            "commands",
            "extra_protected_paths",
            "extra_check_influencing_paths",
            "extra_ci_config_paths",
            "memory_mb",
            "cpus",
            "max_processes",
        },
        where,
    )
    image = raw.get("image")
    if not isinstance(image, str) or not image:
        raise ConfigError(f"{where} image must be a non-empty string")
    commands_raw = raw.get("commands", [])
    if not isinstance(commands_raw, list) or not commands_raw:
        raise ConfigError(f"{where} needs at least one [[verification.commands]] table")
    commands = []
    for index, entry in enumerate(commands_raw):
        at = f"[[verification.commands]] #{index + 1}"
        if not isinstance(entry, Mapping):
            raise ConfigError(f"{at} must be a table")
        _reject_unknown(entry, {"name", "argv", "timeout_seconds", "required", "job"}, at)
        argv = entry.get("argv")
        if not isinstance(argv, list) or not all(isinstance(a, str) for a in argv):
            raise ConfigError(f"{at} argv must be a list of strings")
        timeout = entry.get("timeout_seconds", 600)
        required = entry.get("required", True)
        if isinstance(timeout, bool) or not isinstance(timeout, int | float):
            raise ConfigError(f"{at} timeout_seconds must be a number")
        if not isinstance(required, bool):
            raise ConfigError(f"{at} required must be true or false")
        try:
            commands.append(
                VerificationCommand(
                    str(entry.get("name", "")),
                    tuple(argv),
                    float(timeout),
                    required,
                    str(entry.get("job", DEFAULT_JOB)),
                )
            )
        except ValueError as exc:
            raise ConfigError(f"{at}: {exc}") from None
    limits: dict[str, Any] = {}
    for key in ("memory_mb", "max_processes"):
        if key in raw:
            limits[key] = _positive_int(raw, key, 1)
    if "cpus" in raw:
        cpus = raw["cpus"]
        if isinstance(cpus, bool) or not isinstance(cpus, int | float) or cpus <= 0:
            raise ConfigError(f"{where} cpus must be a positive number")
        limits["cpus"] = float(cpus)
    try:
        return VerificationPolicy(
            image=image,
            commands=tuple(commands),
            protected_paths=DEFAULT_PROTECTED_PATHS + _patterns(raw, "extra_protected_paths"),
            check_influencing_paths=DEFAULT_CHECK_INFLUENCING_PATHS
            + _patterns(raw, "extra_check_influencing_paths"),
            ci_config_paths=DEFAULT_CI_CONFIG_PATHS + _patterns(raw, "extra_ci_config_paths"),
            limits=ResourceLimits(**limits),
        )
    except ValueError as exc:
        raise ConfigError(f"{where}: {exc}") from None


def _patterns(raw: Mapping[str, Any], key: str) -> tuple[str, ...]:
    value = raw.get(key, [])
    if not isinstance(value, list) or not all(isinstance(v, str) and v for v in value):
        raise ConfigError(f"[verification] {key} must be a list of non-empty strings")
    return tuple(value)


def _parse_limits(raw: Mapping[str, Any]) -> Limits:
    defaults = Limits()
    _reject_unknown(
        raw,
        {
            "max_attempts",
            "max_concurrent_executions",
            "agent_timeout_seconds",
            "retry_backoff_base_seconds",
            "retry_backoff_max_seconds",
            "stall_timeout_seconds",
            "execution_wall_seconds",
        },
        "[limits]",
    )
    max_attempts = _positive_int(raw, "max_attempts", defaults.max_attempts)
    max_concurrent = _positive_int(
        raw, "max_concurrent_executions", defaults.max_concurrent_executions
    )
    timeout = raw.get("agent_timeout_seconds", defaults.agent_timeout_seconds)
    if isinstance(timeout, bool) or not isinstance(timeout, int | float) or timeout <= 0:
        raise ConfigError("[limits] agent_timeout_seconds must be a positive number")
    base = _positive_number(raw, "retry_backoff_base_seconds", defaults.retry_backoff_base_seconds)
    cap = _positive_number(raw, "retry_backoff_max_seconds", defaults.retry_backoff_max_seconds)
    if cap < base:
        raise ConfigError(
            "[limits] retry_backoff_max_seconds must not be less than retry_backoff_base_seconds"
        )
    stall = _positive_number(raw, "stall_timeout_seconds", defaults.stall_timeout_seconds)
    if stall > float(timeout):
        raise ConfigError(
            "[limits] stall_timeout_seconds must not be greater than agent_timeout_seconds"
        )
    wall = _positive_number(raw, "execution_wall_seconds", float(timeout) + WALL_MARGIN_SECONDS)
    if wall < float(timeout):
        raise ConfigError(
            "[limits] execution_wall_seconds must not be less than agent_timeout_seconds"
        )
    return Limits(
        max_attempts=max_attempts,
        max_concurrent_executions=max_concurrent,
        agent_timeout_seconds=float(timeout),
        retry_backoff_base_seconds=base,
        retry_backoff_max_seconds=cap,
        stall_timeout_seconds=stall,
        execution_wall_seconds=wall,
    )


def _positive_number(raw: Mapping[str, Any], key: str, default: float) -> float:
    value = raw.get(key, default)
    if (
        isinstance(value, bool)
        or not isinstance(value, int | float)
        or not math.isfinite(value)
        or value <= 0
    ):
        raise ConfigError(f"[limits] {key} must be a positive, finite number")
    return float(value)


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
        raise ConfigError(f"{key} must be a positive integer")
    return value
