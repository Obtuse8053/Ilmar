# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

import pytest

from ilmar.config import PORT_NAMES, Limits, load_config, parse_config
from ilmar.domain.errors import ConfigError
from ilmar.domain.safety_controls import safety_controls

REPO_ROOT = Path(__file__).resolve().parents[2]
BASE = Path("/srv/ilmar")


def minimal() -> dict[str, Any]:
    return {"adapters": {port: {"kind": "fake"} for port in PORT_NAMES}}


def with_options(port: str, **options: Any) -> dict[str, Any]:
    data = minimal()
    data["adapters"][port]["options"] = options
    return data


def test_example_configuration_is_valid() -> None:
    config = load_config(REPO_ROOT / "ilmar.example.toml")
    assert config.adapters["task_provider"].kind == "github"
    assert config.adapters["container_runtime"].kind == "podman"
    assert config.adapters["agent_runner"].kind == "claude-code"
    assert config.adapters["execution_store"].kind == "sqlite"
    assert config.state_dir == REPO_ROOT / ".ilmar"
    assert config.verification is not None
    assert [c.name for c in config.verification.commands] == ["lint", "types", "tests"]


def test_defaults() -> None:
    config = parse_config(minimal(), base_dir=BASE)
    assert config.limits == Limits()
    assert config.state_dir == BASE / ".ilmar"
    assert dict(config.adapters["agent_runner"].options) == {}


def test_absolute_state_dir_is_kept() -> None:
    data = minimal() | {"ilmar": {"state_dir": "/var/lib/ilmar"}}
    assert parse_config(data, base_dir=BASE).state_dir == Path("/var/lib/ilmar")


def test_options_are_read_only() -> None:
    config = parse_config(with_options("execution_store", path="x.db"), base_dir=BASE)
    options = config.adapters["execution_store"].options
    assert options["path"] == "x.db"
    with pytest.raises(TypeError):
        options["path"] = "other.db"  # type: ignore[index]


@pytest.mark.parametrize("port", PORT_NAMES)
def test_every_port_is_required(port: str) -> None:
    data = minimal()
    del data["adapters"][port]
    with pytest.raises(ConfigError, match=port):
        parse_config(data, base_dir=BASE)


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda d: d.update(extra={}), "unknown key"),
        (lambda d: d.update(ilmar={"colour": "blue"}), "unknown key"),
        (lambda d: d.update(limits={"max_attempt": 2}), "unknown key"),
        (lambda d: d["adapters"].update(scheduler={"kind": "x"}), "unknown key"),
        (lambda d: d["adapters"]["task_provider"].update(token="x"), "unknown key"),
        (lambda d: d["adapters"]["task_provider"].update(kind="GitHub"), "kind"),
        (lambda d: d["adapters"]["task_provider"].pop("kind"), "kind"),
        (lambda d: d["adapters"]["task_provider"].update(options="x"), "table"),
        (lambda d: d.update(ilmar={"state_dir": ""}), "state_dir"),
        (lambda d: d.update(limits={"max_attempts": 0}), "max_attempts"),
        (lambda d: d.update(limits={"max_attempts": True}), "max_attempts"),
        (lambda d: d.update(limits={"max_concurrent_executions": 1.5}), "max_concurrent"),
        (lambda d: d.update(limits={"agent_timeout_seconds": -1}), "agent_timeout"),
        (lambda d: d.update(limits={"agent_timeout_seconds": "1h"}), "agent_timeout"),
        (lambda d: d.update(limits={"retry_backoff_base_seconds": 0}), "retry_backoff_base"),
        (lambda d: d.update(limits={"retry_backoff_base_seconds": True}), "retry_backoff_base"),
        (
            lambda d: d.update(limits={"retry_backoff_max_seconds": float("inf")}),
            "retry_backoff_max",
        ),
        (
            lambda d: d.update(limits={"retry_backoff_max_seconds": float("nan")}),
            "retry_backoff_max",
        ),
        (
            lambda d: d.update(
                limits={"retry_backoff_base_seconds": 600, "retry_backoff_max_seconds": 60}
            ),
            "must not be less than",
        ),
        (lambda d: d.update(limits={"stall_timeout_seconds": 0}), "stall_timeout"),
        (lambda d: d.update(limits={"stall_timeout_seconds": -5}), "stall_timeout"),
        (lambda d: d.update(limits={"stall_timeout_seconds": True}), "stall_timeout"),
        (lambda d: d.update(limits={"stall_timeout_seconds": "15m"}), "stall_timeout"),
        (lambda d: d.update(limits={"stall_timeout_seconds": float("nan")}), "stall_timeout"),
        (
            lambda d: d.update(limits={"agent_timeout_seconds": 600, "stall_timeout_seconds": 601}),
            "must not be greater than agent_timeout",
        ),
    ],
)
def test_invalid_configuration_is_rejected(mutate: Any, message: str) -> None:
    data = copy.deepcopy(minimal())
    mutate(data)
    with pytest.raises(ConfigError, match=message):
        parse_config(data, base_dir=BASE)


