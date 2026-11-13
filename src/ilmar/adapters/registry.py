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
import inspect
import os
import stat
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ilmar.adapters.options import Options, Secrets
from ilmar.config import PORT_NAMES, IlmarConfig
from ilmar.core.components import AgentSandbox, Components, Target, close_all
from ilmar.core.layout import WorkspaceLayout
from ilmar.core.naming import safe_identifier
from ilmar.core.secret_scan import SecretScanner, SecretTooShortError
from ilmar.domain.changes import RepositoryRef
from ilmar.domain.errors import ConfigError, ProviderError
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
      its tasks belong to, which must then equal the target repository;
    * an ``execution_store`` plan may give ``read_only``, which opens the
      store for reading only (``ilmar events``): no secret, no migration, no
      write, and no other adapter is planned or created.
    """

    create: Callable[[Secrets], object]
    close: Callable[[Any], None] | None = None
    environment: tuple[str, ...] = ()
    target: Target | None = None
    agent_sandbox: AgentSandbox | None = None
    networks: frozenset[NetworkPolicy] = frozenset({NetworkPolicy.NONE})
    task_repository: RepositoryRef | None = None
    enters_sandbox: bool = False
    read_only: Callable[[], object] | None = None
    """True if the secrets in ``environment`` are handed into a sandbox (the
    agent's model credential, ADR-0006). They must then be distinct from every
    other adapter's secrets, by name and by value."""


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


def event_reader(
    config: IlmarConfig, *, registry: Registry = DEFAULT_REGISTRY
) -> Callable[[], object]:
    """How to open ``config``'s execution store read-only. Only that adapter's options
    are planned, so reading events needs nothing else from the host (no container
    runtime, no agent, no secret). Raises ``ConfigError`` for a store kind that
    cannot be read this way."""
    context = _context(config)
    port = "execution_store"
    adapter = config.adapters[port]
    where = f"[adapters.{port}] options"
    options = Options(adapter.options, where)
    try:
        store_plan = registry.parser(port, adapter.kind)(options, context)
    except ValueError as error:
        if isinstance(error, ConfigError):
            raise
        raise ConfigError(f"{where}: {error}") from None
    options.finish()
    if not isinstance(store_plan, Plan) or store_plan.read_only is None:
        raise ConfigError(
            f"[adapters.{port}] kind '{adapter.kind}' keeps no event log that can be read here"
        )
    return store_plan.read_only


def store_writer(
    config: IlmarConfig, *, registry: Registry = DEFAULT_REGISTRY
) -> tuple[WorkspaceLayout, Callable[[], object]]:
    """The layout, and how to open ``config``'s execution store for writing, with
    nothing else planned or built: no tracker, no forge, no container runtime and
    no secret (``ilmar suspend`` and ``ilmar resume``). Raises ``ConfigError``."""
    context = _context(config)
    port = "execution_store"
    adapter = config.adapters[port]
    where = f"[adapters.{port}] options"
    options = Options(adapter.options, where)
    try:
        store_plan = registry.parser(port, adapter.kind)(options, context)
    except ValueError as error:
        if isinstance(error, ConfigError):
            raise
        raise ConfigError(f"{where}: {error}") from None
    options.finish()
    if not isinstance(store_plan, Plan) or store_plan.environment:
        raise ConfigError(f"[adapters.{port}] kind '{adapter.kind}' cannot be opened here")
    return context.layout, lambda: store_plan.create(Secrets({}))


def build(
    config: IlmarConfig,
    *,
    environ: Mapping[str, str] | None = None,
    registry: Registry = DEFAULT_REGISTRY,
) -> Components:
    """Create every configured adapter. The caller owns the result and must close it."""
    blueprint = plan(config, registry=registry)
    environment = os.environ if environ is None else environ
    _check_sandbox_secrets_by_value(blueprint.plans, environment)
    scanner = _secret_scanner(blueprint.plans, environment)
    secrets = Secrets(environment)
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
    provider = blueprint.target.repository.provider
    if created["source_control"].name != provider:  # type: ignore[attr-defined]
        close_all(list(reversed(closers)))
        raise ConfigError(
            "[adapters.source_control] the adapter's name does not match the provider "
            f"'{provider}' of its target repository"
        )
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
        secret_scanner=scanner,
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
    if config.source is not None:
        _check_trusted_file(config.source)
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
    _check_sandbox_secrets_by_name(plans)
    tasks = plans["task_provider"].task_repository
    if tasks is not None and tasks != target.repository:
        raise ConfigError(
            "[adapters.task_provider] and [adapters.source_control] name different "
            "repositories; tasks and changes must belong to the same repository"
        )
    return target, sandbox


def _check_sandbox_secrets_by_name(plans: Mapping[str, Plan]) -> None:
    inside = {name for p in plans.values() if p.enters_sandbox for name in p.environment}
    outside = {name for p in plans.values() if not p.enters_sandbox for name in p.environment}
    shared = sorted(inside & outside)
    if shared:
        raise ConfigError(
            f"{', '.join(shared)} would be handed into the agent sandbox and is also a "
            "task-tracker or source-control credential (ADR-0006); use separate variables"
        )


def _secret_scanner(plans: Mapping[str, Plan], environ: Mapping[str, str]) -> SecretScanner:
    """Every configured secret, the model credential included, is looked for in
    what Ilmar publishes (#110, ADR-0038)."""
    values = {
        name: environ[name] for p in plans.values() for name in p.environment if environ.get(name)
    }
    try:
        return SecretScanner(values)
    except SecretTooShortError as error:
        raise ConfigError(f"{error}; use a real credential") from None


def _check_sandbox_secrets_by_value(plans: Mapping[str, Plan], environ: Mapping[str, str]) -> None:
    def values(enters: bool) -> dict[str, str]:
        return {
            name: environ[name]
            for p in plans.values()
            if p.enters_sandbox is enters
            for name in p.environment
            if environ.get(name)
        }

    outside = values(False)
    for name, value in values(True).items():
        same = sorted(other for other, secret in outside.items() if secret == value)
        if same:
            raise ConfigError(
                f"{name} holds the same value as {', '.join(same)}: a task-tracker or "
                "source-control credential must never enter the agent sandbox (ADR-0006)"
            )


def _create(port: str, kind: str, step: Plan, secrets: Secrets) -> object:
    try:
        return step.create(secrets)
    except ConfigError as error:
        raise ConfigError(secrets.redact(str(error))) from None
    except Exception as error:  # any adapter error may quote a secret
        # Adapter errors may quote what they were given; a secret never survives,
        # and nothing chained to this error carries the original text.
        detail = secrets.redact(str(error)) or "no detail"
        raise ConfigError(
            f"[adapters.{port}] kind '{kind}' could not be created "
            f"({type(error).__name__}): {detail}"
        ) from None


def _closer(close: Callable[[Any], None], adapter: object, secrets: Secrets) -> Callable[[], None]:
    def run() -> None:
        try:
            close(adapter)
        except Exception as error:
            raise ProviderError(secrets.redact(f"{type(error).__name__}: {error}")) from None

    return run


def _check_port(port: str, kind: str, adapter: object) -> None:
    """Fail closed unless ``adapter`` can be used as the port's protocol.

    Beyond the member names: properties must be readable (``name`` a non-empty
    string), methods callable, and every parameter the port passes accepted
    under the same name and kind, with no extra required parameter.
    """
    protocol = _PORTS[port]
    problems: list[str] = []
    for member, declared in vars(protocol).items():
        if member.startswith("_"):
            continue
        if isinstance(declared, property):
            try:
                value = getattr(adapter, member)
            except Exception:  # reported as a problem
                problems.append(f"{member} is not readable")
                continue
            if member == "name" and (not isinstance(value, str) or not value):
                problems.append("name is not a non-empty string")
            continue
        if not callable(declared):
            continue
        actual = getattr(adapter, member, None)
        if actual is None:
            problems.append(f"{member} is missing")
        elif not callable(actual):
            problems.append(f"{member} is not callable")
        else:
            problem = _signature_problem(member, declared, actual)
            if problem:
                problems.append(problem)
    if problems:
        raise ConfigError(
            f"[adapters.{port}] kind '{kind}' built something that is not a "
            f"{protocol.__name__}: {'; '.join(sorted(problems))}"
        )


def _signature_problem(member: str, declared: Callable[..., Any], actual: Any) -> str | None:
    try:
        expected = list(inspect.signature(declared).parameters.values())[1:]  # without self
        offered = inspect.signature(actual).parameters
    except (TypeError, ValueError):
        return f"{member} has no inspectable signature"
    takes_any_keyword = any(p.kind is p.VAR_KEYWORD for p in offered.values())
    positional = (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD)
    for parameter in expected:
        other = offered.get(parameter.name)
        if other is None:
            if takes_any_keyword and parameter.kind is parameter.KEYWORD_ONLY:
                continue
            return f"{member} does not accept {parameter.name}"
        if parameter.kind is parameter.KEYWORD_ONLY and other.kind not in (
            other.KEYWORD_ONLY,
            other.POSITIONAL_OR_KEYWORD,
        ):
            return f"{member} must accept {parameter.name} by keyword"
        if parameter.kind in positional and other.kind not in positional:
            return f"{member} must accept {parameter.name} positionally"
    names = {p.name for p in expected}
    extra = [
        p.name
        for p in offered.values()
        if p.default is p.empty
        and p.kind not in (p.VAR_POSITIONAL, p.VAR_KEYWORD)
        and p.name not in names
    ]
    if extra:
        return f"{member} requires {', '.join(extra)}, which the port never passes"
    return None


def _check_trusted_file(path: Path) -> None:
    """The verification policy is only as trustworthy as whoever can write the file."""
    try:
        info = path.stat()
    except FileNotFoundError:
        return  # parsed from data; nothing on disk to trust or distrust
    if info.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
        raise ConfigError(
            "the configuration file must not be writable by group or others: it defines "
            "the verification policy (ADR-0018, ADR-0019)"
        )
    if info.st_uid not in {os.geteuid(), 0}:
        raise ConfigError("the configuration file must be owned by this user or root")


def _inside(path: Path, root: Path) -> bool:
    resolved, base = path.resolve(), root.resolve()
    return resolved == base or resolved.is_relative_to(base)
