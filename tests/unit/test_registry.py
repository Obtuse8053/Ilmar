# SPDX-License-Identifier: Apache-2.0
"""The adapter registry (#10, ADR-0019): validation, construction and lifecycle."""

from __future__ import annotations

import copy
import os
import stat
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

import registry_fakes
from ilmar.adapters.builtin import ANTHROPIC_API_HOST
from ilmar.adapters.claude_code.runner import ClaudeCodeRunner
from ilmar.adapters.github.source_control import GitHubSourceControl
from ilmar.adapters.github.tasks import GitHubTaskProvider
from ilmar.adapters.podman.runtime import PodmanRuntime
from ilmar.adapters.registry import DEFAULT_REGISTRY, Registry, build, plan
from ilmar.adapters.sqlite import SQLiteExecutionStore
from ilmar.config import IlmarConfig, parse_config
from ilmar.core.components import AgentSandbox, Components
from ilmar.domain.changes import RepositoryRef
from ilmar.domain.errors import ConfigError, ProviderError
from ilmar.domain.workspace import NetworkPolicy

GITHUB_SECRET = "ghp_THIS-MUST-NEVER-BE-PRINTED"
MODEL_SECRET = "sk-ant-THIS-MUST-NEVER-BE-PRINTED"
ENVIRON = {"GITHUB_TOKEN": GITHUB_SECRET, "ANTHROPIC_API_KEY": MODEL_SECRET}
SECRETS = (GITHUB_SECRET, MODEL_SECRET)


@pytest.fixture
def tools(tmp_path: Path) -> dict[str, str]:
    """Stand-ins for the podman and git executables (never run by these tests)."""
    found = {}
    for name in ("podman", "git"):
        path = tmp_path / "bin" / name
        path.parent.mkdir(exist_ok=True)
        path.write_text("#!/bin/sh\nexit 99\n")
        path.chmod(path.stat().st_mode | stat.S_IXUSR)
        found[name] = str(path)
    return found


def data(tools: dict[str, str] | None = None) -> dict[str, Any]:
    tools = tools or {"podman": "podman", "git": "git"}
    return {
        "ilmar": {"state_dir": "state", "instance_id": "test"},
        "adapters": {
            "task_provider": {
                "kind": "github",
                "options": {
                    "repository": "owner/repo",
                    "human_claim_labels": ["status:in-progress"],
                    "token_env": "GITHUB_TOKEN",
                },
            },
            "source_control": {
                "kind": "github",
                "options": {
                    "repository": "owner/repo",
                    "token_env": "GITHUB_TOKEN",
                    "git": tools["git"],
                },
            },
            "container_runtime": {
                "kind": "podman",
                "options": {"podman": tools["podman"], "proxy_image": "localhost/proxy:1"},
            },
            "agent_runner": {
                "kind": "claude-code",
                "options": {"image": "localhost/agent:1", "api_key_env": "ANTHROPIC_API_KEY"},
            },
            "execution_store": {"kind": "sqlite", "options": {}},
        },
        "verification": {
            "image": "localhost/verify:1",
            "commands": [{"name": "tests", "argv": ["pytest"]}],
        },
    }


def config(raw: dict[str, Any], base: Path, *, source: Path | None = None) -> IlmarConfig:
    return parse_config(raw, base_dir=base, source=source or base / "ilmar.toml")


def options(raw: dict[str, Any], port: str) -> dict[str, Any]:
    result: dict[str, Any] = raw["adapters"][port].setdefault("options", {})
    return result


def plan_error(raw: dict[str, Any], base: Path, registry: Registry = DEFAULT_REGISTRY) -> str:
    with pytest.raises(ConfigError) as info:
        plan(config(raw, base), registry=registry)
    return str(info.value)


@pytest.fixture
def built(tmp_path: Path, tools: dict[str, str]) -> Iterator[Components]:
    with build(config(data(tools), tmp_path), environ=ENVIRON) as components:
        yield components


# -- the configured adapters -------------------------------------------------------------


