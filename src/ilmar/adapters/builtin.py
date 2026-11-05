# SPDX-License-Identifier: Apache-2.0
"""Parsers for the adapters that ship with Ilmar (ADR-0019).

Each parser reads its options strictly and returns a :class:`Plan`. Adapter
modules are imported inside the parser, so only configured adapters are
loaded.

Settings that would weaken the sandbox are not configurable at all
(ADR-0011, ADR-0014): there is no option for unrestricted network, the
sandbox user, the init process, extra mounts or capabilities. The hosts an
agent sandbox may reach come from the agent adapter, not from options.
"""

from __future__ import annotations

import os
import re
import urllib.parse
from pathlib import Path
from typing import Any

from ilmar.adapters.options import Options, Secrets
from ilmar.adapters.registry import BuildContext, Plan
from ilmar.core.components import AgentSandbox, Target
from ilmar.domain.changes import RepositoryRef
from ilmar.domain.errors import ConfigError
from ilmar.domain.workspace import NetworkPolicy, ResourceLimits

_IMAGE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/:@-]{0,254}")
_EXECUTABLE = re.compile(r"[A-Za-z0-9_./][A-Za-z0-9_./+-]{0,254}")
_GLOB = re.compile(r"[*?\[]")

ANTHROPIC_API_HOST = "api.anthropic.com"

# Keys an operator might reach for that would weaken the sandbox. They get a
# clear refusal instead of "unknown option".
_SANDBOX_WEAKENING = frozenset(
    {
        "allow_unrestricted_network",
        "network",
        "privileged",
        "uid",
        "gid",
        "user",
        "userns",
        "init",
        "keepalive",
        "home",
        "cap_add",
        "capabilities",
        "security_opt",
        "seccomp",
        "volumes",
        "mounts",
        "devices",
        "extra_hosts",
        "allowed_hosts",
    }
)


def _refuse_weakening(options: Options) -> None:
    for key in sorted(options.keys() & _SANDBOX_WEAKENING):
        raise options.error(key, "is not configurable: it would weaken the sandbox (ADR-0011)")


def _executable(options: Options, key: str, default: str) -> str:
    return options.string(key, default, pattern=_EXECUTABLE)


def _host_executable(options: Options, key: str, default: str) -> str:
    """A program the coordinator runs on the host: a bare name or an absolute path."""
    name = _executable(options, key, default)
    if "/" in name and not name.startswith("/"):
        raise options.error(key, "must be a bare name or an absolute path")
    return name


def _resolve_executable(name: str, where: str, context: BuildContext) -> str:
    """Find ``name`` on the host, never through a relative or agent-writable directory.

    ``shutil.which`` would honour relative ``PATH`` entries such as ``.``, so a
    program planted in the working directory (for example a repository
    checkout) could be picked up. Only absolute ``PATH`` entries are searched,
    and the result is resolved and must not lie under ``workspaces/``.
    """
    if name.startswith("/"):
        candidates = [Path(name)]
    else:
        entries = os.environ.get("PATH", os.defpath).split(os.pathsep)
        candidates = [Path(entry) / name for entry in entries if os.path.isabs(entry)]
    for candidate in candidates:
        if candidate.is_file() and os.access(candidate, os.X_OK):
            resolved = candidate.resolve()
            workspaces = context.layout.workspaces_root.resolve()
            if resolved == workspaces or resolved.is_relative_to(workspaces):
                raise ConfigError(f"{where}: '{name}' resolves under the workspaces directory")
            return str(resolved)
    raise ConfigError(f"{where}: executable '{name}' not found on an absolute PATH entry")


# -- GitHub ------------------------------------------------------------------------------


def github_tasks(options: Options, context: BuildContext) -> Plan:
    from ilmar.adapters.github.client import DEFAULT_API_URL, GitHubClient
    from ilmar.adapters.github.tasks import GitHubTaskProvider, check_settings

    repository = options.string("repository")
    eligible = options.string("eligible_label", "ilmar:ready")
    claimed = options.string("claimed_label", "ilmar:claimed")
    excluded = options.strings("excluded_labels")
    human = options.strings("human_claim_labels")
    skip_assigned = options.boolean("skip_assigned", True)
    token_env = options.env_name("token_env")
    api_url = options.https_url("api_url", DEFAULT_API_URL)
    check_settings(
        repository=repository,
        eligible_label=eligible,
        claimed_label=claimed,
        excluded_labels=excluded,
        human_claim_labels=human,
    )

    def create(secrets: Secrets) -> GitHubTaskProvider:
        client = GitHubClient(secrets.get(token_env, where=options.where), api_url=api_url)
        return GitHubTaskProvider(
            client,
            repository=repository,
            instance_id=context.instance_id,
            eligible_label=eligible,
            claimed_label=claimed,
            excluded_labels=excluded,
            human_claim_labels=human,
            skip_assigned=skip_assigned,
        )

    return Plan(
        create,
        environment=(token_env,),
        task_repository=RepositoryRef("github", repository),
    )


