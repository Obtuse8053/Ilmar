# SPDX-License-Identifier: Apache-2.0
"""The adapter registry: builds ``Components`` from ``ilmar.toml`` (ADR-0019).

Each ``(port, kind)`` maps to a *parser* named as ``"module:function"``. The
module is imported only when that kind is configured, so the registry itself
imports no adapter.

Building has two phases:

1. :func:`plan` parses every adapter's options strictly and checks the rules
   between ports. It reads no environment variable, touches no file and
   starts nothing. ``ilmar config validate`` runs this phase.
2. :func:`build` runs :func:`plan`, then creates the adapters in port order.
   Secrets are read from the environment only now. If any step fails,
   everything already created is closed, in reverse order, before the error
   propagates, and the error text never contains a secret.

The verification policy comes only from the configuration file
(``[verification]``), which must not live where agents or candidates write.
"""

from __future__ import annotations

import importlib
import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ilmar.adapters.options import Options, Secrets
from ilmar.config import PORT_NAMES, IlmarConfig
from ilmar.core.components import AgentSandbox, Components, Target, close_all
from ilmar.core.layout import WorkspaceLayout
from ilmar.core.naming import safe_identifier
from ilmar.domain.changes import RepositoryRef
from ilmar.domain.errors import ConfigError, IlmarError, ProviderError
from ilmar.domain.verification import VerificationPolicy
from ilmar.domain.workspace import NetworkPolicy
from ilmar.ports.agent_runner import AgentRunner
from ilmar.ports.container_runtime import ContainerRuntime
from ilmar.ports.execution_store import ExecutionStore
from ilmar.ports.source_control import SourceControlProvider
from ilmar.ports.task_provider import TaskProvider


@dataclass(frozen=True, slots=True)
class BuildContext:
    """What every parser may use besides its own options."""

    instance_id: str
    layout: WorkspaceLayout
    state_dir: Path
    base_dir: Path
    verification: VerificationPolicy


@dataclass(frozen=True, slots=True)
class Plan:
    """A parsed, validated adapter that has not been created yet.

    ``create`` builds the adapter, reading secrets through ``Secrets`` only.
    ``close`` releases what ``create`` returned, if it holds resources.
    Port-specific facts the registry checks across ports:

    * a ``source_control`` plan must give ``target``;
    * an ``agent_runner`` plan must give ``agent_sandbox``;
    * a ``container_runtime`` plan lists the network policies it can
      enforce in ``networks``;
    * a ``task_provider`` plan may give ``task_repository``, the repository
      its tasks belong to, which must then equal the target repository.
    """

    create: Callable[[Secrets], object]
    close: Callable[[Any], None] | None = None
    environment: tuple[str, ...] = ()
    target: Target | None = None
    agent_sandbox: AgentSandbox | None = None
    networks: frozenset[NetworkPolicy] = frozenset({NetworkPolicy.NONE})
    task_repository: RepositoryRef | None = None


Parser = Callable[[Options, BuildContext], Plan]

_PORTS: Mapping[str, type] = {
    "task_provider": TaskProvider,
    "source_control": SourceControlProvider,
    "container_runtime": ContainerRuntime,
    "agent_runner": AgentRunner,
    "execution_store": ExecutionStore,
}


class Registry:
    """Maps ``(port, kind)`` to a parser reference ``"module:function"``."""

    def __init__(self, entries: Mapping[tuple[str, str], str]) -> None:
        for port, kind in entries:
            if port not in _PORTS:
                raise ValueError(f"unknown port: {port}")
            if not kind:
                raise ValueError("kind must not be empty")
        self._entries = dict(entries)

    def kinds(self, port: str) -> tuple[str, ...]:
        return tuple(sorted(kind for p, kind in self._entries if p == port))

    def extended(self, entries: Mapping[tuple[str, str], str]) -> Registry:
        """A new registry with more (or replaced) entries; this one is unchanged."""
        return Registry({**self._entries, **entries})

    def parser(self, port: str, kind: str) -> Parser:
        reference = self._entries.get((port, kind))
        if reference is None:
            known = ", ".join(self.kinds(port)) or "none"
            raise ConfigError(f"[adapters.{port}] kind '{kind}' is not known (known: {known})")
        module_name, _, attribute = reference.partition(":")
        try:
            module = importlib.import_module(module_name)
        except ImportError as error:
            raise ConfigError(
                f"[adapters.{port}] kind '{kind}' is not installed (cannot import {error.name})"
            ) from None
        parser = getattr(module, attribute, None)
        if not callable(parser):
            raise ConfigError(f"[adapters.{port}] kind '{kind}': {reference} is not a parser")
        return parser  # type: ignore[no-any-return]