def test_builds_the_configured_adapters(built: Components, tmp_path: Path) -> None:
    assert isinstance(built.task_provider, GitHubTaskProvider)
    assert isinstance(built.source_control, GitHubSourceControl)
    assert isinstance(built.container_runtime, PodmanRuntime)
    assert isinstance(built.agent_runner, ClaudeCodeRunner)
    assert isinstance(built.execution_store, SQLiteExecutionStore)
    assert built.instance_id == "test"
    assert built.layout.workspaces_root == tmp_path / "state" / "workspaces"
    assert built.target.repository == RepositoryRef("github", "owner/repo")
    assert built.target.base_branch == "main"
    assert (tmp_path / "state" / "executions.sqlite3").exists()


def test_the_agent_sandbox_reaches_only_the_model_endpoint(built: Components) -> None:
    assert built.agent_sandbox == AgentSandbox(
        "localhost/agent:1", NetworkPolicy.ALLOWLIST, (ANTHROPIC_API_HOST,)
    )


def test_a_gateway_replaces_the_model_endpoint(tmp_path: Path) -> None:
    raw = data()
    options(raw, "agent_runner")["base_url"] = "https://llm.example.org/v1"
    assert plan(config(raw, tmp_path)).agent_sandbox.allowed_hosts == ("llm.example.org",)


def test_the_verification_policy_is_exactly_the_configured_one(
    tmp_path: Path, tools: dict[str, str]
) -> None:
    cfg = config(data(tools), tmp_path)
    with build(cfg, environ=ENVIRON) as components:
        assert components.verification is cfg.verification


def test_publishing_refuses_the_ci_paths_verification_rejects(built: Components) -> None:
    ci_paths = built.source_control._ci_paths  # type: ignore[attr-defined]
    assert ".github" in ci_paths
    assert ".gitlab-ci.yml" in ci_paths
    assert "Jenkinsfile" in ci_paths


def test_runtime_options_reach_the_profile(tmp_path: Path, tools: dict[str, str]) -> None:
    raw = data(tools)
    options(raw, "container_runtime").update(memory_mb=2048, max_processes=256, tmpfs_mb=64)
    with build(config(raw, tmp_path), environ=ENVIRON) as components:
        profile = components.container_runtime._profile  # type: ignore[attr-defined]
        assert profile.default_limits.memory_mb == 2048
        assert profile.default_limits.max_processes == 256
        assert profile.default_limits.cpus == 2.0  # the default stays
        assert profile.tmpfs_mb == 64
        assert profile.uid == 1000
        assert components.container_runtime._allow_unrestricted is False  # type: ignore[attr-defined]


def test_relative_store_paths_resolve_against_the_configuration(
    tmp_path: Path, tools: dict[str, str]
) -> None:
    raw = data(tools)
    options(raw, "execution_store")["path"] = "db/ilmar.sqlite3"
    with build(config(raw, tmp_path), environ=ENVIRON):
        assert (tmp_path / "db" / "ilmar.sqlite3").exists()


def test_the_example_configuration_plans() -> None:
    root = Path(__file__).resolve().parents[2]
    from ilmar.config import load_config

    blueprint = plan(load_config(root / "ilmar.example.toml"))
    assert blueprint.environment == ("ANTHROPIC_API_KEY", "GITHUB_TOKEN")
    assert blueprint.kinds["container_runtime"] == "podman"


def test_planning_reads_no_secret_and_creates_nothing(tmp_path: Path) -> None:
    blueprint = plan(config(data(), tmp_path))  # executables need not exist yet either
    assert blueprint.environment == ("ANTHROPIC_API_KEY", "GITHUB_TOKEN")
    assert not (tmp_path / "state").exists()


# -- strict validation -------------------------------------------------------------------


def test_unknown_kind_lists_known_kinds(tmp_path: Path) -> None:
    raw = data()
    raw["adapters"]["container_runtime"]["kind"] = "docker"
    message = plan_error(raw, tmp_path)
    assert "kind 'docker' is not known" in message
    assert "podman" in message