@pytest.mark.parametrize(
    "key", ["token", "github_token", "api_key", "apiKey", "password", "client_secret"]
)
def test_inline_secrets_are_rejected(key: str) -> None:
    with pytest.raises(ConfigError, match="looks like a secret"):
        parse_config(with_options("source_control", **{key: "hunter2"}), base_dir=BASE)


def test_nested_inline_secrets_are_rejected() -> None:
    data = with_options("agent_runner", auth={"api_key": "sk-test"})
    with pytest.raises(ConfigError, match=r"options\.auth"):
        parse_config(data, base_dir=BASE)


def test_secret_references_via_environment_are_allowed() -> None:
    data = with_options("source_control", token_env="GITHUB_TOKEN")
    config = parse_config(data, base_dir=BASE)
    assert config.adapters["source_control"].options["token_env"] == "GITHUB_TOKEN"


def test_missing_file(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="not found"):
        load_config(tmp_path / "nope.toml")


def test_invalid_toml(tmp_path: Path) -> None:
    path = tmp_path / "ilmar.toml"
    path.write_text("[adapters\n")
    with pytest.raises(ConfigError, match="invalid TOML"):
        load_config(path)


def test_relative_state_dir_resolves_against_config_file(tmp_path: Path) -> None:
    path = tmp_path / "conf" / "ilmar.toml"
    path.parent.mkdir()
    lines = ['[ilmar]\nstate_dir = "state"\n']
    lines += [f'[adapters.{port}]\nkind = "fake"\n' for port in PORT_NAMES]
    path.write_text("".join(lines))
    assert load_config(path).state_dir == tmp_path / "conf" / "state"


# -- [verification] (#9, ADR-0018) ---------------------------------------------------


def with_verification(**section: Any) -> dict[str, Any]:
    data = minimal()
    data["verification"] = {
        "image": "localhost/verify:1",
        "commands": [{"name": "tests", "argv": ["pytest", "-q"], "timeout_seconds": 900}],
    } | section
    return data


def test_verification_is_optional() -> None:
    assert parse_config(minimal(), base_dir=BASE).verification is None


def test_verification_policy_is_parsed() -> None:
    policy = parse_config(
        with_verification(memory_mb=2048, cpus=2, max_processes=256), base_dir=BASE
    ).verification
    assert policy is not None
    assert policy.image == "localhost/verify:1"
    (command,) = policy.commands
    assert (command.name, command.argv, command.timeout_seconds, command.required) == (
        "tests",
        ("pytest", "-q"),
        900.0,
        True,
    )
    assert command.job == "default"
    assert (policy.limits.memory_mb, policy.limits.cpus, policy.limits.max_processes) == (
        2048,
        2.0,
        256,
    )


def test_path_classes_can_only_be_extended() -> None:
    policy = parse_config(
        with_verification(
            extra_protected_paths=["docs/policy.md"],
            extra_check_influencing_paths=["fixtures/"],
            extra_ci_config_paths=[".ci/pipeline.yml"],
        ),
        base_dir=BASE,
    ).verification
    assert policy is not None
    assert "**/AGENTS.md" in policy.protected_paths
    assert "docs/policy.md" in policy.protected_paths
    assert ".github/" in policy.ci_config_paths
    assert ".ci/pipeline.yml" in policy.ci_config_paths
    assert "tests/" in policy.check_influencing_paths
    assert "fixtures/" in policy.check_influencing_paths


def test_safety_controls_come_from_the_release_and_can_only_be_extended() -> None:
    """ADR-0031 §1 (#92): built-ins always; Ilmar's own list only for itself."""
    other = parse_config(with_verification(), base_dir=BASE).verification
    assert other is not None
    assert other.safety_controls == safety_controls(self_repository=False)
    own = parse_config(
        with_verification(self_repository=True, extra_safety_controls=["ops/"]),
        base_dir=BASE,
    ).verification
    assert own is not None
    assert own.safety_controls == safety_controls(self_repository=True, extra=("ops/",))
    assert "src/ilmar/domain/safety_controls.py" in own.safety_controls
    assert own.digest() != other.digest()