DEFAULT_REGISTRY = Registry(
    {
        ("task_provider", "github"): "ilmar.adapters.builtin:github_tasks",
        ("source_control", "github"): "ilmar.adapters.builtin:github_source_control",
        ("container_runtime", "podman"): "ilmar.adapters.builtin:podman_runtime",
        ("agent_runner", "claude-code"): "ilmar.adapters.builtin:claude_code_runner",
        ("execution_store", "sqlite"): "ilmar.adapters.builtin:sqlite_store",
    }
)


@dataclass(frozen=True, slots=True)
class Blueprint:
    """The result of :func:`plan`: everything validated, nothing created."""

    context: BuildContext
    kinds: Mapping[str, str]
    plans: Mapping[str, Plan] = field(repr=False)
    target: Target
    agent_sandbox: AgentSandbox

    @property
    def environment(self) -> tuple[str, ...]:
        """Every environment variable the adapters will read, by name."""
        return tuple(sorted({name for p in self.plans.values() for name in p.environment}))


def plan(config: IlmarConfig, *, registry: Registry = DEFAULT_REGISTRY) -> Blueprint:
    """Validate ``config`` completely without reading secrets or creating anything."""
    context = _context(config)
    plans: dict[str, Plan] = {}
    for port in PORT_NAMES:
        adapter = config.adapters[port]
        where = f"[adapters.{port}] options"
        parser = registry.parser(port, adapter.kind)
        options = Options(adapter.options, where)
        try:
            plans[port] = parser(options, context)
        except ConfigError:
            raise
        except ValueError as error:
            raise ConfigError(f"{where}: {error}") from None
        options.finish()
        if not isinstance(plans[port], Plan):
            raise ConfigError(f"[adapters.{port}] kind '{adapter.kind}' returned no plan")
    target, sandbox = _check_combinations(config, plans)
    kinds = {port: config.adapters[port].kind for port in PORT_NAMES}
    return Blueprint(context, kinds, plans, target, sandbox)


def build(
    config: IlmarConfig,
    *,
    environ: Mapping[str, str] | None = None,
    registry: Registry = DEFAULT_REGISTRY,
) -> Components:
    """Create every configured adapter. The caller owns the result and must close it."""
    blueprint = plan(config, registry=registry)
    secrets = Secrets(os.environ if environ is None else environ)
    created: dict[str, object] = {}
    closers: list[Callable[[], None]] = []
    try:
        for port in PORT_NAMES:
            step = blueprint.plans[port]
            adapter = _create(port, blueprint.kinds[port], step, secrets)
            if step.close is not None:
                closers.append(_closer(step.close, adapter, secrets))
            created[port] = adapter
            _check_port(port, blueprint.kinds[port], adapter)
    except BaseException as error:
        try:
            close_all(list(reversed(closers)))
        except ProviderError as cleanup:
            error.add_note(secrets.redact(f"cleanup after this failure also failed: {cleanup}"))
            for note in getattr(cleanup, "__notes__", ()):
                error.add_note(secrets.redact(note))
        raise
    context = blueprint.context
    return Components(
        instance_id=context.instance_id,
        layout=context.layout,
        task_provider=created["task_provider"],  # type: ignore[arg-type]
        source_control=created["source_control"],  # type: ignore[arg-type]
        container_runtime=created["container_runtime"],  # type: ignore[arg-type]
        agent_runner=created["agent_runner"],  # type: ignore[arg-type]
        execution_store=created["execution_store"],  # type: ignore[arg-type]
        agent_sandbox=blueprint.agent_sandbox,
        target=blueprint.target,
        verification=context.verification,
        closers=closers,
    )