def test_a_kind_whose_module_is_missing_is_not_installed(tmp_path: Path) -> None:
    registry = DEFAULT_REGISTRY.extended(
        {("container_runtime", "docker"): "ilmar_docker_adapter_absent:parse"}
    )
    raw = data()
    raw["adapters"]["container_runtime"] = {"kind": "docker"}
    assert "is not installed" in plan_error(raw, tmp_path, registry)


@pytest.mark.parametrize("attribute", ["missing", "NOT_CALLABLE"])
def test_a_reference_to_something_else_is_refused(tmp_path: Path, attribute: str) -> None:
    registry = DEFAULT_REGISTRY.extended(
        {("container_runtime", "odd"): f"registry_fakes:{attribute}"}
    )
    raw = data()
    raw["adapters"]["container_runtime"] = {"kind": "odd"}
    assert "is not a parser" in plan_error(raw, tmp_path, registry)


def test_a_parser_must_return_a_plan(tmp_path: Path) -> None:
    registry = DEFAULT_REGISTRY.extended({("container_runtime", "odd"): "registry_fakes:no_plan"})
    raw = data()
    raw["adapters"]["container_runtime"] = {"kind": "odd"}
    assert "returned no plan" in plan_error(raw, tmp_path, registry)


def test_registry_entries_are_checked() -> None:
    with pytest.raises(ValueError, match="unknown port"):
        Registry({("scheduler", "x"): "m:f"})


@pytest.mark.parametrize(
    ("port", "key"),
    [
        ("task_provider", "repository"),
        ("task_provider", "token_env"),
        ("source_control", "repository"),
        ("source_control", "token_env"),
        ("agent_runner", "image"),
        ("agent_runner", "api_key_env"),
    ],
)
def test_required_options(tmp_path: Path, port: str, key: str) -> None:
    raw = data()
    del options(raw, port)[key]
    assert f"[adapters.{port}] options {key}: is required" in plan_error(raw, tmp_path)


@pytest.mark.parametrize(
    "port",
    ["task_provider", "source_control", "container_runtime", "agent_runner", "execution_store"],
)
def test_unknown_options_are_rejected(tmp_path: Path, port: str) -> None:
    raw = data()
    options(raw, port)["colour"] = "blue"
    assert "unknown option(s): colour" in plan_error(raw, tmp_path)


@pytest.mark.parametrize(
    ("port", "key", "value", "expected"),
    [
        ("task_provider", "excluded_labels", "needs:maintainer", "list of non-empty strings"),
        ("task_provider", "skip_assigned", "yes", "true or false"),
        ("task_provider", "repository", "not-a-repository", "owner/name"),
        ("task_provider", "claimed_label", "ilmar:ready", "must differ"),
        ("task_provider", "claimed_label", "status:in-progress", "owned by Ilmar"),
        ("task_provider", "api_url", "http://api.github.com", "https URL"),
        ("task_provider", "api_url", "https://user:pw@api.github.com", "https URL"),
        ("task_provider", "token_env", "github token", "environment variable"),
        ("task_provider", "token_env", "$GITHUB_TOKEN", "environment variable"),
        ("source_control", "base_branch", "../main", "valid branch name"),
        ("source_control", "git_base_url", "file:///srv/git", "https URL"),
        ("source_control", "git", "-c core.sshCommand=evil", "must match"),
        ("container_runtime", "memory_mb", 0, "positive integer"),
        ("container_runtime", "cpus", -1, "positive number"),
        ("container_runtime", "proxy_image", "--privileged", "must match"),
        ("agent_runner", "max_turns", 0, "positive integer"),
        ("agent_runner", "permission_mode", "plan", "permission_mode"),
        ("agent_runner", "expected_version", "latest", "1.2.3"),
        ("agent_runner", "base_url", "http://llm.local", "https URL"),
        ("agent_runner", "image", "image with spaces", "must match"),
        ("execution_store", "path", "", "non-empty path"),
    ],
)
def test_invalid_option_values(
    tmp_path: Path, port: str, key: str, value: Any, expected: str
) -> None:
    raw = data()
    options(raw, port)[key] = value
    message = plan_error(raw, tmp_path)
    assert expected in message
    assert f"[adapters.{port}]" in message


