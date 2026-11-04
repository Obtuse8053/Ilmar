# SPDX-License-Identifier: Apache-2.0
"""PodmanRuntime details beyond the shared contract (ADR-0011, ADR-0014)."""

from __future__ import annotations

import logging
import os
import sys
import time
from pathlib import Path, PurePosixPath

import pytest

from ilmar.adapters.podman import PodmanRuntime, SandboxProfile
from ilmar.adapters.podman.runtime import (
    LABEL_MANAGED,
    PODMAN_ENVIRONMENT,
    proc_root_identity,
    subprocess_executor,
)
from ilmar.core.layout import WorkspaceLayout
from ilmar.core.safe_fs import ensure_directory
from ilmar.domain.errors import OwnershipConflictError, ProviderError
from ilmar.domain.workspace import (
    ContainerHandle,
    ContainerSpec,
    DirectoryIdentity,
    NetworkPolicy,
    ResourceLimits,
    ResourceOwner,
)

from fake_podman import FakePodman

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
        runtime(layout, podman).start(spec(workspace, network=NetworkPolicy.UNRESTRICTED))
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


def test_values_travel_in_a_private_env_file_not_argv(
    layout: WorkspaceLayout, podman: FakePodman, workspace: Workspace
) -> None:
    rt = runtime(layout, podman)
    handle = rt.start(spec(workspace, env={"ANTHROPIC_API_KEY": "sk-model"}))
    rt.run(handle, ["echo", "x"], timeout_seconds=5, env={"STEP": "two"})
    started = next(c for c in podman.calls if c.argv[1] == "run")
    executed = next(c for c in podman.calls if c.argv[1] == "exec")
    assert started.env_file == {"HOME": "/home/ilmar", "ANTHROPIC_API_KEY": "sk-model"}
    assert executed.env_file == {"STEP": "two"}
    for call in podman.calls:
        assert not any("sk-model" in arg or arg == "two" for arg in call.argv)
    for path in (started.argv, executed.argv):
        env_file = path[path.index("--env-file") + 1]
        assert not Path(env_file).exists()  # deleted right after use


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
    assert podman.commands()[-1] == "rm"


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
    assert podman.commands()[-1] == "rm"
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
