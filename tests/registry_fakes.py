# SPDX-License-Identifier: Apache-2.0
"""Fake adapter parsers for registry lifecycle tests (ADR-0019).

Each parser builds a fake from ``tests/fakes.py`` and records what happens in
``EVENTS``. Options steer the failure being tested:

* ``fail = true``: ``create`` raises, quoting the secret if one was read;
* ``close_fails = true``: closing raises, quoting the secret;
* ``secret_env``: the variable whose value ``create`` reads;
* ``not_a_port = true``: ``create`` returns something that is not an adapter.
"""

from __future__ import annotations

from typing import Any

from ilmar.adapters.memory import InMemoryExecutionStore
from ilmar.adapters.options import Options, Secrets
from ilmar.adapters.registry import BuildContext, Plan
from ilmar.core.components import AgentSandbox, Target
from ilmar.domain.changes import RepositoryRef
from ilmar.domain.workspace import NetworkPolicy

from fakes import (
    FakeAgentRunner,
    FakeContainerRuntime,
    FakeSourceControl,
    FakeTaskProvider,
)

EVENTS: list[str] = []

ENTRIES = {
    ("task_provider", "fake"): "registry_fakes:task_provider",
    ("source_control", "fake"): "registry_fakes:source_control",
    ("container_runtime", "fake"): "registry_fakes:container_runtime",
    ("agent_runner", "fake"): "registry_fakes:agent_runner",
    ("execution_store", "fake"): "registry_fakes:execution_store",
}


def _plan(options: Options, port: str, make: Any, **facts: Any) -> Plan:
    fail = options.boolean("fail", False)
    close_fails = options.boolean("close_fails", False)
    not_a_port = options.boolean("not_a_port", False)
    secret_env = options.optional_string("secret_env")

    seen: dict[str, str] = {}

    def create(secrets: Secrets) -> object:
        secret = secrets.get(secret_env, where=options.where) if secret_env else ""
        seen["secret"] = secret
        if fail:
            EVENTS.append(f"fail {port}")
            raise ValueError(f"{port} rejected credential {secret}")
        EVENTS.append(f"create {port}")
        return object() if not_a_port else make()

    def close(adapter: object) -> None:
        EVENTS.append(f"close {port}")
        if close_fails:
            raise OSError(f"{port} could not close (credential {seen.get('secret', '')})")

    return Plan(
        create,
        close=close,
        environment=(secret_env,) if secret_env else (),
        **facts,
    )


def task_provider(options: Options, context: BuildContext) -> Plan:
    return _plan(options, "task_provider", FakeTaskProvider)


def source_control(options: Options, context: BuildContext) -> Plan:
    target = Target(RepositoryRef("fake", "owner/repo"), "main")
    return _plan(options, "source_control", FakeSourceControl, target=target)


def container_runtime(options: Options, context: BuildContext) -> Plan:
    networks = frozenset({NetworkPolicy.NONE, NetworkPolicy.ALLOWLIST})
    return _plan(options, "container_runtime", FakeContainerRuntime, networks=networks)


def agent_runner(options: Options, context: BuildContext) -> Plan:
    sandbox = AgentSandbox("localhost/agent:1", NetworkPolicy.ALLOWLIST, ("model.example",))
    return _plan(options, "agent_runner", FakeAgentRunner, agent_sandbox=sandbox)


def execution_store(options: Options, context: BuildContext) -> Plan:
    return _plan(options, "execution_store", InMemoryExecutionStore)


def no_plan(options: Options, context: BuildContext) -> object:
    return "not a plan"


NOT_CALLABLE = 42