@pytest.mark.parametrize(
    ("port", "key"),
    [
        ("container_runtime", "allow_unrestricted_network"),
        ("container_runtime", "network"),
        ("container_runtime", "privileged"),
        ("container_runtime", "uid"),
        ("container_runtime", "init"),
        ("container_runtime", "mounts"),
        ("container_runtime", "cap_add"),
        ("container_runtime", "security_opt"),
        ("container_runtime", "extra_hosts"),
        ("agent_runner", "allowed_hosts"),
        ("agent_runner", "network"),
    ],
)
def test_settings_that_weaken_the_sandbox_are_refused(tmp_path: Path, port: str, key: str) -> None:
    raw = data()
    options(raw, port)[key] = True
    assert "it would weaken the sandbox" in plan_error(raw, tmp_path)


def test_values_are_never_repeated_in_errors(tmp_path: Path) -> None:
    raw = data()
    options(raw, "task_provider")["repository"] = "SENTINEL-VALUE"
    assert "SENTINEL-VALUE" not in plan_error(raw, tmp_path)


# -- rules between ports -----------------------------------------------------------------


def test_the_agent_network_needs_a_runtime_that_enforces_it(tmp_path: Path) -> None:
    raw = data()
    del options(raw, "container_runtime")["proxy_image"]
    assert "cannot enforce the allowlist network" in plan_error(raw, tmp_path)


def test_egress_network_without_proxy_image_is_an_error(tmp_path: Path) -> None:
    raw = data()
    options(raw, "container_runtime").pop("proxy_image")
    options(raw, "container_runtime")["egress_network"] = "podman"
    assert "egress_network: needs proxy_image" in plan_error(raw, tmp_path)


def test_tasks_and_changes_must_belong_to_one_repository(tmp_path: Path) -> None:
    raw = data()
    options(raw, "source_control")["repository"] = "owner/other"
    assert "different repositories" in plan_error(raw, tmp_path)


def test_verification_is_required(tmp_path: Path) -> None:
    raw = data()
    del raw["verification"]
    assert "[verification] is required" in plan_error(raw, tmp_path)


@pytest.mark.parametrize("instance_id", ["../x", "a.b", "-x", "x" * 65])
def test_instance_id_must_be_safe(tmp_path: Path, instance_id: str) -> None:
    raw = data()
    raw["ilmar"]["instance_id"] = instance_id
    assert "instance_id" in plan_error(raw, tmp_path)


def test_the_store_never_lives_where_agents_write(tmp_path: Path) -> None:
    raw = data()
    options(raw, "execution_store")["path"] = "state/workspaces/e1/1/agent/db.sqlite3"
    assert "must not be under the workspaces directory" in plan_error(raw, tmp_path)


def test_configuration_inside_a_workspace_is_refused(tmp_path: Path) -> None:
    """The policy must not come from a file an agent or a candidate could have written."""
    workspace = tmp_path / "state" / "workspaces" / "e1" / "1" / "agent"
    raw = data()
    raw["ilmar"]["state_dir"] = str(tmp_path / "state")
    cfg = config(raw, workspace, source=workspace / "ilmar.toml")
    with pytest.raises(ConfigError, match="must not live under the workspaces directory"):
        plan(cfg)


# -- creation, secrets and lifecycle -----------------------------------------------------


def test_a_missing_secret_names_the_variable_only(tmp_path: Path, tools: dict[str, str]) -> None:
    with pytest.raises(ConfigError) as info:
        build(config(data(tools), tmp_path), environ={"GITHUB_TOKEN": GITHUB_SECRET})
    message = str(info.value)
    assert "ANTHROPIC_API_KEY is not set" in message
    assert GITHUB_SECRET not in message


