# SPDX-License-Identifier: Apache-2.0
"""Opt-in live test: real Claude Code in an ALLOWLIST sandbox (#8, ADR-0011 §6).

Records the hosts Claude Code actually contacts through the egress proxy and
fails if it needed anything outside the allow-list. Needs, besides the Podman
suite's requirements:

* ``ILMAR_TEST_CLAUDE_IMAGE``: an agent image built from
  ``images/claude-code/Containerfile`` at a pinned version (built locally under
  Anthropic's terms; Ilmar never distributes it);
* ``ILMAR_TEST_PODMAN_PROXY_IMAGE``: an image with ``python3`` for the proxy;
* ``ANTHROPIC_API_KEY``: a model credential (costs a few cents);
* ``ILMAR_TEST_CLAUDE_VERSION``: the pinned ``x.y.z`` the image was built
  with. Optional for a local smoke run, which then writes no record. Under
  ``ILMAR_LIVE_REQUIRED=1`` it is required: an unpinned run fails and is never
  Gate 1 evidence.

It is also ADR-0035's pinned-version stream check (#13): every event the real
CLI emits must be classified, exactly one result must appear and only verified
after-result telemetry may follow it. With ``ILMAR_LIVE_REPORT_DIR`` set and
a pinned version, the sequence of event *types* (never content) is written,
marked ``"provenance": "recorded"``; only such a record, reviewed, meets the
enablement gate. The checked-in ``*-synthetic.json`` fixture never does.
"""

from __future__ import annotations

import os
import re
from datetime import UTC, datetime
from pathlib import Path

import pytest

from ilmar.adapters.claude_code import ClaudeCodeRunner, ClaudeCodeSettings
from ilmar.adapters.claude_code.credential_route import SEALED_PATHS
from ilmar.adapters.claude_code.stream import (
    ACTIVITY_TYPES,
    RESULT_TYPE,
    VERIFIED_TELEMETRY,
)
from ilmar.adapters.podman import EgressSettings, PodmanRuntime
from ilmar.core.layout import WorkspaceLayout
from ilmar.domain.agent import AgentOutcome, AgentRequest
from ilmar.domain.workspace import ContainerSpec, NetworkPolicy, ResourceOwner
from live_harness import LiveGuardError, Secret, pinned_claude_version, write_report

from helpers import task
from integration.podman_support import (
    PODMAN,
    PROXY_IMAGE,
    identity_of,
    make_workspace,
    podman,
    remove_everything,
)

pytestmark = pytest.mark.integration

IMAGE = os.environ.get("ILMAR_TEST_CLAUDE_IMAGE", "")
KEY = os.environ.get("ANTHROPIC_API_KEY", "")
NAME = "ilmar-it-claude-live"
ALLOWED = ("api.anthropic.com",)


def record_smoke_test(contacted: list[str], version: str | None) -> None:
    """The #44 record: version, image digest, date and hosts; never the key.

    Written only for a pinned version: an unpinned run is not evidence.
    """
    directory = os.environ.get("ILMAR_LIVE_REPORT_DIR")
    if not directory or version is None:
        return
    digest = podman("image", "inspect", "--format", "{{.Digest}}", IMAGE).stdout.strip()
    path = write_report(Path(directory), "issue-44-smoke-test", {
        "claude_code_version": version,
        "image": IMAGE, "image_digest": digest, "hosts_contacted": contacted,
        "denied": [], "at": datetime.now(UTC).isoformat(),
    }, [Secret("ANTHROPIC_API_KEY", KEY)])  # fmt: skip
    print(f"#44 record written to {path}")


_IDENTIFIER = re.compile(r"[a-z][a-z0-9_]{0,63}")


class EventTypes:
    """Records ``(type, subtype, after_result)`` per event; never any content.

    A type or subtype that is not a short identifier is recorded as a
    placeholder so nothing the model wrote can reach the fixture.
    """

    def __init__(self) -> None:
        self.events: list[tuple[str, str | None, bool]] = []

    def __call__(self, kind: str, subtype: str | None, after_result: bool) -> None:
        self.events.append((_safe(kind), None if subtype is None else _safe(subtype), after_result))


def _safe(name: str) -> str:
    return name if _IDENTIFIER.fullmatch(name) else "<not-an-identifier>"


def unclassified(events: list[tuple[str, str | None, bool]]) -> list[str]:
    """Every problem ADR-0035 would reject, described by type only."""
    problems: list[str] = []
    results = [e for e in events if e[0] == RESULT_TYPE]
    if len(results) != 1:
        problems.append(f"{len(results)} result events")
    for kind, subtype, after in events:
        telemetry = next(
            (t for t in VERIFIED_TELEMETRY
             if t.type == kind and (t.subtype is None or t.subtype == subtype)),
            None,
        )  # fmt: skip
        if telemetry is not None:
            if after and not telemetry.after_result:
                problems.append(f"{kind}/{subtype} after the result")
        elif kind == RESULT_TYPE:
            continue
        elif kind not in ACTIVITY_TYPES:
            problems.append(f"unknown event type {kind}/{subtype}")
        elif after:
            problems.append(f"agent activity {kind}/{subtype} after the result")
    return problems