# -- internals -------------------------------------------------------------------------


def _context(config: IlmarConfig) -> BuildContext:
    if config.verification is None:
        raise ConfigError(
            "[verification] is required: without it nothing can be verified or published"
        )
    try:
        instance_id = safe_identifier(config.instance_id, "[ilmar] instance_id")
    except ValueError as error:
        raise ConfigError(str(error)) from None
    try:
        layout = WorkspaceLayout.under(config.state_dir)
    except ValueError as error:
        raise ConfigError(f"[ilmar] state_dir: {error}") from None
    if config.source is not None and _inside(config.source, layout.workspaces_root):
        raise ConfigError(
            "the configuration file must not live under the workspaces directory, where "
            "agents and verification checkouts write (ADR-0019)"
        )
    base_dir = config.base_dir or config.state_dir.parent
    return BuildContext(instance_id, layout, config.state_dir, base_dir, config.verification)


def _check_combinations(
    config: IlmarConfig, plans: Mapping[str, Plan]
) -> tuple[Target, AgentSandbox]:
    kinds = {port: config.adapters[port].kind for port in PORT_NAMES}
    target = plans["source_control"].target
    if target is None:
        raise ConfigError(
            f"[adapters.source_control] kind '{kinds['source_control']}' gives no target repository"
        )
    sandbox = plans["agent_runner"].agent_sandbox
    if sandbox is None:
        raise ConfigError(
            f"[adapters.agent_runner] kind '{kinds['agent_runner']}' gives no agent sandbox"
        )
    networks = plans["container_runtime"].networks
    runtime = f"[adapters.container_runtime] kind '{kinds['container_runtime']}'"
    if NetworkPolicy.NONE not in networks:
        raise ConfigError(f"{runtime} cannot run sandboxes without network (verification needs it)")
    if sandbox.network not in networks:
        raise ConfigError(
            f"{runtime} cannot enforce the {sandbox.network.value} network the agent "
            f"'{kinds['agent_runner']}' needs (for podman: set proxy_image)"
        )
    tasks = plans["task_provider"].task_repository
    if tasks is not None and tasks != target.repository:
        raise ConfigError(
            "[adapters.task_provider] and [adapters.source_control] name different "
            "repositories; tasks and changes must belong to the same repository"
        )
    return target, sandbox


def _create(port: str, kind: str, step: Plan, secrets: Secrets) -> object:
    try:
        return step.create(secrets)
    except ConfigError as error:
        raise ConfigError(secrets.redact(str(error))) from None
    except (ValueError, OSError, IlmarError) as error:
        # Adapter errors may quote what they were given; a secret never survives.
        detail = secrets.redact(str(error)) or type(error).__name__
        raise ConfigError(
            f"[adapters.{port}] kind '{kind}' could not be created: {detail}"
        ) from None


def _closer(close: Callable[[Any], None], adapter: object, secrets: Secrets) -> Callable[[], None]:
    def run() -> None:
        try:
            close(adapter)
        except Exception as error:
            raise ProviderError(secrets.redact(f"{type(error).__name__}: {error}")) from None

    return run


def _check_port(port: str, kind: str, adapter: object) -> None:
    protocol = _PORTS[port]
    missing = [
        name for name in vars(protocol) if not name.startswith("_") and not hasattr(adapter, name)
    ]
    if missing:
        raise ConfigError(
            f"[adapters.{port}] kind '{kind}' built something that is not a "
            f"{protocol.__name__} (missing: {', '.join(sorted(missing))})"
        )


def _inside(path: Path, root: Path) -> bool:
    resolved, base = path.resolve(), root.resolve()
    return resolved == base or resolved.is_relative_to(base)
