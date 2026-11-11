# SPDX-License-Identifier: Apache-2.0
"""PodmanRuntime details beyond the shared contract (ADR-0011, ADR-0014)."""

from __future__ import annotations

import logging
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path, PurePosixPath

import pytest

from ilmar.adapters.podman import EgressSettings, PodmanRuntime, SandboxProfile
from ilmar.adapters.podman.runtime import (
    LABEL_MANAGED,
    PODMAN_ENVIRONMENT,
    TIMED_OUT_EXIT_CODE,
    proc_root_identity,
    subprocess_executor,
)
from ilmar.core.layout import WorkspaceLayout
from ilmar.core.safe_fs import ensure_directory
from ilmar.domain.errors import (
    ContainmentError,
    OwnershipConflictError,
    PolicyViolationError,
    ProviderError,
)
from ilmar.domain.workspace import (
    ContainerHandle,
    ContainerSpec,
    DirectoryIdentity,
    NetworkPolicy,
    ResourceLimits,
    ResourceOwner,
)

from contracts.container_runtime import SetAfter
from fake_podman import FakeNetwork, FakePodman

OWNER = ResourceOwner(instance_id="inst-a", execution_id="exec-1", attempt=1, token="tok-1")
Workspace = tuple[Path, DirectoryIdentity]
SECRET = "ghp-must-never-reach-podman"


@pytest.fixture
def layout(tmp_path: Path) -> WorkspaceLayout:
    layout = WorkspaceLayout.under(tmp_path.resolve() / "state")
    layout.workspaces_root.mkdir(parents=True)
    return layout


@pytest.fixture
def podman() -> FakePodman:
    return FakePodman()


@pytest.fixture
def workspace(layout: WorkspaceLayout) -> tuple[Path, DirectoryIdentity]:
    path = layout.agent_workspace("exec-1", 1)
    return path, ensure_directory(path, root=layout.workspaces_root)


def runtime(layout: WorkspaceLayout, podman: FakePodman, **kwargs: object) -> PodmanRuntime:
    settings: dict[str, object] = {
        "layout": layout,
        "environ": {"PATH": "/usr/bin", "GITHUB_TOKEN": SECRET, "HTTPS_PROXY": "http://p:1"},
        "executor": podman,
        "mount_probe": podman.mount_probe,
    } | kwargs
    return PodmanRuntime(**settings)  # type: ignore[arg-type]


def spec(workspace: tuple[Path, DirectoryIdentity], **kwargs: object) -> ContainerSpec:
    path, identity = workspace
    settings: dict[str, object] = {
        "image": "localhost/ilmar-test:1",
        "host_workspace": path,
        "name": "ilmar-exec-1-1",
        "owner": OWNER,
        "workspace_identity": identity,
    } | kwargs
    return ContainerSpec(**settings)  # type: ignore[arg-type]


def run_args(podman: FakePodman) -> list[str]:
    (call,) = [c for c in podman.calls if c.argv[1] == "run"]
    return call.argv


# -- the ADR-0011 profile ---------------------------------------------------


def test_hardened_profile_options(
    layout: WorkspaceLayout, podman: FakePodman, workspace: Workspace
) -> None:
    runtime(layout, podman).start(spec(workspace))
    args = run_args(podman)
    for option in (
        "--pull=never",
        "--userns=keep-id:uid=1000,gid=1000",
        "--cap-drop=all",
        "--security-opt=no-new-privileges",
        "--read-only",
        "--read-only-tmpfs=false",
        "--env-host=false",
        "--http-proxy=false",
        "--network=none",
        "--no-hosts",
        "--init",
        "--log-driver=none",
    ):
        assert option in args
    assert args[args.index("--user") + 1] == "1000:1000"
    assert args[args.index("--sysctl") + 1] == "net.ipv4.ping_group_range=1 0"
    assert "--privileged" not in args
    assert not any(a.startswith(("--cap-add", "--security-opt=seccomp")) for a in args)
    tmpfs = [args[i + 1] for i, a in enumerate(args) if a == "--tmpfs"]
    assert [t.split(":")[0] for t in tmpfs] == ["/tmp", "/home/ilmar"]
    assert all("nosuid" in t and "nodev" in t and "size=1024m" in t for t in tmpfs)
    assert args[-3:] == ["localhost/ilmar-test:1", "sleep", "1000000000"]


def test_exactly_one_host_mount(
    layout: WorkspaceLayout, podman: FakePodman, workspace: Workspace
) -> None:
    runtime(layout, podman).start(spec(workspace))
    args = run_args(podman)
    mounts = [args[i + 1] for i, a in enumerate(args) if a == "--mount"]
    assert mounts == [f"type=bind,source={workspace[0]},destination=/workspace"]
    assert not any(a in {"-v", "--volume", "--volumes-from"} for a in args)
    assert args[args.index("--workdir") + 1] == "/workspace"


def test_default_and_requested_limits(
    layout: WorkspaceLayout, podman: FakePodman, workspace: Workspace
) -> None:
    rt = runtime(layout, podman)
    rt.start(spec(workspace, limits=ResourceLimits(memory_mb=512)))
    args = run_args(podman)
    assert args[args.index("--pids-limit") + 1] == "1024"
    assert args[args.index("--memory") + 1] == "512m"
    assert args[args.index("--cpus") + 1] == "2"


def test_ownership_labels(
    layout: WorkspaceLayout, podman: FakePodman, workspace: Workspace
) -> None:
    runtime(layout, podman).start(spec(workspace))
    container = podman.by_name("ilmar-exec-1-1")
    assert container is not None
    assert container.labels[LABEL_MANAGED] == "true"
    assert container.labels["io.ilmar.owner.token"] == "tok-1"


def test_unrestricted_network_is_explicit_and_logged(
    layout: WorkspaceLayout,
    podman: FakePodman,
    workspace: Workspace,
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.WARNING):
        runtime(layout, podman, allow_unrestricted_network=True).start(
            spec(workspace, network=NetworkPolicy.UNRESTRICTED)
        )
    args = run_args(podman)
    assert not any(a.startswith("--network") for a in args)
    assert "UNRESTRICTED" in caplog.text