@pytest.mark.parametrize(
    ("section", "message"),
    [
        ({"image": ""}, "image"),
        ({"commands": []}, "at least one"),
        ({"commands": [{"name": "t", "argv": "pytest"}]}, "argv"),
        ({"commands": [{"name": "t", "argv": []}]}, "argv"),
        ({"commands": [{"name": "t", "argv": ["x"], "timeout_seconds": "5"}]}, "timeout"),
        ({"commands": [{"name": "t", "argv": ["x"], "required": "yes"}]}, "required"),
        ({"commands": [{"name": "t", "argv": ["x"], "shell": True}]}, "unknown"),
        ({"commands": [{"name": "bad/name", "argv": ["x"]}]}, "check name"),
        ({"commands": [{"name": "t", "argv": ["x"], "job": "Bad Job"}]}, "job"),
        ({"ci_config_paths": []}, "unknown"),
        ({"extra_ci_config_paths": "x"}, "list"),
        ({"extra_protected_paths": ["/abs"]}, "pattern"),
        ({"self_repository": "yes"}, "self_repository"),
        ({"extra_safety_controls": ["/abs"]}, "pattern"),
        ({"cpus": 0}, "cpus"),
        ({"memory_mb": -1}, "memory_mb"),
    ],
)
def test_verification_errors_are_reported(section: dict[str, Any], message: str) -> None:
    with pytest.raises(ConfigError, match=message):
        parse_config(with_verification(**section), base_dir=BASE)


def test_retry_backoff_defaults_and_values() -> None:
    """#13: a positive base is required in configuration (no immediate retries)."""
    config = parse_config(minimal(), base_dir=BASE)
    assert (config.limits.retry_backoff_base_seconds, config.limits.retry_backoff_max_seconds) == (
        300.0,
        21600.0,
    )
    data = copy.deepcopy(minimal())
    data["limits"] = {"retry_backoff_base_seconds": 30, "retry_backoff_max_seconds": 30}
    config = parse_config(data, base_dir=BASE)
    assert (config.limits.retry_backoff_base_seconds, config.limits.retry_backoff_max_seconds) == (
        30.0,
        30.0,
    )


def test_stall_timeout_default_and_values() -> None:
    """#13, ADR-0035: 900 s by default, never longer than the hard lifetime."""
    assert parse_config(minimal(), base_dir=BASE).limits.stall_timeout_seconds == 900.0
    data = copy.deepcopy(minimal())
    data["limits"] = {"agent_timeout_seconds": 600, "stall_timeout_seconds": 600}
    assert parse_config(data, base_dir=BASE).limits.stall_timeout_seconds == 600.0
    data["limits"] = {"agent_timeout_seconds": 600, "stall_timeout_seconds": 2.5}
    assert parse_config(data, base_dir=BASE).limits.stall_timeout_seconds == 2.5


def test_execution_wall_default_follows_the_agent_timeout() -> None:
    """#90, ADR-0029 §1: unset, it is the agent timeout plus 600 seconds."""
    assert parse_config(minimal(), base_dir=BASE).limits.execution_wall_seconds == 4200.0
    data = copy.deepcopy(minimal())
    data["limits"] = {"agent_timeout_seconds": 7200}
    assert parse_config(data, base_dir=BASE).limits.execution_wall_seconds == 7800.0
    data["limits"] = {
        "agent_timeout_seconds": 600,
        "stall_timeout_seconds": 300,
        "execution_wall_seconds": 600,
    }
    assert parse_config(data, base_dir=BASE).limits.execution_wall_seconds == 600.0


@pytest.mark.parametrize(
    ("limits", "message"),
    [
        ({"execution_wall_seconds": 0}, "execution_wall"),
        ({"execution_wall_seconds": True}, "execution_wall"),
        ({"execution_wall_seconds": "1h"}, "execution_wall"),
        ({"execution_wall_seconds": float("inf")}, "execution_wall"),
        (
            {
                "agent_timeout_seconds": 600,
                "stall_timeout_seconds": 300,
                "execution_wall_seconds": 599,
            },
            "not be less than",
        ),
    ],
)
def test_invalid_execution_wall_is_rejected(limits: dict[str, Any], message: str) -> None:
    data = copy.deepcopy(minimal())
    data["limits"] = limits
    with pytest.raises(ConfigError, match=message):
        parse_config(data, base_dir=BASE)