def github_source_control(options: Options, context: BuildContext) -> Plan:
    from ilmar.adapters.github.client import DEFAULT_API_URL, GitHubClient
    from ilmar.adapters.github.source_control import (
        GitHubSourceControl,
        check_branch,
        check_repository,
    )

    repository = options.string("repository")
    base_branch = options.string("base_branch", "main")
    token_env = options.env_name("token_env")
    api_url = options.https_url("api_url", DEFAULT_API_URL)
    git_base_url = options.https_url("git_base_url", "https://github.com")
    git = _host_executable(options, "git", "git")
    check_repository(repository)
    try:
        check_branch(base_branch)
    except ValueError:
        raise options.error("base_branch", "is not a valid branch name") from None
    # Publishing refuses the same CI paths verification rejects (ADR-0016,
    # ADR-0018); glob patterns cannot be expressed as paths and are skipped.
    ci_paths = tuple(
        pattern.rstrip("/")
        for pattern in context.verification.ci_config_paths
        if not _GLOB.search(pattern)
    )
    state_dir = context.layout.coordinator_root / "source-control"

    def create(secrets: Secrets) -> GitHubSourceControl:
        token = secrets.get(token_env, where=options.where)
        return GitHubSourceControl(
            GitHubClient(token, api_url=api_url),
            token=token,
            state_dir=state_dir,
            git_base_url=git_base_url,
            allowed_protocols=("https",),
            git=_resolve_executable(git, options.where, context),
            ci_config_paths=ci_paths,
        )

    return Plan(
        create,
        environment=(token_env,),
        target=Target(RepositoryRef("github", repository), base_branch),
    )


# -- Podman ------------------------------------------------------------------------------


def podman_runtime(options: Options, context: BuildContext) -> Plan:
    from ilmar.adapters.podman.runtime import EgressSettings, PodmanRuntime, SandboxProfile

    _refuse_weakening(options)
    podman = _host_executable(options, "podman", "podman")
    proxy_image = options.optional_string("proxy_image", pattern=_IMAGE)
    egress_network = options.string("egress_network", "podman")
    defaults = SandboxProfile()
    tmpfs_mb = options.positive_int("tmpfs_mb", defaults.tmpfs_mb)
    base = defaults.default_limits
    limits = ResourceLimits(
        cpus=options.optional_positive_number("cpus") or base.cpus,
        memory_mb=options.optional_positive_int("memory_mb") or base.memory_mb,
        max_processes=options.optional_positive_int("max_processes") or base.max_processes,
    )
    profile = SandboxProfile(tmpfs_mb=tmpfs_mb, default_limits=limits)
    egress = None
    if proxy_image is not None:
        egress = EgressSettings(proxy_image=proxy_image, egress_network=egress_network)
    elif options.has("egress_network"):
        raise options.error("egress_network", "needs proxy_image")
    networks = {NetworkPolicy.NONE} | ({NetworkPolicy.ALLOWLIST} if egress else set())

    def create(secrets: Secrets) -> PodmanRuntime:
        return PodmanRuntime(
            layout=context.layout,
            podman=_resolve_executable(podman, options.where, context),
            profile=profile,
            egress=egress,
            allow_unrestricted_network=False,  # never from configuration (ADR-0014)
        )

    return Plan(create, networks=frozenset(networks))


# -- Claude Code -------------------------------------------------------------------------


def claude_code_runner(options: Options, context: BuildContext) -> Plan:
    from ilmar.adapters.claude_code.runner import ClaudeCodeRunner, ClaudeCodeSettings

    _refuse_weakening(options)
    api_key_env = options.env_name("api_key_env")
    image = options.string("image", pattern=_IMAGE)
    base_url = options.optional_https_url("base_url")
    settings = ClaudeCodeSettings(
        executable=_executable(options, "executable", "claude"),
        model=options.optional_string("model"),
        max_turns=options.positive_int("max_turns", 200),
        permission_mode=options.string("permission_mode", "bypassPermissions"),
        expected_version=options.optional_string("expected_version"),
        base_url=base_url,
    )
    host = urllib.parse.urlsplit(base_url).hostname if base_url else ANTHROPIC_API_HOST
    # The agent reaches exactly the model endpoint: no option widens this.
    sandbox = AgentSandbox(image, NetworkPolicy.ALLOWLIST, (host or ANTHROPIC_API_HOST,))

    def create(secrets: Secrets) -> ClaudeCodeRunner:
        return ClaudeCodeRunner(
            api_key=secrets.get(api_key_env, where=options.where), settings=settings
        )

    return Plan(create, environment=(api_key_env,), agent_sandbox=sandbox, enters_sandbox=True)


# -- SQLite ------------------------------------------------------------------------------


def sqlite_store(options: Options, context: BuildContext) -> Plan:
    from ilmar.adapters.sqlite import SQLiteExecutionStore

    path = options.path("path", context.state_dir / "executions.sqlite3", base_dir=context.base_dir)
    resolved = path.resolve()
    workspaces = context.layout.workspaces_root.resolve()
    if resolved == workspaces or resolved.is_relative_to(workspaces):
        raise options.error("path", "must not be under the workspaces directory agents write to")

    def create(secrets: Secrets) -> SQLiteExecutionStore:
        resolved.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        return SQLiteExecutionStore(resolved)

    return Plan(create, close=_close)


def _close(store: Any) -> None:
    store.close()


__all__ = [
    "claude_code_runner",
    "github_source_control",
    "github_tasks",
    "podman_runtime",
    "sqlite_store",
]