def test_a_missing_executable_fails_closed_and_closes_nothing_twice(
    tmp_path: Path, tools: dict[str, str]
) -> None:
    raw = data(tools)
    options(raw, "container_runtime")["podman"] = str(tmp_path / "no-such-podman")
    with pytest.raises(ConfigError, match="not found"):
        build(config(raw, tmp_path), environ=ENVIRON)
    assert not (tmp_path / "state" / "executions.sqlite3").exists()  # store comes later


def test_the_sqlite_store_is_closed_with_the_components(
    tmp_path: Path, tools: dict[str, str]
) -> None:
    components = build(config(data(tools), tmp_path), environ=ENVIRON)
    store = components.execution_store
    components.close()
    components.close()  # idempotent
    with pytest.raises(Exception, match="closed"):
        store.get("e1")


@pytest.fixture
def fakes_registry() -> Iterator[Registry]:
    registry_fakes.EVENTS.clear()
    yield DEFAULT_REGISTRY.extended(registry_fakes.ENTRIES)
    registry_fakes.EVENTS.clear()


def fake_data(**per_port: dict[str, Any]) -> dict[str, Any]:
    raw = copy.deepcopy(data())
    for port in raw["adapters"]:
        raw["adapters"][port] = {"kind": "fake", "options": per_port.get(port, {})}
    return raw


def test_a_failure_closes_what_was_created_in_reverse_order(
    tmp_path: Path, fakes_registry: Registry
) -> None:
    raw = fake_data(agent_runner={"fail": True})
    with pytest.raises(ConfigError, match="agent_runner"):
        build(config(raw, tmp_path), environ={}, registry=fakes_registry)
    assert registry_fakes.EVENTS == [
        "create task_provider",
        "create source_control",
        "create container_runtime",
        "fail agent_runner",
        "close container_runtime",
        "close source_control",
        "close task_provider",
    ]


def test_a_failing_cleanup_keeps_the_original_error_and_closes_the_rest(
    tmp_path: Path, fakes_registry: Registry
) -> None:
    raw = fake_data(
        source_control={"close_fails": True, "secret_env": "GITHUB_TOKEN"},
        execution_store={"fail": True},
    )
    with pytest.raises(ConfigError, match="execution_store") as info:
        build(config(raw, tmp_path), environ=ENVIRON, registry=fakes_registry)
    assert "close task_provider" in registry_fakes.EVENTS  # after the failing close
    notes = " ".join(getattr(info.value, "__notes__", []))
    assert "cleanup after this failure also failed" in notes
    assert GITHUB_SECRET not in notes


def test_secrets_never_appear_in_creation_errors(tmp_path: Path, fakes_registry: Registry) -> None:
    raw = fake_data(task_provider={"fail": True, "secret_env": "GITHUB_TOKEN"})
    with pytest.raises(ConfigError) as info:
        build(config(raw, tmp_path), environ=ENVIRON, registry=fakes_registry)
    assert GITHUB_SECRET not in str(info.value)
    assert "[redacted]" in str(info.value)
    assert info.value.__cause__ is None
    assert info.value.__suppress_context__


def test_something_that_is_not_an_adapter_is_refused_and_cleaned_up(
    tmp_path: Path, fakes_registry: Registry
) -> None:
    raw = fake_data(container_runtime={"not_a_port": True})
    with pytest.raises(ConfigError, match="not a ContainerRuntime"):
        build(config(raw, tmp_path), environ={}, registry=fakes_registry)
    assert registry_fakes.EVENTS[-3:] == [
        "close container_runtime",
        "close source_control",
        "close task_provider",
    ]