def test_profile_rejects_root_and_bad_settings() -> None:
    with pytest.raises(ValueError, match="root"):
        SandboxProfile(uid=0)
    with pytest.raises(ValueError, match="home"):
        SandboxProfile(home=PurePosixPath("/tmp"))
    with pytest.raises(ValueError, match="keepalive"):
        SandboxProfile(keepalive=())


# -- environment --------------------------------------------------------------


def test_podman_itself_sees_only_a_minimal_environment(
    layout: WorkspaceLayout, podman: FakePodman, workspace: Workspace
) -> None:
    runtime(layout, podman).start(spec(workspace))
    for call in podman.calls:
        assert set(call.env) <= set(PODMAN_ENVIRONMENT)
        assert SECRET not in call.env.values()
        assert "HTTPS_PROXY" not in call.env


def test_values_travel_in_memory_never_in_argv_or_on_disk(
    layout: WorkspaceLayout,
    podman: FakePodman,
    workspace: Workspace,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Review for #11d: an env *file* outlived a killed coordinator in $TMPDIR."""
    spool = tmp_path / "tmp"
    spool.mkdir()
    monkeypatch.setenv("TMPDIR", str(spool))
    rt = runtime(layout, podman)
    handle = rt.start(spec(workspace, env={"PROFILE": "sandbox"}))
    rt.run(handle, ["echo", "x"], timeout_seconds=5, env={"ANTHROPIC_API_KEY": "sk-model"})
    started = next(c for c in podman.calls if c.argv[1] == "run")
    executed = next(c for c in podman.calls if c.argv[1] == "exec")
    # The container's own environment: a memfd env file (it becomes part of the
    # container's definition, so ContainerSpec.env never carries a secret).
    assert started.env_file == {"HOME": "/home/ilmar", "PROFILE": "sandbox"}
    env_file = started.argv[started.argv.index("--env-file") + 1]
    assert env_file.startswith("/dev/fd/")
    assert len(started.pass_fds) == 1
    # A command's environment: stdin to an in-container loader, never --env or
    # --env-file, which Podman records in its state database (review of PR #50).
    assert executed.command_env == {"ANTHROPIC_API_KEY": "sk-model"}
    assert executed.argv[2:3] == ["--interactive"]
    assert "--env-file" not in executed.argv
    assert executed.pass_fds == ()
    for call in podman.calls:
        assert not any("sk-model" in arg for arg in call.argv)
    open_memory = [link for link in _open_descriptors() if "ilmar-env" in link]
    assert open_memory == []  # every descriptor was closed right after use
    assert list(spool.iterdir()) == []  # nothing was ever written to disk


def test_the_env_loader_exports_values_verbatim_without_evaluating_them(tmp_path: Path) -> None:
    """The real loader in a real POSIX sh: hostile values stay data."""
    import subprocess

    from ilmar.adapters.podman.runtime import ENV_LOADER, env_stream

    hostile = {"A": "$(touch pwned) `touch pwned2` ; rm -rf / # ' \" \\ *", "B": "=x=y", "C": ""}
    done = subprocess.run(  # noqa: S603 - a fixed program
        ["/bin/sh", "-c", ENV_LOADER, "ilmar-env", "/usr/bin/env"],
        input=env_stream(hostile), capture_output=True, cwd=tmp_path, check=False,
        env={"PATH": "/usr/bin:/bin"},
    )  # fmt: skip
    assert done.returncode == 0, done.stderr
    exported = dict(line.split("=", 1) for line in done.stdout.decode().splitlines())
    assert {k: exported[k] for k in hostile} == hostile
    assert list(tmp_path.iterdir()) == []  # nothing was executed


def test_a_command_without_values_runs_as_before(
    layout: WorkspaceLayout, podman: FakePodman, workspace: Workspace
) -> None:
    rt = runtime(layout, podman)
    handle = rt.start(spec(workspace))
    rt.run(handle, ["echo", "x"], timeout_seconds=5)
    executed = next(c for c in podman.calls if c.argv[1] == "exec")
    assert "--interactive" not in executed.argv
    assert executed.stdin is None


def _open_descriptors() -> list[str]:
    links = []
    for fd in os.listdir("/proc/self/fd"):
        try:
            links.append(os.readlink(f"/proc/self/fd/{fd}"))
        except FileNotFoundError:  # the listing's own descriptor, already closed
            continue
    return links


@pytest.mark.parametrize(
    ("env", "message"),
    [({"BAD-NAME": "x"}, "name"), ({"X": "two\nlines"}, "single line")],
)
def test_environment_is_validated(
    layout: WorkspaceLayout,
    podman: FakePodman,
    workspace: Workspace,
    env: dict[str, str],
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        runtime(layout, podman).start(spec(workspace, env=env))
    assert "run" not in podman.commands()


# -- preconditions --------------------------------------------------------------


def test_rootful_podman_is_refused(layout: WorkspaceLayout, workspace: Workspace) -> None:
    podman = FakePodman(rootless=False)
    with pytest.raises(ProviderError, match="rootless"):
        runtime(layout, podman).start(spec(workspace))
    assert podman.commands() == ["info"]


def test_rootless_is_checked_once(
    layout: WorkspaceLayout, podman: FakePodman, workspace: Workspace
) -> None:
    rt = runtime(layout, podman)
    rt.start(spec(workspace))
    rt.start(spec(workspace))
    assert podman.commands().count("info") == 1


def test_mount_is_authorized_before_podman_run(
    layout: WorkspaceLayout, podman: FakePodman, workspace: Workspace, tmp_path: Path
) -> None:
    path, _ = workspace
    host_data = tmp_path / "host-data"
    host_data.mkdir()
    path.rmdir()
    path.symlink_to(host_data)
    with pytest.raises(OwnershipConflictError):
        runtime(layout, podman).start(spec(workspace))
    assert "run" not in podman.commands()


def test_paths_outside_the_workspaces_root_are_never_mounted(
    layout: WorkspaceLayout, podman: FakePodman, tmp_path: Path
) -> None:
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    info = outside.stat()
    with pytest.raises(ValueError, match="only paths inside"):
        runtime(layout, podman).start(spec((outside, DirectoryIdentity(info.st_dev, info.st_ino))))
    assert "run" not in podman.commands()


def test_post_start_identity_mismatch_removes_the_container(
    layout: WorkspaceLayout, podman: FakePodman, workspace: Workspace
) -> None:
    rt = runtime(layout, podman, mount_probe=lambda pid, path: DirectoryIdentity(0, 0))
    with pytest.raises(OwnershipConflictError, match="mounted"):
        rt.start(spec(workspace))
    assert podman.containers == {}
    assert "rm" in podman.commands()


def test_post_start_probe_failure_fails_closed(
    layout: WorkspaceLayout, podman: FakePodman, workspace: Workspace
) -> None:
    def broken(pid: int, path: PurePosixPath) -> DirectoryIdentity:
        raise PermissionError(13, "Permission denied")

    with pytest.raises(ProviderError, match="mounted workspace"):
        runtime(layout, podman, mount_probe=broken).start(spec(workspace))
    assert podman.containers == {}


@pytest.mark.parametrize("image", ["-v", "img name", ""])
def test_image_reference_is_validated(
    layout: WorkspaceLayout, podman: FakePodman, workspace: Workspace, image: str
) -> None:
    with pytest.raises(ValueError, match="image"):
        runtime(layout, podman).start(spec(workspace, image=image))


# -- handles ------------------------------------------------------------------


@pytest.mark.parametrize(
    "handle",
    [
        ContainerHandle(runtime="docker", id="a" * 64, name="x"),
        ContainerHandle(runtime="podman", id="ilmar-exec-1-1", name="x"),
        ContainerHandle(runtime="podman", id="abc", name="x"),
    ],
)
def test_operations_require_a_full_id_from_this_runtime(
    layout: WorkspaceLayout, podman: FakePodman, handle: ContainerHandle
) -> None:
    rt = runtime(layout, podman)
    with pytest.raises(ProviderError):
        rt.is_running(handle)
    with pytest.raises(ProviderError):
        rt.stop(handle, owner=OWNER)
    assert podman.calls == []


def test_timeout_removes_the_container(
    layout: WorkspaceLayout, podman: FakePodman, workspace: Workspace
) -> None:
    rt = runtime(layout, podman)
    handle = rt.start(spec(workspace))
    result = rt.run(handle, ["sleep", "60"], timeout_seconds=1)
    assert result.timed_out
    assert "rm" in podman.commands()
    assert podman.containers == {}


# -- the real executor and probe ---------------------------------------------------


def test_subprocess_executor_captures_output() -> None:
    done = subprocess_executor(
        [sys.executable, "-c", "import os,sys; print(os.environ['ONLY']); sys.exit(4)"],
        {"ONLY": "this"},
        10,
    )
    assert (done.returncode, done.stdout.strip(), done.timed_out) == (4, "this", False)


def test_subprocess_executor_kills_the_whole_process_group_on_timeout(tmp_path: Path) -> None:
    marker = tmp_path / "grandchild-survived"
    grandchild = tmp_path / "grandchild.py"
    grandchild.write_text(f"import time\ntime.sleep(3)\nopen({str(marker)!r}, 'w').close()\n")
    parent = tmp_path / "parent.py"
    parent.write_text(
        "import subprocess, sys, time\n"
        f"subprocess.Popen([sys.executable, {str(grandchild)!r}])\n"
        "time.sleep(60)\n"
    )
    script = str(parent)
    began = time.monotonic()
    done = subprocess_executor([sys.executable, script], dict(os.environ), 1)
    assert done.timed_out
    assert time.monotonic() - began < 15
    time.sleep(4)
    assert not marker.exists()


def test_subprocess_executor_bounds_the_output_it_keeps() -> None:
    """A sandboxed command cannot exhaust the coordinator's memory (PR #45 review)."""
    program = (
        "import sys; sys.stdout.write('x' * 5_000_000); sys.stderr.write('e' * 10); sys.exit(3)"
    )
    done = subprocess_executor([sys.executable, "-c", program], {}, 30, output_limit=1000)
    assert done.returncode == 3
    assert not done.timed_out
    assert done.stdout.startswith("x" * 1000)
    assert done.stdout.endswith("[ilmar: 4999000 further bytes of output discarded]\n")
    assert len(done.stdout) < 1100
    assert done.stderr == "e" * 10


def test_subprocess_executor_times_out_when_a_leftover_process_holds_the_pipe(
    tmp_path: Path,
) -> None:
    script = tmp_path / "detach.py"
    script.write_text(
        "import subprocess, sys\n"
        "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
    )
    began = time.monotonic()
    done = subprocess_executor([sys.executable, str(script)], dict(os.environ), 1)
    assert done.timed_out
    assert time.monotonic() - began < 15


def test_subprocess_executor_reports_a_missing_program() -> None:
    with pytest.raises(ProviderError, match="cannot run"):
        subprocess_executor(["/nonexistent/podman"], {}, 5)


def test_proc_root_identity_matches_the_host_view(tmp_path: Path) -> None:
    info = tmp_path.stat()
    seen = proc_root_identity(os.getpid(), PurePosixPath(str(tmp_path)))
    assert seen == DirectoryIdentity(info.st_dev, info.st_ino)


def test_proc_root_identity_refuses_a_symlink(tmp_path: Path) -> None:
    link = tmp_path / "link"
    link.symlink_to(tmp_path)
    with pytest.raises(OwnershipConflictError):
        proc_root_identity(os.getpid(), PurePosixPath(str(link)))


# -- PR #39 review: default-deny egress, fail-closed removal -----------------------


def test_unrestricted_network_is_denied_by_default(
    layout: WorkspaceLayout, podman: FakePodman, workspace: Workspace
) -> None:
    with pytest.raises(PolicyViolationError, match="not permitted"):
        runtime(layout, podman).start(spec(workspace, network=NetworkPolicy.UNRESTRICTED))
    assert "run" not in podman.commands()


@pytest.mark.parametrize("behaviour", ["fail", "hang"])
def test_unconfirmed_removal_after_timeout_fails_closed_and_quarantines(
    layout: WorkspaceLayout, podman: FakePodman, workspace: Workspace, behaviour: str
) -> None:
    rt = runtime(layout, podman)
    handle = rt.start(spec(workspace))
    podman.rm_behaviour = behaviour
    with pytest.raises(ContainmentError) as caught:
        rt.run(handle, ["sleep", "60"], timeout_seconds=1)
    assert caught.value.container_id == handle.id
    assert handle.id in podman.containers  # still there: reported, not assumed gone

    # The workspace stays quarantined while removal cannot be confirmed ...
    runs_before = podman.commands().count("run")
    with pytest.raises(ContainmentError):
        rt.start(spec(workspace))
    assert podman.commands().count("run") == runs_before

    # ... and is released once a retry confirms the container is gone.
    podman.rm_behaviour = "ok"
    replacement = rt.start(spec(workspace))
    assert handle.id not in podman.containers
    assert replacement.id != handle.id


def test_quarantine_is_per_workspace(
    layout: WorkspaceLayout, podman: FakePodman, workspace: Workspace
) -> None:
    rt = runtime(layout, podman)
    handle = rt.start(spec(workspace))
    podman.rm_behaviour = "fail"
    with pytest.raises(ContainmentError):
        rt.run(handle, ["sleep", "60"], timeout_seconds=1)
    podman.rm_behaviour = "ok"
    other = layout.agent_workspace("exec-2", 1)
    other_spec = spec(
        (other, ensure_directory(other, root=layout.workspaces_root)), name="ilmar-exec-2-1"
    )
    assert rt.start(other_spec).name == "ilmar-exec-2-1"


def test_failed_stop_is_a_containment_error(
    layout: WorkspaceLayout, podman: FakePodman, workspace: Workspace
) -> None:
    rt = runtime(layout, podman)
    handle = rt.start(spec(workspace))
    podman.rm_behaviour = "fail"
    with pytest.raises(ContainmentError):
        rt.stop(handle, owner=OWNER)
    assert rt.is_running(handle)
    podman.rm_behaviour = "ok"
    rt.stop(handle, owner=OWNER)
    assert not rt.is_running(handle)


def test_quarantined_workspace_is_refused_under_any_container_name(
    layout: WorkspaceLayout, podman: FakePodman, workspace: Workspace
) -> None:
    rt = runtime(layout, podman)
    handle = rt.start(spec(workspace))
    podman.rm_behaviour = "fail"
    with pytest.raises(ContainmentError):
        rt.run(handle, ["sleep", "60"], timeout_seconds=1)
    runs_before = podman.commands().count("run")
    with pytest.raises(ContainmentError):
        rt.start(spec(workspace, name="ilmar-exec-1-verify"))
    assert podman.commands().count("run") == runs_before


# -- ALLOWLIST: internal network plus egress proxy sidecar (#40, ADR-0015) ----------

EGRESS = EgressSettings(proxy_image="localhost/ilmar-proxy:1")
HOSTS = ("api.example.com",)


def allowlist_spec(workspace: Workspace, **kwargs: object) -> ContainerSpec:
    return spec(workspace, network=NetworkPolicy.ALLOWLIST, allowed_hosts=HOSTS, **kwargs)


def call_args(podman: FakePodman, *prefix: str) -> list[list[str]]:
    return [c.argv for c in podman.calls if c.argv[1 : 1 + len(prefix)] == list(prefix)]


def test_allowlist_needs_egress_settings(
    layout: WorkspaceLayout, podman: FakePodman, workspace: Workspace
) -> None:
    with pytest.raises(ProviderError, match="EgressSettings"):
        runtime(layout, podman).start(allowlist_spec(workspace))
    assert "run" not in podman.commands()
    assert set(podman.networks) == {"podman"}


def test_allowlist_creates_an_owned_internal_network_without_dns(
    layout: WorkspaceLayout, podman: FakePodman, workspace: Workspace
) -> None:
    runtime(layout, podman, egress=EGRESS).start(allowlist_spec(workspace))
    (create,) = call_args(podman, "network", "create")
    assert "--internal" in create
    assert "--disable-dns" in create
    assert "--ipv6" not in create
    network = podman.networks["ilmar-exec-1-1-net"]
    assert network.internal
    assert not network.dns_enabled
    assert network.labels["io.ilmar.owner.token"] == "tok-1"
    assert network.labels["io.ilmar.role"] == "egress-network"


def test_egress_proxy_sidecar_is_hardened_and_bridges_two_networks(
    layout: WorkspaceLayout, podman: FakePodman, workspace: Workspace
) -> None:
    runtime(layout, podman, egress=EGRESS).start(allowlist_spec(workspace))
    proxy = podman.by_name("ilmar-exec-1-1-egress")
    assert proxy is not None
    args = proxy.run_args
    for option in (
        "--cap-drop=all",
        "--security-opt=no-new-privileges",
        "--read-only",
        "--read-only-tmpfs=false",
        "--env-host=false",
        "--http-proxy=false",
        "--pull=never",
    ):
        assert option in args
    assert args[args.index("--user") + 1] == "65534:65534"
    assert [args[i + 1] for i, a in enumerate(args) if a == "--network"] == [
        "ilmar-exec-1-1-net",
        "podman",
    ]
    (mount,) = [args[i + 1] for i, a in enumerate(args) if a == "--mount"]
    assert mount.endswith("egress_proxy.py,destination=/ilmar-egress-proxy.py,ro=true")
    command = args[args.index("localhost/ilmar-proxy:1") :]
    assert command[:3] == ["localhost/ilmar-proxy:1", "python3", "-I"]
    assert command[command.index("--allow") + 1] == "api.example.com"
    subnet = podman.networks["ilmar-exec-1-1-net"].subnet
    assert command[command.index("--clients") + 1] == subnet
    assert proxy.labels["io.ilmar.role"] == "egress-proxy"


def test_allowlist_sandbox_sits_only_on_the_internal_network(
    layout: WorkspaceLayout, podman: FakePodman, workspace: Workspace
) -> None:
    runtime(layout, podman, egress=EGRESS).start(allowlist_spec(workspace))
    sandbox = podman.by_name("ilmar-exec-1-1")
    proxy = podman.by_name("ilmar-exec-1-1-egress")
    assert sandbox is not None
    assert proxy is not None
    args = sandbox.run_args
    assert [args[i + 1] for i, a in enumerate(args) if a == "--network"] == ["ilmar-exec-1-1-net"]
    assert "--network=none" not in args
    for option in ("--dns=none", "--no-hosts", "--cap-drop=all"):
        assert option in args
    sysctls = {args[i + 1] for i, a in enumerate(args) if a == "--sysctl"}
    assert sysctls == {
        "net.ipv6.conf.all.disable_ipv6=1",
        "net.ipv6.conf.default.disable_ipv6=1",
        "net.ipv4.ping_group_range=1 0",
    }
    proxy_url = f"http://{proxy.networks['ilmar-exec-1-1-net']}:3128"
    assert sandbox.env["HTTPS_PROXY"] == proxy_url
    assert sandbox.env["https_proxy"] == proxy_url
    assert sandbox.labels["io.ilmar.role"] == "sandbox"


def sidecars(podman: FakePodman) -> tuple[bool, bool]:
    return podman.by_name("ilmar-exec-1-1-egress") is not None, (
        "ilmar-exec-1-1-net" in podman.networks
    )


def test_stop_removes_sandbox_proxy_and_network(
    layout: WorkspaceLayout, podman: FakePodman, workspace: Workspace
) -> None:
    rt = runtime(layout, podman, egress=EGRESS)
    handle = rt.start(allowlist_spec(workspace))
    assert sidecars(podman) == (True, True)
    rt.stop(handle, owner=OWNER)
    assert podman.containers == {}
    assert sidecars(podman) == (False, False)


def test_timeout_removes_the_sidecar_too(
    layout: WorkspaceLayout, podman: FakePodman, workspace: Workspace
) -> None:
    rt = runtime(layout, podman, egress=EGRESS)
    handle = rt.start(allowlist_spec(workspace))
    assert rt.run(handle, ["sleep", "60"], timeout_seconds=1).timed_out
    assert podman.containers == {}
    assert sidecars(podman) == (False, False)


def test_restart_replaces_sidecars_without_leaking(
    layout: WorkspaceLayout, podman: FakePodman, workspace: Workspace
) -> None:
    rt = runtime(layout, podman, egress=EGRESS)
    rt.start(allowlist_spec(workspace))
    rt.start(allowlist_spec(workspace))
    assert sorted(c.name for c in podman.containers.values()) == [
        "ilmar-exec-1-1",
        "ilmar-exec-1-1-egress",
    ]
    assert set(podman.networks) == {"podman", "ilmar-exec-1-1-net"}


def test_owned_leftovers_of_a_crashed_start_are_cleaned_first(
    layout: WorkspaceLayout, podman: FakePodman, workspace: Workspace
) -> None:
    rt = runtime(layout, podman, egress=EGRESS)
    rt.start(allowlist_spec(workspace))
    sandbox = podman.by_name("ilmar-exec-1-1")
    assert sandbox is not None
    del podman.containers[sandbox.id]  # the sandbox vanished, sidecars remain
    rt.start(allowlist_spec(workspace))
    assert len([c for c in podman.containers.values() if c.name.endswith("-egress")]) == 1


def test_foreign_sidecar_resources_are_never_touched(
    layout: WorkspaceLayout, podman: FakePodman, workspace: Workspace
) -> None:
    other = {"io.ilmar.owner.instance": "inst-b", "io.ilmar.owner.execution": "exec-1"}
    other |= {"io.ilmar.owner.attempt": "1", "io.ilmar.owner.token": "tok-9"}
    podman.networks["ilmar-exec-1-1-net"] = FakeNetwork(
        "ilmar-exec-1-1-net", other, "10.89.9.0/24", internal=True, dns_enabled=False
    )
    with pytest.raises(ProviderError, match="network create"):
        runtime(layout, podman, egress=EGRESS).start(allowlist_spec(workspace))
    assert podman.networks["ilmar-exec-1-1-net"].labels == other
    assert "run" not in podman.commands()


def test_a_proxy_that_never_becomes_ready_fails_closed(
    layout: WorkspaceLayout, podman: FakePodman, workspace: Workspace
) -> None:
    podman.proxy_ready = False
    ticks = iter(range(1000))
    rt = runtime(
        layout,
        podman,
        egress=EGRESS,
        clock=lambda: float(next(ticks)),
        sleep=lambda seconds: None,
    )
    with pytest.raises(ProviderError, match="did not become ready"):
        rt.start(allowlist_spec(workspace))
    assert podman.containers == {}
    assert sidecars(podman) == (False, False)


def test_egress_settings_are_validated() -> None:
    with pytest.raises(ValueError, match="image"):
        EgressSettings(proxy_image="-x")
    with pytest.raises(ValueError, match="network"):
        EgressSettings(proxy_image="img", egress_network="bad name")
    with pytest.raises(ValueError, match="unprivileged"):
        EgressSettings(proxy_image="img", proxy_port=80)


def test_extra_hosts_pin_addresses_in_the_proxy_only(
    layout: WorkspaceLayout, podman: FakePodman, workspace: Workspace
) -> None:
    egress = EgressSettings(
        proxy_image="localhost/ilmar-proxy:1", extra_hosts={"API.Example.com": "10.1.2.3"}
    )
    runtime(layout, podman, egress=egress).start(allowlist_spec(workspace))
    proxy = podman.by_name("ilmar-exec-1-1-egress")
    sandbox = podman.by_name("ilmar-exec-1-1")
    assert proxy is not None
    assert sandbox is not None
    assert proxy.run_args[proxy.run_args.index("--add-host") + 1] == "api.example.com:10.1.2.3"
    assert "--add-host" not in sandbox.run_args


@pytest.mark.parametrize(
    ("hosts", "message"),
    [({"1.2.3.4": "1.2.3.4"}, "hostname"), ({"api.example.com": "not-an-ip"}, "IP")],
)
def test_extra_hosts_are_validated(hosts: dict[str, str], message: str) -> None:
    with pytest.raises(ValueError, match=message):
        EgressSettings(proxy_image="img", extra_hosts=hosts)


# -- PR #41 review: partial sidecar teardown fails closed ------------------------------


def break_teardown(podman: FakePodman, part: str) -> None:
    if part == "proxy":
        podman.fail_rm_names.add("ilmar-exec-1-1-egress")
    else:
        podman.network_rm_fails = True


def repair_teardown(podman: FakePodman) -> None:
    podman.fail_rm_names.clear()
    podman.network_rm_fails = False


def runs_of(podman: FakePodman, name: str) -> int:
    return sum(1 for c in call_args(podman, "run") if c[c.index("--name") + 1] == name)


@pytest.mark.parametrize("part", ["proxy", "network"])
@pytest.mark.parametrize("how", ["stop", "timeout", "stop-signal"])
def test_failed_sidecar_teardown_quarantines_the_workspace(
    layout: WorkspaceLayout, podman: FakePodman, workspace: Workspace, part: str, how: str
) -> None:
    rt = runtime(layout, podman, egress=EGRESS)
    handle = rt.start(allowlist_spec(workspace))
    break_teardown(podman, part)

    def tear_down() -> object:
        if how == "stop":
            rt.stop(handle, owner=OWNER)
            return None
        if how == "stop-signal":  # ADR-0025 §2: exactly what a timeout does
            return rt.run(handle, ["sleep", "60"], timeout_seconds=300, stop=SetAfter(1))
        return rt.run(handle, ["sleep", "60"], timeout_seconds=1)  # containment, not a result

    with pytest.raises(ContainmentError, match="ilmar-exec-1-1"):
        tear_down()
    assert podman.by_name("ilmar-exec-1-1") is None  # the sandbox itself went
    assert sidecars(podman) != (False, False)  # but not all of its egress resources

    # Retrying under the same or another container name is refused, and no
    # sandbox is started on the workspace while egress resources remain.
    for name in ("ilmar-exec-1-1-retry", "ilmar-exec-1-1"):  # another name first
        with pytest.raises(ContainmentError):
            rt.start(allowlist_spec(workspace, name=name))
        with pytest.raises(ContainmentError):
            rt.start(spec(workspace, name=name))  # NONE too: the workspace is what is owed
    assert runs_of(podman, "ilmar-exec-1-1") == 1
    assert runs_of(podman, "ilmar-exec-1-1-retry") == 0

    # Once removal succeeds, the obligation is discharged and the workspace usable.
    repair_teardown(podman)
    replacement = rt.start(allowlist_spec(workspace))
    assert replacement.id != handle.id
    assert len([c for c in podman.containers.values() if c.name.endswith("-egress")]) == 1


def test_sidecar_quarantine_is_per_workspace(
    layout: WorkspaceLayout, podman: FakePodman, workspace: Workspace
) -> None:
    rt = runtime(layout, podman, egress=EGRESS)
    handle = rt.start(allowlist_spec(workspace))
    break_teardown(podman, "proxy")
    with pytest.raises(ContainmentError):
        rt.stop(handle, owner=OWNER)
    other = layout.agent_workspace("exec-2", 1)
    other_spec = spec(
        (other, ensure_directory(other, root=layout.workspaces_root)), name="ilmar-exec-2-1"
    )
    assert rt.start(other_spec).name == "ilmar-exec-2-1"


def test_failed_cleanup_after_a_failed_start_quarantines_the_workspace(
    layout: WorkspaceLayout, podman: FakePodman, workspace: Workspace
) -> None:
    podman.proxy_ready = False
    podman.network_rm_fails = True
    ticks = iter(range(1000))
    rt = runtime(
        layout, podman, egress=EGRESS, clock=lambda: float(next(ticks)), sleep=lambda s: None
    )
    with pytest.raises(ContainmentError):
        rt.start(allowlist_spec(workspace))
    with pytest.raises(ContainmentError):
        rt.start(spec(workspace, name="ilmar-exec-1-other"))
    assert runs_of(podman, "ilmar-exec-1-other") == 0
    podman.network_rm_fails = False
    podman.proxy_ready = True
    rt.start(spec(workspace, name="ilmar-exec-1-other"))
    assert "ilmar-exec-1-1-net" not in podman.networks


# -- remove_owned: from labels alone, fail closed (#11c, ADR-0022) -------------------------


def test_remove_owned_never_removes_a_network_replaced_after_its_labels_were_read(
    layout: WorkspaceLayout, podman: FakePodman, workspace: Workspace
) -> None:
    """Review of 28a4a89: the network is removed by the ID read with its owner labels."""
    runtime(layout, podman, egress=EGRESS).start(allowlist_spec(workspace))
    name = "ilmar-exec-1-1"
    foreign = FakeNetwork(name + "-net", {"io.ilmar.owner.token": "other"}, "10.99.0.0/24",
                          internal=True, dns_enabled=False)  # fmt: skip

    def replace(network: str) -> None:  # someone swaps the network in between
        if network == name + "-net":
            podman.after_network_inspect = None
            del podman.networks[network]
            podman.networks[network] = foreign

    podman.after_network_inspect = replace
    fresh = runtime(layout, podman, egress=EGRESS)
    assert fresh.remove_owned(name, owner=OWNER) is True  # ours is gone
    assert podman.networks[name + "-net"] is foreign  # the stranger's is untouched
    assert podman.by_name(name) is None
    assert podman.by_name(name + "-egress") is None


@pytest.mark.parametrize("part", ["", "-egress"])
def test_remove_owned_never_removes_a_container_replaced_after_its_labels_were_read(
    layout: WorkspaceLayout, podman: FakePodman, workspace: Workspace, part: str
) -> None:
    """#15: an ownership change between validation and operation. Ours is replaced
    by a stranger's container of the same name after its labels were read; the
    removal targets the ID read with the labels, so the stranger's is untouched."""
    runtime(layout, podman, egress=EGRESS).start(allowlist_spec(workspace))
    name = "ilmar-exec-1-1" + part
    ours = podman.by_name(name)
    assert ours is not None
    strangers: list[str] = []

    def replace(container_id: str) -> None:  # right after its labels were read
        if container_id == ours.id:
            podman.after_container_inspect = None
            del podman.containers[ours.id]
            strangers.append(podman.create(name, {"io.ilmar.owner.token": "other"}))

    podman.after_container_inspect = replace
    fresh = runtime(layout, podman, egress=EGRESS)
    assert fresh.remove_owned("ilmar-exec-1-1", owner=OWNER) is True
    (stranger,) = strangers
    assert podman.containers[stranger].labels == {"io.ilmar.owner.token": "other"}
    assert podman.by_name(name) is podman.containers[stranger]  # still there, still theirs
    assert stranger not in [c.argv[-1] for c in podman.calls if c.argv[1] == "rm"]


def test_remove_owned_works_without_any_memory_of_the_sandbox(
    layout: WorkspaceLayout, podman: FakePodman, workspace: Workspace
) -> None:
    runtime(layout, podman, egress=EGRESS).start(allowlist_spec(workspace))
    fresh = runtime(layout, podman, egress=EGRESS)
    assert fresh.remove_owned("ilmar-exec-1-1", owner=OWNER) is True
    assert set(podman.networks) == {"podman"}
    assert podman.containers == {}
    assert fresh.remove_owned("ilmar-exec-1-1", owner=OWNER) is False


@pytest.mark.parametrize("part", ["-egress", "-net"])
def test_remove_owned_touches_nothing_if_any_part_is_foreign(
    layout: WorkspaceLayout, podman: FakePodman, workspace: Workspace, part: str
) -> None:
    runtime(layout, podman, egress=EGRESS).start(allowlist_spec(workspace))
    name = "ilmar-exec-1-1"
    if part == "-net":
        podman.networks[name + part].labels["io.ilmar.owner.token"] = "spoofed"
    else:
        proxy = podman.by_name(name + part)
        assert proxy is not None
        proxy.labels["io.ilmar.owner.token"] = "spoofed"
    before = (set(podman.networks), set(podman.containers))
    with pytest.raises(OwnershipConflictError):
        runtime(layout, podman, egress=EGRESS).remove_owned(name, owner=OWNER)
    assert (set(podman.networks), set(podman.containers)) == before


def test_a_coordinator_killed_mid_command_leaves_no_credential_on_disk(tmp_path: Path) -> None:
    """The crash drill's concern, offline: SIGKILL while the env descriptor is in use."""
    import subprocess
    import sys

    spool = tmp_path / "tmp"
    spool.mkdir()
    child = (
        "import os, signal\n"
        "from pathlib import Path\n"
        "from ilmar.adapters.podman.runtime import PodmanRuntime\n"
        "from ilmar.core.layout import WorkspaceLayout\n"
        f"rt = PodmanRuntime(layout=WorkspaceLayout.under(Path({str(tmp_path)!r})))\n"
        "with rt._env_file({'ANTHROPIC_API_KEY': 'sk-ant-must-not-persist'}):\n"
        "    os.kill(os.getpid(), signal.SIGKILL)\n"
    )
    src = Path(__file__).resolve().parents[2] / "src"
    done = subprocess.run(  # noqa: S603 - a fixed child program
        [sys.executable, "-c", child], env={"TMPDIR": str(spool), "PYTHONPATH": str(src)},
        check=False,
    )  # fmt: skip
    assert done.returncode == -9  # it really died there
    leftovers = [p for p in spool.rglob("*") if b"must-not-persist" in p.read_bytes()]
    assert leftovers == []
    assert list(spool.iterdir()) == []


# -- the stop signal (ADR-0025 §2, #16 part 2) ---------------------------------------


def test_a_stop_signal_reaches_the_executor_and_removes_the_container(
    layout: WorkspaceLayout, podman: FakePodman, workspace: Workspace
) -> None:
    rt = runtime(layout, podman)
    handle = rt.start(spec(workspace))
    stop = SetAfter(2)
    result = rt.run(handle, ["sleep", "60"], timeout_seconds=300, stop=stop)
    assert (result.cancelled, result.timed_out) == (True, False)
    (exec_call,) = [c for c in podman.calls if c.argv[1] == "exec"]
    assert exec_call.stopped_by is stop
    assert ["rm", "--force", "--time", "0", handle.id] in [c.argv[1:] for c in podman.calls]
    assert podman.containers == {}


def test_a_stop_signal_also_reaches_a_command_with_credentials(
    layout: WorkspaceLayout, podman: FakePodman, workspace: Workspace
) -> None:
    rt = runtime(layout, podman)
    handle = rt.start(spec(workspace))
    stop = SetAfter(1)
    result = rt.run(handle, ["sleep", "60"], timeout_seconds=300,
                    env={"ANTHROPIC_API_KEY": "sk-model"}, stop=stop)  # fmt: skip
    assert result.cancelled
    (exec_call,) = [c for c in podman.calls if c.argv[1] == "exec"]
    assert exec_call.stopped_by is stop
    assert podman.containers == {}


def test_a_preset_stop_signal_execs_nothing_and_removes_everything(
    layout: WorkspaceLayout, podman: FakePodman, workspace: Workspace
) -> None:
    rt = runtime(layout, podman, egress=EGRESS)
    handle = rt.start(allowlist_spec(workspace))
    stop = threading.Event()
    stop.set()
    result = rt.run(handle, ["write", "ran.txt", "x"], timeout_seconds=30, stop=stop)
    assert result.cancelled
    assert "exec" not in podman.commands()
    assert not (workspace[0] / "ran.txt").exists()
    assert podman.containers == {}
    assert sidecars(podman) == (False, False)


def test_a_stop_signal_removes_the_sidecar_too(
    layout: WorkspaceLayout, podman: FakePodman, workspace: Workspace
) -> None:
    rt = runtime(layout, podman, egress=EGRESS)
    handle = rt.start(allowlist_spec(workspace))
    assert rt.run(handle, ["sleep", "60"], timeout_seconds=300, stop=SetAfter(1)).cancelled
    assert podman.containers == {}
    assert sidecars(podman) == (False, False)


@pytest.mark.parametrize("behaviour", ["fail", "hang"])
def test_unconfirmed_removal_after_a_stop_fails_closed_and_quarantines(
    layout: WorkspaceLayout, podman: FakePodman, workspace: Workspace, behaviour: str
) -> None:
    rt = runtime(layout, podman)
    handle = rt.start(spec(workspace))
    podman.rm_behaviour = behaviour
    with pytest.raises(ContainmentError) as caught:
        rt.run(handle, ["sleep", "60"], timeout_seconds=300, stop=SetAfter(1))
    assert caught.value.container_id == handle.id
    assert handle.id in podman.containers  # reported, not assumed gone
    runs_before = podman.commands().count("run")
    with pytest.raises(ContainmentError):
        rt.start(spec(workspace, name="ilmar-exec-1-1-retry"))  # under any name
    assert podman.commands().count("run") == runs_before
    podman.rm_behaviour = "ok"
    replacement = rt.start(spec(workspace))
    assert handle.id not in podman.containers
    assert replacement.id != handle.id


def test_a_stop_signal_never_affects_a_container_of_another_owner(
    layout: WorkspaceLayout, podman: FakePodman, workspace: Workspace
) -> None:
    """The runtime removes the container by the id the handle holds, never by name."""
    rt = runtime(layout, podman)
    handle = rt.start(spec(workspace))
    foreign = podman.create("ilmar-someone-else", {})
    assert rt.run(handle, ["sleep", "60"], timeout_seconds=300, stop=SetAfter(1)).cancelled
    assert foreign in podman.containers
    removed = [c.argv[-1] for c in podman.calls if c.argv[1] == "rm"]
    assert removed == [handle.id]


# The real executor: a stop kills exactly the executor's own process group.


def _later(event: threading.Event, seconds: float) -> threading.Timer:
    timer = threading.Timer(seconds, event.set)
    timer.start()
    return timer


def test_subprocess_executor_stops_within_the_poll_interval() -> None:
    stop = threading.Event()
    timer = _later(stop, 0.3)
    began = time.monotonic()
    done = subprocess_executor([sys.executable, "-c", "import time; time.sleep(60)"],
                               dict(os.environ), 120, stop=stop)  # fmt: skip
    elapsed = time.monotonic() - began
    timer.cancel()
    assert (done.cancelled, done.timed_out) == (True, False)
    assert done.returncode == TIMED_OUT_EXIT_CODE
    assert elapsed < 0.3 + 1.0 + 1.0  # the signal is looked at at least once a second


def test_subprocess_executor_stop_kills_the_whole_own_process_group(tmp_path: Path) -> None:
    marker = tmp_path / "grandchild-survived"
    grandchild = tmp_path / "grandchild.py"
    grandchild.write_text(f"import time\ntime.sleep(3)\nopen({str(marker)!r}, 'w').close()\n")
    parent = tmp_path / "parent.py"
    parent.write_text(
        "import subprocess, sys, time\n"
        f"subprocess.Popen([sys.executable, {str(grandchild)!r}])\n"
        "time.sleep(60)\n"
    )
    stop = threading.Event()
    timer = _later(stop, 0.5)
    done = subprocess_executor([sys.executable, str(parent)], dict(os.environ), 120, stop=stop)
    timer.cancel()
    assert done.cancelled
    time.sleep(4)
    assert not marker.exists()


def test_subprocess_executor_stop_never_signals_another_process(tmp_path: Path) -> None:
    """Only the executor's own group is killed: not an unrelated process, not one
    in the coordinator's own process group, not the coordinator itself."""
    unrelated = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"], start_new_session=True
    )
    sibling = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        stop = threading.Event()
        stop.set()
        done = subprocess_executor([sys.executable, "-c", "import time; time.sleep(60)"],
                                   dict(os.environ), 120, stop=stop)  # fmt: skip
        assert done.cancelled
        time.sleep(0.5)
        assert unrelated.poll() is None
        assert sibling.poll() is None
        assert os.getpgid(sibling.pid) == os.getpgrp()
    finally:
        for process in (unrelated, sibling):
            process.send_signal(signal.SIGKILL)
            process.wait(10)


def test_subprocess_executor_stops_while_a_leftover_holds_the_pipe(tmp_path: Path) -> None:
    script = tmp_path / "detach.py"
    script.write_text(
        "import subprocess, sys\n"
        "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
    )
    stop = threading.Event()
    timer = _later(stop, 0.5)
    began = time.monotonic()
    done = subprocess_executor([sys.executable, str(script)], dict(os.environ), 120, stop=stop)
    timer.cancel()
    assert (done.cancelled, done.timed_out) == (True, False)
    assert time.monotonic() - began < 15


def test_subprocess_executor_with_an_unset_signal_behaves_as_before() -> None:
    stop = threading.Event()
    done = subprocess_executor(
        [sys.executable, "-c", "import sys; print('done'); sys.exit(4)"], {}, 30, stop=stop
    )
    assert (done.returncode, done.stdout.strip(), done.cancelled, done.timed_out) == (
        4, "done", False, False)  # fmt: skip
    timed = subprocess_executor([sys.executable, "-c", "import time; time.sleep(60)"], {}, 1,
                                stop=stop)  # fmt: skip
    assert (timed.timed_out, timed.cancelled) == (True, False)


# -- runtime-side lifetime (ADR-0029 §1, #90) ---------------------------------------


@pytest.mark.parametrize(("lifetime", "expected"), [(60.0, "60"), (4260.2, "4261"), (0.5, "1")])
def test_a_lifetime_becomes_conmons_timeout_rounded_up(
    layout: WorkspaceLayout, podman: FakePodman, workspace: Workspace, lifetime: float,
    expected: str,
) -> None:  # fmt: skip
    runtime(layout, podman).start(spec(workspace, lifetime_seconds=lifetime))
    timeouts = [a for a in run_args(podman) if a.startswith("--timeout")]
    assert timeouts == [f"--timeout={expected}"]
    assert run_args(podman).index(timeouts[0]) < run_args(podman).index("localhost/ilmar-test:1")


def test_no_lifetime_sets_no_timeout(
    layout: WorkspaceLayout, podman: FakePodman, workspace: Workspace
) -> None:
    runtime(layout, podman).start(spec(workspace))
    assert not [a for a in run_args(podman) if a.startswith("--timeout")]


def test_the_egress_proxy_gets_the_same_lifetime(
    layout: WorkspaceLayout, podman: FakePodman, workspace: Workspace
) -> None:
    runtime(layout, podman, egress=EGRESS).start(allowlist_spec(workspace, lifetime_seconds=90))
    proxy = podman.by_name("ilmar-exec-1-1-egress")
    sandbox = podman.by_name("ilmar-exec-1-1")
    assert proxy is not None
    assert sandbox is not None
    assert "--timeout=90" in proxy.run_args
    assert "--timeout=90" in sandbox.run_args