def record_stream_fixture(events: list[tuple[str, str | None, bool]], version: str | None) -> None:
    """The ADR-0035 enablement record: the pinned version's event types.

    Written only for a pinned version, and marked ``recorded`` so it can never
    be confused with the synthetic fixture.
    """
    directory = os.environ.get("ILMAR_LIVE_REPORT_DIR")
    if not directory or version is None:
        return
    path = write_report(Path(directory), "issue-13-stream-event-types", {
        "provenance": "recorded",
        "note": "Recorded by tests/integration/test_claude_code_live.py from a real run.",
        "claude_code_version": version,
        "image": IMAGE, "at": datetime.now(UTC).isoformat(),
        "events": [{"type": k, "subtype": s, "after_result": a} for k, s, a in events],
    }, [Secret("ANTHROPIC_API_KEY", KEY)])  # fmt: skip
    print(f"stream event-type record written to {path}")


REQUIRED = os.environ.get("ILMAR_LIVE_REQUIRED") == "1"
MISSING = not (PODMAN and IMAGE and KEY and PROXY_IMAGE)
NEEDS = "podman, ILMAR_TEST_CLAUDE_IMAGE, ILMAR_TEST_PODMAN_PROXY_IMAGE and ANTHROPIC_API_KEY"


@pytest.fixture(autouse=True)
def _required_never_skips() -> None:
    """Under ``ILMAR_LIVE_REQUIRED=1`` (scripts/live_run.py) a missing need fails the
    run, and so does a missing or unpinned Claude Code version."""
    try:
        pinned_claude_version(os.environ, required=REQUIRED)
    except LiveGuardError as error:
        pytest.fail(str(error))
    if MISSING and REQUIRED:
        pytest.fail(f"#44 needs {NEEDS}")


@pytest.mark.skipif(MISSING and not REQUIRED, reason=f"set {NEEDS}")
def test_claude_code_completes_a_tiny_task_using_only_allowed_hosts(tmp_path: Path) -> None:
    version = pinned_claude_version(os.environ, required=REQUIRED)
    layout = WorkspaceLayout.under(tmp_path.resolve() / "state")
    layout.workspaces_root.mkdir(parents=True)
    workspace = make_workspace(layout, "live")
    (workspace / "README.md").write_text("A scratch project.\n")
    owner = ResourceOwner(instance_id="it", execution_id="live", attempt=1, token="tok-live")
    runtime = PodmanRuntime(layout=layout, egress=EgressSettings(proxy_image=PROXY_IMAGE))
    remove_everything(NAME)
    handle = runtime.start(
        ContainerSpec(
            image=IMAGE,
            host_workspace=workspace,
            name=NAME,
            owner=owner,
            workspace_identity=identity_of(workspace),
            network=NetworkPolicy.ALLOWLIST,
            allowed_hosts=ALLOWED,
            sealed_paths=SEALED_PATHS,  # ADR-0036, as the claude-code adapter sets them
        )
    )
    try:
        observed = EventTypes()
        runner = ClaudeCodeRunner(
            api_key=KEY,
            settings=ClaudeCodeSettings(max_turns=5, expected_version=version),
            event_observer=observed,
        )
        result = runner.run(
            runtime,
            handle,
            AgentRequest(
                task=task("live", title="Create a file"),
                instructions="Create a file named hello.txt containing exactly: hi",
                timeout_seconds=300,
                stall_timeout_seconds=120,
            ),
        )
        proxy_log = podman("logs", f"{NAME}-egress")
        logs = proxy_log.stdout + proxy_log.stderr
        contacted = sorted(set(re.findall(r"ilmar-egress ALLOW host=(\S+)", logs)))
        denied = [line for line in logs.splitlines() if "ilmar-egress DENY" in line]
        assert not denied, f"Claude Code needed hosts outside the allow-list: {denied}"
        assert set(contacted) <= set(ALLOWED), contacted
        assert result.outcome is AgentOutcome.COMPLETED, result.summary
        assert observed.events, "no stream events were observed"
        assert not unclassified(observed.events), unclassified(observed.events)
        assert (workspace / "hello.txt").read_text().strip() == "hi"
        assert contacted, "Claude Code never reached the model API"
        record_smoke_test(contacted, version)
        record_stream_fixture(observed.events, version)
    finally:
        runtime.stop(handle, owner=owner)