def test_components_close_everything_even_if_one_close_fails(
    tmp_path: Path, fakes_registry: Registry
) -> None:
    raw = fake_data(source_control={"close_fails": True, "secret_env": "GITHUB_TOKEN"})
    components = build(config(raw, tmp_path), environ=ENVIRON, registry=fakes_registry)
    registry_fakes.EVENTS.clear()
    with pytest.raises(ProviderError) as info:
        components.close()
    assert registry_fakes.EVENTS == [
        "close execution_store",
        "close agent_runner",
        "close container_runtime",
        "close source_control",
        "close task_provider",
    ]
    assert GITHUB_SECRET not in " ".join(getattr(info.value, "__notes__", []))
    components.close()  # nothing left to close
    assert len(registry_fakes.EVENTS) == 5


def test_components_never_show_credentials(built: Components) -> None:
    text = repr(built) + repr(vars(built))
    for secret in SECRETS:
        assert secret not in text


def test_the_coordinator_only_needs_ports(tmp_path: Path, fakes_registry: Registry) -> None:
    """A new adapter is one registry entry; Components look the same to the coordinator."""
    with build(config(fake_data(), tmp_path), environ={}, registry=fakes_registry) as components:
        assert components.target.repository == RepositoryRef("fake", "owner/repo")
        assert components.agent_sandbox.network is NetworkPolicy.ALLOWLIST


def test_no_adapter_module_is_imported_until_configured() -> None:
    program = (
        "import sys, ilmar.adapters.registry, ilmar.adapters.builtin, ilmar.cli\n"
        "loaded = sorted(m for m in sys.modules if m.startswith(("
        "'ilmar.adapters.github', 'ilmar.adapters.podman', 'ilmar.adapters.claude_code', "
        "'ilmar.adapters.sqlite', 'ilmar.adapters.git')))\n"
        "print(loaded)\n"
    )
    env = {"PATH": os.environ.get("PATH", ""), "PYTHONPATH": str(Path(__file__).parents[2] / "src")}
    out = subprocess.run(  # noqa: S603 - a fixed program
        [sys.executable, "-c", program], capture_output=True, text=True, check=True, env=env
    ).stdout
    assert out.strip() == "[]"


@pytest.mark.parametrize("secret_like", ["token", "api_key", "password"])
def test_inline_secrets_are_still_rejected_by_the_loader(tmp_path: Path, secret_like: str) -> None:
    raw = data()
    options(raw, "task_provider")[secret_like] = "literal"
    with pytest.raises(ConfigError, match="looks like a secret"):
        config(raw, tmp_path)


# -- adversarial review of 60067d8 -------------------------------------------------------


@pytest.mark.parametrize("error", ["TypeError", "KeyError"])
def test_any_creation_error_is_redacted_and_cleaned_up(
    tmp_path: Path, fakes_registry: Registry, error: str
) -> None:
    raw = fake_data(
        task_provider={"secret_env": "GITHUB_TOKEN"},
        container_runtime={"error": error, "secret_env": "GITHUB_TOKEN"},
    )
    with pytest.raises(ConfigError) as info:
        build(config(raw, tmp_path), environ=ENVIRON, registry=fakes_registry)
    assert GITHUB_SECRET not in str(info.value)
    assert f"({error})" in str(info.value)
    assert info.value.__cause__ is None
    assert info.value.__suppress_context__
    assert registry_fakes.EVENTS[-2:] == ["close source_control", "close task_provider"]


def test_the_model_credential_variable_cannot_be_a_tracker_or_scm_credential(
    tmp_path: Path,
) -> None:
    raw = data()
    options(raw, "agent_runner")["api_key_env"] = "GITHUB_TOKEN"
    message = plan_error(raw, tmp_path)
    assert "GITHUB_TOKEN would be handed into the agent sandbox" in message


def test_the_model_credential_value_cannot_be_a_tracker_or_scm_credential(
    tmp_path: Path, tools: dict[str, str]
) -> None:
    same = {"GITHUB_TOKEN": GITHUB_SECRET, "ANTHROPIC_API_KEY": GITHUB_SECRET}
    with pytest.raises(ConfigError) as info:
        build(config(data(tools), tmp_path), environ=same)
    assert "ANTHROPIC_API_KEY holds the same value as GITHUB_TOKEN" in str(info.value)
    assert GITHUB_SECRET not in str(info.value)
    assert not (tmp_path / "state").exists()  # refused before anything was created


def test_a_relative_path_entry_cannot_supply_the_podman_binary(
    tmp_path: Path, tools: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Running from a repository checkout with "." on PATH must not pick up its podman."""
    planted = tmp_path / "checkout"
    planted.mkdir()
    script = planted / "podman"
    script.write_text("#!/bin/sh\nexit 0\n")
    script.chmod(0o755)
    monkeypatch.chdir(planted)
    monkeypatch.setenv("PATH", ".")
    raw = data(tools)
    options(raw, "container_runtime")["podman"] = "podman"
    with pytest.raises(ConfigError, match="not found on an absolute PATH entry"):
        build(config(raw, tmp_path), environ=ENVIRON)


def test_relative_executable_paths_are_refused(tmp_path: Path) -> None:
    raw = data()
    options(raw, "container_runtime")["podman"] = "bin/podman"
    assert "bare name or an absolute path" in plan_error(raw, tmp_path)


def test_an_executable_resolving_into_a_workspace_is_refused(
    tmp_path: Path, tools: dict[str, str]
) -> None:
    workspace = tmp_path / "state" / "workspaces" / "e1" / "1" / "agent"
    workspace.mkdir(parents=True)
    agent_written = workspace / "podman"
    agent_written.write_text("#!/bin/sh\nexit 0\n")
    agent_written.chmod(0o755)
    link = tmp_path / "bin" / "podman-link"
    link.symlink_to(agent_written)
    raw = data(tools)
    options(raw, "container_runtime")["podman"] = str(link)
    with pytest.raises(ConfigError, match="resolves under the workspaces directory"):
        build(config(raw, tmp_path), environ=ENVIRON)


@pytest.mark.parametrize(
    ("shape", "problem"),
    [
        ("name-method", "name is not a non-empty string"),
        ("missing-argument", "run does not accept timeout_seconds"),
        ("extra-required", "stop requires force"),
        ("positional-timeout", "run must accept timeout_seconds by keyword"),
        ("not-callable", "is_running is not callable"),
    ],
)
def test_ports_are_checked_beyond_member_names(
    tmp_path: Path, fakes_registry: Registry, shape: str, problem: str
) -> None:
    raw = fake_data(container_runtime={"shape": shape})
    with pytest.raises(ConfigError, match="not a ContainerRuntime") as info:
        build(config(raw, tmp_path), environ={}, registry=fakes_registry)
    assert problem in str(info.value)
    assert "close container_runtime" in registry_fakes.EVENTS


def test_every_built_in_adapter_passes_the_deep_port_check(built: Components) -> None:
    assert built.source_control.name == built.target.repository.provider


def test_the_source_control_must_own_its_target(tmp_path: Path, fakes_registry: Registry) -> None:
    raw = fake_data(source_control={"target_provider": "github"})
    with pytest.raises(ConfigError, match="does not match the provider 'github'"):
        build(config(raw, tmp_path), environ={}, registry=fakes_registry)
    assert registry_fakes.EVENTS[-1] == "close task_provider"


def test_a_group_or_world_writable_configuration_is_refused(tmp_path: Path) -> None:
    source = tmp_path / "ilmar.toml"
    source.write_text("")
    source.chmod(0o664)
    with pytest.raises(ConfigError, match="writable by group or others"):
        plan(config(data(), tmp_path, source=source))
    source.chmod(0o644)
    plan(config(data(), tmp_path, source=source))


def test_a_configuration_owned_by_someone_else_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "ilmar.toml"
    source.write_text("")
    source.chmod(0o644)
    if os.geteuid() == 0:  # root-owned files are trusted, so give it a stranger as owner
        os.chown(source, 4242, -1)
    owner = source.stat().st_uid
    monkeypatch.setattr(os, "geteuid", lambda: owner + 1)
    with pytest.raises(ConfigError, match="owned by this user or root"):
        plan(config(data(), tmp_path, source=source))
